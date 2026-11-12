"""POST /matches/<id>/outcome and the interaction of walkovers with the existing score/status APIs."""
import pytest

from conftest import outcome_url, score_rows, walkover
from models import db, Match, Score
import services.match_outcomes as outcomes


def reload_match(match_id):
    db.session.expire_all()
    return db.session.get(Match, match_id)


# --- Test 1 / 12: regular scoring still works and is now classified ----------

def test_regular_final_score_sets_winner_outcome_and_status(client, world):
    world.team('A'); world.team('B')
    m = world.match('A', 'B', status='on-going')

    r = client.post('/update-score', json={'match_id': m.id, 'tournament_id': world.tid,
                                           'score': '11-7', 'final': True})
    assert r.status_code == 200
    body = r.get_json()
    # legacy keys unchanged
    assert body['message'] == 'Scores updated successfully'
    assert (body['team1_score'], body['team2_score'], body['is_final']) == (11, 7, True)

    m = reload_match(m.id)
    assert (m.winner_team_id, m.outcome_type, m.status, m.is_final) == ('A', 'regular', 'completed', True)
    assert body['outcome_type'] == 'regular' and body['winner_team_id'] == 'A'

    got = client.get('/score/match', query_string={'match_id': m.id, 'tournament_id': world.tid}).get_json()
    assert got['outcome_type'] == 'regular'
    assert got['team1']['score'] == 11 and got['team2']['score'] == 7
    assert got['winner_team_id'] == 'A' and got['is_final'] is True


def test_non_final_update_keeps_outcome_empty(client, world):
    world.team('A'); world.team('B')
    m = world.match('A', 'B', status='on-going')
    r = client.post('/update-score', json={'match_id': m.id, 'tournament_id': world.tid, 'score': '3-2'})
    assert r.status_code == 200
    m = reload_match(m.id)
    assert (m.outcome_type, m.winner_team_id, m.is_final) == (None, None, False)


def test_regular_draw_final_keeps_legacy_behaviour(client, world):
    world.team('A'); world.team('B')
    m = world.match('A', 'B', status='on-going')
    r = client.post('/update-score', json={'match_id': m.id, 'tournament_id': world.tid,
                                           'score': '5-5', 'final': True})
    assert r.status_code == 200
    m = reload_match(m.id)
    assert m.winner_team_id is None and m.is_final is True


def test_regular_final_can_still_be_overridden(client, world):
    """Pre-existing behaviour: regular results stay correctable through update-score."""
    world.team('A'); world.team('B')
    m = world.match('A', 'B', status='on-going')
    for score in ('11-7', '9-11'):
        assert client.post('/update-score', json={'match_id': m.id, 'tournament_id': world.tid,
                                                  'score': score, 'final': True}).status_code == 200
    assert reload_match(m.id).winner_team_id == 'B'


def test_update_score_bad_format_and_missing_fields_unchanged(client, world):
    assert client.post('/update-score', json={'match_id': 1}).status_code == 400
    r = client.post('/update-score', json={'match_id': 1, 'tournament_id': 1, 'score': 'abc'})
    assert r.status_code == 400
    assert client.post('/update-score', json={'match_id': 999, 'tournament_id': 1, 'score': '1-0'}).status_code == 404


# --- Tests 2, 3: walkover for either team ------------------------------------

@pytest.mark.parametrize('winner', ['A', 'B'])
def test_walkover_for_either_team(client, world, emitted, winner):
    world.team('A'); world.team('B')
    m = world.match('A', 'B')

    r = walkover(client, m.id, world.tid, winner)
    assert r.status_code == 200
    assert r.get_json() == {
        'match_id': m.id, 'tournament_id': world.tid, 'status': 'completed', 'is_final': True,
        'outcome_type': 'walkover', 'winner_team_id': winner, 'message': 'Walkover recorded successfully',
    }
    m = reload_match(m.id)
    assert (m.outcome_type, m.winner_team_id, m.status, m.is_final) == ('walkover', winner, 'completed', True)
    # no artificial competitive score
    assert [s.score for s in score_rows(m.id)] == [0, 0]


def test_walkover_allowed_from_ongoing(client, world):
    world.team('A'); world.team('B')
    m = world.match('A', 'B', status='on-going')
    assert walkover(client, m.id, world.tid, 'B').status_code == 200


def test_walkover_creates_missing_zero_score_rows_only(client, world):
    world.team('A'); world.team('B')
    m = world.match('A', 'B', with_scores=False)
    assert walkover(client, m.id, world.tid, 'A').status_code == 200
    assert sorted((s.team_id, s.score) for s in score_rows(m.id)) == [('A', 0), ('B', 0)]


# --- Test 4: invalid winner ---------------------------------------------------

