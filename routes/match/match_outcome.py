from flask import request, jsonify, current_app
from . import match_bp
from services.match_outcomes import (
    MatchOutcomeError, declare_outcome, serialize_outcome, publish_result_update, OUTCOMES,
)


@match_bp.route('/matches/<int:match_id>/outcome', methods=['POST'])
def declare_match_outcome(match_id):
    """Declare how a match was decided without playing it (currently: walkover).

    Body: {"tournament_id": 1, "outcome_type": "walkover", "winner_team_id": "T001"}

    200 recorded (or an identical earlier request replayed), 400 invalid request,
    404 tournament/match not found, 409 match state conflict, 500 unexpected.
    The backend has no authentication layer yet, so there is no 403 path.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _error('Request body must be a JSON object', 'invalid_json', 400)

    try:
        declared = declare_outcome(
            match_id=match_id,
            tournament_id=data.get('tournament_id'),
            outcome_type=data.get('outcome_type'),
            winner_team_id=data.get('winner_team_id'),
        )
    except MatchOutcomeError as e:
        return _error(e.message, e.code, e.status_code)
    except Exception:
        current_app.logger.exception('Unexpected error declaring outcome for match %s', match_id)
        return _error('Internal server error', 'internal_error', 500)

    match = declared.match
    label = OUTCOMES[match.outcome_type].label
    response = serialize_outcome(match)
    response['message'] = (f'{label} recorded successfully' if declared.changed
                           else f'{label} was already recorded')

    if declared.changed:
        # Only after the commit inside declare_outcome succeeded. No numeric scores:
        # a walkover has none, and fabricating 0-0 would be misleading.
        publish_result_update({
            **serialize_outcome(match),
            'team1_id': match.team1_id,
            'team2_id': match.team2_id,
            'message': response['message'],
        })

    return jsonify(response), 200


def _error(message, code, status):
    return jsonify({'error': message, 'code': code}), status
