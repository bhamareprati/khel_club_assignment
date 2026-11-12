from flask import request, jsonify
from flask_socketio import emit
from models import Score, Match, db, Player, Team
from . import score_bp
from socket_instance import socketio
from flask_cors import cross_origin
from services.match_outcomes import (
    MatchOutcomeError, ensure_scores_editable, finalize_regular_match, publish_result_update,
)

# NOTE: update_successor_match() used to live here. It committed internally and
# re-created successor Score rows on every call. Knockout progression now lives
# in services.match_outcomes.advance_winner(): no internal commit (the caller
# owns the transaction) and no duplicate Score rows.

@score_bp.route('/update-score', methods=['POST'])
def update_score():
    print("\n=== Starting update_score endpoint ===")
    data = request.get_json()
    print(f"Received request data: {data}")
    
    match_id = data.get('match_id')
    score_input = data.get('score')
    tournament_id = data.get('tournament_id')
    final = data.get('final', False)
    override = data.get('override', False)
    
    print(f"Processing update for match_id={match_id}, score={score_input}, final={final}")

    if not all(k in data for k in ['match_id', 'score', 'tournament_id']):
        return jsonify({"error": "match_id, score, and tournament_id are required"}), 400

    try:
        team1_score, team2_score = map(int, score_input.split('-'))
        print(f"Parsed scores - team1: {team1_score}, team2: {team2_score}")
    except ValueError:
        print("Error: Invalid score format")
        return jsonify({'error': 'Score format is invalid. Use "{teamA score}-{teamB score}"'}), 400

    try:
        match = Match.query.filter_by(id=match_id).first()
        if not match:
            print(f"Match not found with id: {match_id}")
            return jsonify({'error': 'Match not found'}), 404

        print(f"Found match with team1_id={match.team1_id}, team2_id={match.team2_id}")

        # A match decided without scores (e.g. walkover) cannot be scored
        ensure_scores_editable(match)

        # First, check if scores exist, if not create them
        team1_score_record = Score.query.filter_by(
            match_id=match_id,
            team_id=match.team1_id,
            tournament_id=tournament_id
        ).first()
        
        team2_score_record = Score.query.filter_by(
            match_id=match_id,
            team_id=match.team2_id,
            tournament_id=tournament_id
        ).first()

        # Create or update score records
        if not team1_score_record:
            team1_score_record = Score(
                match_id=match_id,
                team_id=match.team1_id,
                tournament_id=tournament_id,
                score=team1_score
            )
            db.session.add(team1_score_record)
        else:
            team1_score_record.score = team1_score

        if not team2_score_record:
            team2_score_record = Score(
                match_id=match_id,
                team_id=match.team2_id,
                tournament_id=tournament_id,
                score=team2_score
            )
            db.session.add(team2_score_record)
        else:
            team2_score_record.score = team2_score

        # Finalize (winner, outcome 'regular', status 'completed', knockout
        # progression) through the shared outcome service. Nothing commits until below.
        if final:
            finalize_regular_match(match, team1_score, team2_score)
            print(f"Match finalized with winner_team_id: {match.winner_team_id}")

        # Commit all changes (single commit: scores + result + successor progression)
        db.session.commit()
        print("Updates committed to database")

        response = {
            'message': 'Scores updated successfully',
            'match_id': match_id,
            'team1_id': match.team1_id,
            'team1_score': team1_score,
            'team2_id': match.team2_id,
            'team2_score': team2_score,
            'is_final': match.is_final,
            'tournament_id': tournament_id,
            # additive fields (existing keys unchanged)
            'status': match.status,
            'outcome_type': match.outcome_type,
            'winner_team_id': match.winner_team_id
        }

        # Emit WebSocket event with the updated scores (after the commit)
        publish_result_update(response)

        print(f"\nSending response: {response}")
        return jsonify(response), 200

    except MatchOutcomeError as e:
        db.session.rollback()
        return jsonify({'error': e.message, 'code': e.code}), e.status_code

    except Exception as e:
        print(f"\n=== Error occurred ===")
        print(f"Error message: {str(e)}")
        print(f"Error type: {type(e)}")
        import traceback
        print(f"Full traceback:")
        print(traceback.format_exc())
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@score_bp.route('/team-score', methods=['GET'])
def get_team_score():
    team_id = request.args.get('team_id')
    if not team_id:
        return jsonify({"error": "Team ID is required"}), 400
        
    scores = Score.query.filter_by(team_id=team_id).all()
    score_data = [{
        'match_id': score.match_id,
        'score': score.score
    } for score in scores]
    
    return jsonify(score_data), 200