def test_invalid_winner_rejected_without_mutation(client, world):
    world.team('A'); world.team('B'); world.team('C')
    m = world.match('A', 'B')
    r = walkover(client, m.id, world.tid, 'C')
    assert r.status_code == 400 and r.get_json()['code'] == 'invalid_winner'
    m = reload_match(m.id)
    assert (m.outcome_type, m.winner_team_id, m.status, m.is_final) == (None, None, 'pending', False)


@pytest.mark.parametrize('winner', [None, '', '   ', 7])
def test_missing_or_malformed_winner_is_400(client, world, winner):
    world.team('A'); world.team('B')
    m = world.match('A', 'B')
    body = {'tournament_id': world.tid, 'outcome_type': 'walkover'}
    if winner is not None:
        body['winner_team_id'] = winner
    assert client.post(outcome_url(m.id), json=body).status_code == 400
    assert reload_match(m.id).status == 'pending'


# --- Test 5: missing match / tournament --------------------------------------

def test_missing_match_is_404(client, world):
    r = walkover(client, 9999, world.tid, 'A')
    assert r.status_code == 404 and r.get_json()['code'] == 'match_not_found'


def test_missing_tournament_is_404(client, world):
    world.team('A'); world.team('B')
    m = world.match('A', 'B')
    r = walkover(client, m.id, 4242, 'A')
    assert r.status_code == 404 and r.get_json()['code'] == 'tournament_not_found'


def test_match_from_other_tournament_is_404(client, world):
    from models import Tournament
    world.team('A'); world.team('B')
    m = world.match('A', 'B')
    other = Tournament(tournament_name='Other', type='elimination', season_id=world.tournament.season_id)
    db.session.add(other); db.session.commit()
    r = walkover(client, m.id, other.id, 'A')
    assert r.status_code == 404 and r.get_json()['code'] == 'match_not_in_tournament'
    assert reload_match(m.id).status == 'pending'


# --- Test 6: invalid outcome / request ---------------------------------------

@pytest.mark.parametrize('outcome', [None, '', 'forfeit', 'bye', 'regular', 'WALKOVER', 5])
def test_invalid_outcome_type_is_400(client, world, outcome):
    world.team('A'); world.team('B')
    m = world.match('A', 'B')
    body = {'tournament_id': world.tid, 'winner_team_id': 'A'}
    if outcome is not None:
        body['outcome_type'] = outcome
    r = client.post(outcome_url(m.id), json=body)
    assert r.status_code == 400 and r.get_json()['code'] == 'invalid_outcome_type'
    assert reload_match(m.id).outcome_type is None


@pytest.mark.parametrize('tid', [None, 'abc', True, [1]])
def test_invalid_tournament_id_is_400(client, world, tid):
    world.team('A'); world.team('B')
    m = world.match('A', 'B')
    body = {'outcome_type': 'walkover', 'winner_team_id': 'A'}
    if tid is not None:
        body['tournament_id'] = tid
    assert client.post(outcome_url(m.id), json=body).status_code == 400


def test_non_json_body_is_400(client, world):
    r = client.post('/matches/1/outcome', data='not json', content_type='text/plain')
    assert r.status_code == 400
    r = client.post('/matches/1/outcome', json=['x'])
    assert r.status_code == 400


# --- Test 7: completed / ineligible matches ----------------------------------

def test_completed_regular_match_conflicts(client, world):
    world.team('A'); world.team('B')
    m = world.match('A', 'B', status='on-going')
    client.post('/update-score', json={'match_id': m.id, 'tournament_id': world.tid, 'score': '11-7', 'final': True})
    r = walkover(client, m.id, world.tid, 'B')
    assert r.status_code == 409 and r.get_json()['code'] == 'already_completed'
    m = reload_match(m.id)
    assert (m.outcome_type, m.winner_team_id) == ('regular', 'A')


def test_walkover_with_different_winner_conflicts(client, world):
    world.team('A'); world.team('B')
    m = world.match('A', 'B')
    assert walkover(client, m.id, world.tid, 'A').status_code == 200
    r = walkover(client, m.id, world.tid, 'B')
    assert r.status_code == 409
    assert reload_match(m.id).winner_team_id == 'A'


def test_walkover_rejected_when_scores_already_recorded(client, world):
    world.team('A'); world.team('B')
    m = world.match('A', 'B', status='on-going', scores=(4, 2))
    r = walkover(client, m.id, world.tid, 'A')
    assert r.status_code == 409 and r.get_json()['code'] == 'scores_recorded'
    m = reload_match(m.id)
    assert (m.outcome_type, m.status) == (None, 'on-going')
    assert sorted(s.score for s in score_rows(m.id)) == [2, 4]


