"""
Match outcome rules - the single place that knows HOW a match result is decided.

Vocabulary (kept deliberately separate):

  status        lifecycle of a match: 'pending' -> 'on-going' -> 'completed'
                (unchanged; owned by routes/match/match_core.py)
  outcome_type  how the result was decided: 'regular' (numeric score) or
                'walkover' (opponent did not show up). NULL = not decided yet,
                or a historical match whose outcome was never recorded.
  Score rows    only meaningful for outcomes that `uses_scores`. A walkover never
                writes a competitive score.

Everything outcome-specific lives in the OUTCOMES registry below, and every
consumer (score API, fixtures, standings, exports) asks this module instead of
comparing strings. Adding a future outcome such as 'forfeit' is one registry
entry:

    FORFEIT = OutcomeDefinition('forfeit', 'Forfeit', uses_scores=False,
                                declarable=True, requires_winner=True)

and it is then accepted by POST /matches/<id>/outcome, labelled in fixtures,
counted by standings, locked against score edits and excluded from the DUPR
export with no other code change. A 'bye' (single team, no opponent) would add
one more validator, but still reuses the same registry, finalization and
bracket-progression code.

Transaction contract: functions here never commit on their own, EXCEPT
`declare_outcome`, which is the one entry point that owns a whole operation and
commits exactly once (rolling everything back on any failure). Callers that
finalize inside their own request (`update_score`) commit once themselves.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from models import db, Match, Score, Tournament
from socket_instance import socketio

logger = logging.getLogger(__name__)

# 'TBD' is what the knockout builder stores for a slot that is not decided yet.
UNASSIGNED_TEAM_IDS = (None, '', 'TBD')

STATUS_PENDING = 'pending'
STATUS_ONGOING = 'on-going'
STATUS_COMPLETED = 'completed'


# ---------------------------------------------------------------------------
# Errors (mapped to HTTP status codes by the routes)
# ---------------------------------------------------------------------------

class MatchOutcomeError(Exception):
    status_code = 400
    code = 'invalid_request'

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class InvalidOutcomeRequest(MatchOutcomeError):
    status_code = 400


class OutcomeNotFound(MatchOutcomeError):
    status_code = 404
    code = 'not_found'


class OutcomeConflict(MatchOutcomeError):
    status_code = 409
    code = 'conflict'


# ---------------------------------------------------------------------------
# Outcome registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OutcomeDefinition:
    name: str                 # value stored in match.outcome_type
    label: str                # human readable, used as match_result for unscored outcomes
    uses_scores: bool         # True: Score rows decide the winner. False: winner_team_id does.
    declarable: bool = False  # can be declared through POST /matches/<id>/outcome
    requires_winner: bool = False


REGULAR = OutcomeDefinition('regular', 'Regular', uses_scores=True)
WALKOVER = OutcomeDefinition('walkover', 'Walkover', uses_scores=False,
                             declarable=True, requires_winner=True)

OUTCOMES = {o.name: o for o in (REGULAR, WALKOVER)}


def uses_scores(outcome_type: Optional[str]) -> bool:
    """True when the numeric Score rows decide this match.

    NULL (live match, or history that was never classified) and unknown values
    keep the pre-existing score-based behaviour.
    """
    definition = OUTCOMES.get(outcome_type)
    return True if definition is None else definition.uses_scores


def result_label(outcome_type: Optional[str], team1_score: int, team2_score: int) -> str:
    """Text shown as `match_result`: 'Walkover' for unscored outcomes, else 'a-b'."""
    if not uses_scores(outcome_type):
        return OUTCOMES[outcome_type].label
    return f"{team1_score}-{team2_score}"


@dataclass(frozen=True)
class DecidedResult:
    winner_team_id: str
    loser_team_id: Optional[str]


def decided_result(outcome_type: Optional[str], team1_id: Optional[str],
                   team2_id: Optional[str], winner_team_id: Optional[str]) -> Optional[DecidedResult]:
    """Winner/loser of a match decided WITHOUT scores, or None for score-decided
    matches. Standings use this instead of comparing numeric scores."""
    if uses_scores(outcome_type) or is_unassigned(winner_team_id):
        return None
    if winner_team_id == team1_id:
        loser = team2_id
    elif winner_team_id == team2_id:
        loser = team1_id
    else:
        return None
    return DecidedResult(winner_team_id=winner_team_id, loser_team_id=loser)


def is_unassigned(team_id: Optional[str]) -> bool:
    return team_id in UNASSIGNED_TEAM_IDS


# ---------------------------------------------------------------------------
# Regular (score based) finalization - used by POST /update-score
# ---------------------------------------------------------------------------

def winner_from_scores(match: Match, team1_score: int, team2_score: int) -> Optional[str]:
    if team1_score > team2_score:
        return match.team1_id
    if team2_score > team1_score:
        return match.team2_id
    return None  # draw


def ensure_scores_editable(match: Match) -> None:
    """Score edits are rejected for matches decided without scores."""
    if not uses_scores(match.outcome_type):
        label = OUTCOMES[match.outcome_type].label.lower()
        raise OutcomeConflict(
            f"Match was decided by {label}; scores cannot be edited.",
            code='outcome_locked')


def ensure_status_change_allowed(match: Match, new_status: str) -> None:
    """A match decided without scores stays completed - it can never be re-opened."""
    if not uses_scores(match.outcome_type) and new_status != STATUS_COMPLETED:
        label = OUTCOMES[match.outcome_type].label.lower()
        raise OutcomeConflict(
            f"Match was decided by {label}; its status cannot be changed.",
            code='outcome_locked')


def finalize_regular_match(match: Match, team1_score: int, team2_score: int) -> None:
    """Finalize a scored match. Does not commit; the caller commits once."""
    match.is_final = True
    match.winner_team_id = winner_from_scores(match, team1_score, team2_score)
    match.outcome_type = REGULAR.name
    match.status = STATUS_COMPLETED
    advance_winner(match)


# ---------------------------------------------------------------------------
# Knockout progression (shared by every outcome)
# ---------------------------------------------------------------------------

def advance_winner(match: Match) -> None:
    """Move match.winner_team_id into its slot of the successor match.

    Does NOT commit (the old helper committed internally, which made it
    impossible to roll back an outcome and its progression together).
    Idempotent: re-running leaves the same slot value and no extra Score rows.
    A missing winner (draw / not decided) is never advanced.
    """
    if not match.successor or is_unassigned(match.winner_team_id):
        return

    successor = Match.query.filter_by(id=match.successor).with_for_update().first()
    if successor is None:
        return

    if successor.predecessor_1 == match.id:
        successor.team1_id = match.winner_team_id
    elif successor.predecessor_2 == match.id:
        successor.team2_id = match.winner_team_id
    else:
        raise OutcomeConflict(
            f"Bracket is inconsistent: match {successor.id} does not list match {match.id} "
            "as a predecessor.", code='bracket_inconsistent')

    ensure_score_rows(successor)


def ensure_score_rows(match: Match) -> None:
    """Create the zero-score rows for a match whose two teams are both known,
    only for teams that do not have one yet (never duplicates)."""
    if is_unassigned(match.team1_id) or is_unassigned(match.team2_id):
        return
    existing = {s.team_id for s in Score.query.filter_by(match_id=match.id).all()}
    for team_id in (match.team1_id, match.team2_id):
        if team_id not in existing:
            db.session.add(Score(match_id=match.id, team_id=team_id, score=0,
                                 tournament_id=match.tournament_id))


# ---------------------------------------------------------------------------
# Declared outcomes (walkover, later forfeit ...) - used by POST /matches/<id>/outcome
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeclaredOutcome:
    match: Match
    changed: bool  # False when an identical earlier request is replayed


def declare_outcome(match_id: int, tournament_id, outcome_type, winner_team_id=None) -> DeclaredOutcome:
    """Validate and apply a declared outcome atomically: outcome fields and
    bracket progression are written together and committed once. Any failure
    rolls the whole operation back."""
    definition = _require_declarable(outcome_type)
    tournament_id = _require_int(tournament_id, 'tournament_id')
    winner = _clean_team_id(winner_team_id)
    if definition.requires_winner and winner is None:
        raise InvalidOutcomeRequest('winner_team_id is required', code='winner_required')

    try:
        if db.session.get(Tournament, tournament_id) is None:
            raise OutcomeNotFound('Tournament not found', code='tournament_not_found')

        # Row lock (InnoDB): concurrent duplicate submissions serialize, the
        # second one then sees the committed result and is treated as a replay.
        match = Match.query.filter_by(id=match_id).with_for_update().first()
        if match is None:
            raise OutcomeNotFound('Match not found', code='match_not_found')
        if match.tournament_id != tournament_id:
            raise OutcomeNotFound('Match does not belong to this tournament', code='match_not_in_tournament')

        if _is_replay(match, definition, winner):
            return DeclaredOutcome(match=match, changed=False)

        _validate_declaration(match, definition, winner)

        match.outcome_type = definition.name
        match.winner_team_id = winner
        match.status = STATUS_COMPLETED
        match.is_final = True
        ensure_score_rows(match)   # zero placeholders only if missing; no competitive score
        advance_winner(match)

        db.session.commit()
        return DeclaredOutcome(match=match, changed=True)
    except Exception:
        db.session.rollback()
        raise


def _require_declarable(outcome_type) -> OutcomeDefinition:
    definition = OUTCOMES.get(outcome_type) if isinstance(outcome_type, str) else None
    if definition is None or not definition.declarable:
        supported = ', '.join(sorted(o.name for o in OUTCOMES.values() if o.declarable))
        raise InvalidOutcomeRequest(
            f"Unsupported outcome_type. Supported: {supported}", code='invalid_outcome_type')
    return definition


def _require_int(value, field: str) -> int:
    if isinstance(value, bool):
        raise InvalidOutcomeRequest(f'{field} must be an integer', code=f'invalid_{field}')
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InvalidOutcomeRequest(f'{field} is required and must be an integer', code=f'invalid_{field}')


def _clean_team_id(value) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidOutcomeRequest('winner_team_id must be a non-empty string', code='invalid_winner')
    return value.strip()


def _is_replay(match: Match, definition: OutcomeDefinition, winner: Optional[str]) -> bool:
    return (match.outcome_type == definition.name
            and match.status == STATUS_COMPLETED
            and bool(match.is_final)
            and match.winner_team_id == winner)


def _validate_declaration(match: Match, definition: OutcomeDefinition, winner: Optional[str]) -> None:
    if is_unassigned(match.team1_id) or is_unassigned(match.team2_id):
        raise OutcomeConflict('Both teams must be assigned before an outcome can be declared',
                              code='teams_not_assigned')

    if match.is_final or match.status == STATUS_COMPLETED:
        current = match.outcome_type or 'unknown'
        raise OutcomeConflict(f'Match is already completed (outcome: {current})', code='already_completed')

    if match.status not in (STATUS_PENDING, STATUS_ONGOING):
        raise OutcomeConflict(f"Match status '{match.status}' cannot be finalized", code='invalid_status')

    if definition.requires_winner and winner not in (match.team1_id, match.team2_id):
        raise InvalidOutcomeRequest('winner_team_id must be one of the two teams in this match',
                                    code='invalid_winner')

    # A walkover means no play took place. Refuse instead of silently
    # discarding or keeping live scores that would contradict the outcome.
    if not definition.uses_scores and Score.query.filter(
            Score.match_id == match.id, Score.score > 0).first() is not None:
        raise OutcomeConflict(
            f'Scores have already been recorded for this match; it cannot be declared a {definition.label.lower()}',
            code='scores_recorded')


# ---------------------------------------------------------------------------
# Serialization + live updates
# ---------------------------------------------------------------------------

def serialize_outcome(match: Match) -> dict:
    return {
        'match_id': match.id,
        'tournament_id': match.tournament_id,
        'status': match.status,
        'is_final': bool(match.is_final),
        'outcome_type': match.outcome_type,
        'winner_team_id': match.winner_team_id,
    }


def publish_result_update(payload: dict) -> None:
    """Emit the existing 'score_update' Socket.IO event. Call ONLY after the
    transaction has committed; a failing socket must never fail the request."""
    try:
        socketio.emit('score_update', payload, namespace='/scores')
    except Exception:  # pragma: no cover - defensive
        logger.exception('Failed to emit score_update')