@score_bp.route('/score/match', methods=['GET', 'OPTIONS'])
@cross_origin()
def get_match_score():
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        print("\n=== Starting get_match_score endpoint ===")
        match_id = request.args.get('match_id')
        tournament_id = request.args.get('tournament_id')
        
        if not match_id or not tournament_id:
            return jsonify({
                "error": "match_id and tournament_id are required"
            }), 400
            
        print(f"Getting score for match_id={match_id}")
        
        # Get match details
        print("\n=== Querying Match table ===")
        match = Match.query.filter_by(id=match_id).first()
        
        if not match:
            return jsonify({
                "error": f"Match with ID {match_id} not found"
            }), 404
            
        print(f"Found match:\n- Match ID: {match.id}\n- Team1 ID: {match.team1_id}\n- Team2 ID: {match.team2_id}\n- Is Final: {match.is_final}\n- Status: {match.status}")
        
        # Get teams
        team1 = Team.query.filter_by(team_id=match.team1_id).first()
        team2 = Team.query.filter_by(team_id=match.team2_id).first()
        
        if not team1 or not team2:
            return jsonify({
                "error": "One or both teams not found"
            }), 404

        # Get players using the new UUID relationships
        players_team1 = []
        if team1.player1:
            players_team1.append({
                'id': team1.player1.id,
                'uuid': team1.player1.uuid,
                'first_name': team1.player1.first_name,
                'last_name': team1.player1.last_name,
                'checked_in': team1.player1.checked_in
            })
        if team1.player2:
            players_team1.append({
                'id': team1.player2.id,
                'uuid': team1.player2.uuid,
                'first_name': team1.player2.first_name,
                'last_name': team1.player2.last_name,
                'checked_in': team1.player2.checked_in
            })

        players_team2 = []
        if team2.player1:
            players_team2.append({
                'id': team2.player1.id,
                'uuid': team2.player1.uuid,
                'first_name': team2.player1.first_name,
                'last_name': team2.player1.last_name,
                'checked_in': team2.player1.checked_in
            })
        if team2.player2:
            players_team2.append({
                'id': team2.player2.id,
                'uuid': team2.player2.uuid,
                'first_name': team2.player2.first_name,
                'last_name': team2.player2.last_name,
                'checked_in': team2.player2.checked_in
            })
            
        # Get scores
        scores = Score.query.filter_by(match_id=match_id).all()
        
        team1_score = 0
        team2_score = 0
        
        for score in scores:
            if score.team_id == match.team1_id:
                team1_score = score.score
            elif score.team_id == match.team2_id:
                team2_score = score.score
                
        response = {
            'match_id': match.id,
            'tournament_id': tournament_id,
            'team1': {
                'team_id': team1.team_id,
                'name': team1.name,
                'players': players_team1,
                'score': team1_score
            },
            'team2': {
                'team_id': team2.team_id,
                'name': team2.name,
                'players': players_team2,
                'score': team2_score
            },
            'status': match.status,
            'is_final': match.is_final,
            'winner_team_id': match.winner_team_id,
            'outcome_type': match.outcome_type
        }

        return jsonify(response), 200
        
    except Exception as e:
        print("\n=== Error occurred ===")
        print(f"Error message: {str(e)}")
        print(f"Error type: {type(e)}")
        print("Full traceback:")
        import traceback
        traceback.print_exc()
        
        return jsonify({
            "error": str(e)
        }), 500

@score_bp.route('/score', methods=['GET'])
def get_scores():
    tournament_id = request.args.get('tournament_id')
    if not tournament_id:
        return jsonify({"error": "Tournament ID is required"}), 400
        
    # Get all matches for the tournament
    matches = Match.query.filter_by(tournament_id=tournament_id).all()
    match_ids = [match.id for match in matches]
    
    # Get scores for these matches
    scores = Score.query.filter(Score.match_id.in_(match_ids)).all()
    score_data = [{
        'match_id': score.match_id,
        'team_id': score.team_id,
        'score': score.score
    } for score in scores]
    
    return jsonify(score_data), 200 