@pytest.mark.parametrize('t1,t2', [('A', None), (None, 'B'), ('A', 'TBD'), ('TBD', 'TBD')])
def test_teams_must_be_assigned(client, world, t1, t2):
    world.team('A'); world.team('B')
    m = world.match(t1, t2)
    r = walkover(client, m.id, world.tid, 'A')
    assert r.status_code == 409 and r.get_json()['code'] == 'teams_not_assigned'
    assert reload_match(m.id).outcome_type is None


# --- Test 8: repeated submission ---------------------------------------------

def test_repeated_identical_walkover_is_idempotent(client, world, emitted):
    sf1, sf2, final = world.bracket(['A', 'B', 'C', 'D'])
    first = walkover(client, sf1.id, world.tid, 'B')
    second = walkover(client, sf1.id, world.tid, 'B')
    assert first.status_code == second.status_code == 200
    assert second.get_json()['message'] == 'Walkover was already recorded'
    assert {k: v for k, v in first.get_json().items() if k != 'message'} == \
           {k: v for k, v in second.get_json().items() if k != 'message'}
    assert Match.query.count() == 3
    assert len(score_rows(sf1.id)) == 2
    assert score_rows(final.id) == []          # other slot still TBD: no rows, no duplicates
    assert reload_match(final.id).team1_id == 'B'
    assert len(emitted) == 1                   # the replay did not broadcast again


def test_repeat_after_both_slots_filled_creates_no_duplicate_score_rows(client, world):
    sf1, sf2, final = world.bracket(['A', 'B', 'C', 'D'])
    walkover(client, sf1.id, world.tid, 'A')
    walkover(client, sf2.id, world.tid, 'D')
    walkover(client, sf1.id, world.tid, 'A')
    walkover(client, sf2.id, world.tid, 'D')
    rows = sorted((s.team_id, s.score) for s in score_rows(final.id))
    assert rows == [('A', 0), ('D', 0)]


# --- Test 9: knockout advancement --------------------------------------------

def test_walkover_winner_advances_to_correct_slots(client, world):
    sf1, sf2, final = world.bracket(['A', 'B', 'C', 'D'])

    assert walkover(client, sf2.id, world.tid, 'C').status_code == 200   # predecessor_2 -> team2
    f = reload_match(final.id)
    assert (f.team1_id, f.team2_id) == ('TBD', 'C')
    assert score_rows(final.id) == []

    assert walkover(client, sf1.id, world.tid, 'B').status_code == 200   # predecessor_1 -> team1
    f = reload_match(final.id)
    assert (f.team1_id, f.team2_id) == ('B', 'C')
    assert sorted((s.team_id, s.score) for s in score_rows(final.id)) == [('B', 0), ('C', 0)]


def test_final_round_walkover_has_no_successor_and_completes(client, world):
    sf1, sf2, final = world.bracket(['A', 'B', 'C', 'D'])
    walkover(client, sf1.id, world.tid, 'A'); walkover(client, sf2.id, world.tid, 'C')
    r = walkover(client, final.id, world.tid, 'C')
    assert r.status_code == 200 and r.get_json()['winner_team_id'] == 'C'


def test_walkover_in_semi_then_regular_final_scores_normally(client, world):
    sf1, sf2, final = world.bracket(['A', 'B', 'C', 'D'])
    walkover(client, sf1.id, world.tid, 'A'); walkover(client, sf2.id, world.tid, 'C')
    r = client.post('/update-score', json={'match_id': final.id, 'tournament_id': world.tid,
                                           'score': '11-9', 'final': True})
    assert r.status_code == 200
    f = reload_match(final.id)
    assert (f.winner_team_id, f.outcome_type) == ('A', 'regular')


def test_regular_final_advances_without_duplicating_on_override(client, world):
    sf1, sf2, final = world.bracket(['A', 'B', 'C', 'D'])
    for score in ('11-5', '11-5'):   # finalize twice (override)
        client.post('/update-score', json={'match_id': sf1.id, 'tournament_id': world.tid,
                                           'score': score, 'final': True})
    client.post('/update-score', json={'match_id': sf2.id, 'tournament_id': world.tid,
                                       'score': '4-11', 'final': True})
    assert sorted((s.team_id, s.score) for s in score_rows(final.id)) == [('A', 0), ('D', 0)]


# --- Test 11: transactional integrity ----------------------------------------

def test_failure_during_advancement_rolls_everything_back(client, world, emitted, monkeypatch):
    sf1, sf2, final = world.bracket(['A', 'B', 'C', 'D'])

    def boom(match):
        # half-applied progression, then crash
        succ = db.session.get(Match, match.successor)
        succ.team1_id = match.winner_team_id
        raise RuntimeError('simulated database failure /secret/path')

    monkeypatch.setattr(outcomes, 'advance_winner', boom)
    r = walkover(client, sf1.id, world.tid, 'A')
    assert r.status_code == 500
    assert r.get_json() == {'error': 'Internal server error', 'code': 'internal_error'}   # no internals leaked
    assert emitted == []

    sf1, final = reload_match(sf1.id), reload_match(final.id)
    assert (sf1.outcome_type, sf1.winner_team_id, sf1.status, sf1.is_final) == (None, None, 'pending', False)
    assert (final.team1_id, final.team2_id) == ('TBD', 'TBD')
    assert score_rows(final.id) == []


def test_inconsistent_bracket_rolls_back_with_409(client, world):
    sf1, sf2, final = world.bracket(['A', 'B', 'C', 'D'])
    final.predecessor_1 = final.predecessor_2 = None
    db.session.commit()
    r = walkover(client, sf1.id, world.tid, 'A')
    assert r.status_code == 409 and r.get_json()['code'] == 'bracket_inconsistent'
    assert reload_match(sf1.id).outcome_type is None


# --- Live updates -------------------------------------------------------------

def test_socket_event_after_commit_without_scores(client, world, emitted):
    world.team('A'); world.team('B')
    m = world.match('A', 'B')
    walkover(client, m.id, world.tid, 'B')
    assert len(emitted) == 1
    event, payload, kw = emitted[0]
    assert event == 'score_update' and kw == {'namespace': '/scores'}
    assert payload['match_id'] == m.id and payload['outcome_type'] == 'walkover'
    assert payload['winner_team_id'] == 'B' and payload['status'] == 'completed' and payload['is_final'] is True
    assert 'team1_score' not in payload and 'team2_score' not in payload


def test_no_event_for_rejected_requests(client, world, emitted):
    world.team('A'); world.team('B')
    m = world.match('A', 'B')
    walkover(client, m.id, world.tid, 'Z')
    walkover(client, 999, world.tid, 'A')
    assert emitted == []


def test_regular_final_event_still_carries_scores(client, world, emitted):
    world.team('A'); world.team('B')
    m = world.match('A', 'B', status='on-going')
    client.post('/update-score', json={'match_id': m.id, 'tournament_id': world.tid, 'score': '11-7', 'final': True})
    _, payload, _ = emitted[-1]
    assert (payload['team1_score'], payload['team2_score'], payload['outcome_type']) == (11, 7, 'regular')


# --- Completed-walkover protection (no second completion path) ----------------

def test_score_update_on_walkover_match_is_rejected(client, world):
    world.team('A'); world.team('B')
    m = world.match('A', 'B')
    walkover(client, m.id, world.tid, 'A')
    for final in (False, True):
        r = client.post('/update-score', json={'match_id': m.id, 'tournament_id': world.tid,
                                               'score': '11-0', 'final': final})
        assert r.status_code == 409 and r.get_json()['code'] == 'outcome_locked'
    assert [s.score for s in score_rows(m.id)] == [0, 0]
    assert reload_match(m.id).winner_team_id == 'A'


def test_status_cannot_reopen_walkover_but_completed_is_idempotent(client, world):
    world.team('A'); world.team('B')
    m = world.match('A', 'B')
    walkover(client, m.id, world.tid, 'A')
    for status in ('pending', 'on-going'):
        r = client.post(f'/update-match-status/{m.id}', json={'status': status, 'tournament_id': world.tid})
        assert r.status_code == 409
    r = client.post(f'/update-match-status/{m.id}', json={'status': 'completed', 'tournament_id': world.tid})
    assert r.status_code == 200 and r.get_json()['match']['outcome_type'] == 'walkover'
    m = reload_match(m.id)
    assert (m.status, m.winner_team_id, m.outcome_type) == ('completed', 'A', 'walkover')


def test_status_endpoint_unchanged_for_regular_matches(client, world):
    world.team('A'); world.team('B')
    m = world.match('A', 'B')
    r = client.post(f'/update-match-status/{m.id}', json={'status': 'on-going', 'tournament_id': world.tid})
    assert r.status_code == 200 and reload_match(m.id).status == 'on-going'
    r = client.post(f'/update-match-status/{m.id}', json={'status': 'completed', 'tournament_id': world.tid})
    assert r.status_code == 200 and reload_match(m.id).is_final is True


def test_get_match_score_exposes_walkover(client, world):
    world.team('A'); world.team('B')
    m = world.match('A', 'B')
    walkover(client, m.id, world.tid, 'B')
    got = client.get('/score/match', query_string={'match_id': m.id, 'tournament_id': world.tid}).get_json()
    assert got['outcome_type'] == 'walkover' and got['winner_team_id'] == 'B' and got['status'] == 'completed'
    assert got['team1']['score'] == 0 and got['team2']['score'] == 0   # legacy keys preserved
