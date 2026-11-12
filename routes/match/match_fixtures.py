from flask import request, jsonify, Response
from models import Match, Team, Player, Score, Tournament, db, Round
from sqlalchemy.orm import aliased
import csv
import io
from . import match_bp
from sqlalchemy import cast
from services.match_outcomes import result_label, uses_scores

@match_bp.route('/get-match-fixtures', methods=['GET'])
def get_match_fixtures():
    tournament_id = request.args.get('tournament_id')
    pool = request.args.get('pool')
    
    if not tournament_id:
        return jsonify({'error': 'tournament_id is required'}), 400

    try:
        # Create aliases for Team table
        Team1 = aliased(Team)
        Team2 = aliased(Team)
        
        # Create aliases for predecessor matches
        PredMatch1 = aliased(Match)
        PredMatch2 = aliased(Match)
        SuccMatch = aliased(Match)

        # Build base query with all necessary joins
        base_query = db.session.query(
            Match,
            Team1.team_id.label('team1_id'),
            Team1.name.label('team1_name'),
            Team1.checked_in.label('team1_checked_in'),
            Team1.player1_uuid.label('team1_player1_uuid'),
            Team1.player2_uuid.label('team1_player2_uuid'),
            Team2.team_id.label('team2_id'),
            Team2.name.label('team2_name'),
            Team2.checked_in.label('team2_checked_in'),
            Team2.player1_uuid.label('team2_player1_uuid'),
            Team2.player2_uuid.label('team2_player2_uuid'),
            Round.name.label('round_name'),
            # Add predecessor and successor match names
            PredMatch1.match_name.label('predecessor_1_name'),
            PredMatch2.match_name.label('predecessor_2_name'),
            SuccMatch.match_name.label('successor_name')
        ).distinct(
            Match.id  # Ensure each match appears only once
        ).outerjoin(
            Team1, Match.team1_id == Team1.team_id
        ).outerjoin(
            Team2, Match.team2_id == Team2.team_id
        ).outerjoin(
            Round, (cast(Match.round_id, db.Integer) == Round.round_id) & 
                  (Match.tournament_id == Round.tournament_id)
        ).outerjoin(
            PredMatch1, Match.predecessor_1 == PredMatch1.id
        ).outerjoin(
            PredMatch2, Match.predecessor_2 == PredMatch2.id
        ).outerjoin(
            SuccMatch, Match.successor == SuccMatch.id
        ).filter(
            Match.tournament_id == tournament_id
        ).order_by(
            Match.id  # Ensure consistent ordering with distinct
        )

        if pool:
            base_query = base_query.filter(Match.pool == pool)

        # Execute the query
        matches_data = base_query.all()
        
        # Create a set to track processed match IDs
        processed_match_ids = set()
        
        if not matches_data:
            return jsonify({'error': 'No matches found'}), 404

        # Get all match IDs (ensuring uniqueness) - keep as integers
        match_ids = list(set(match[0].id for match in matches_data))

        # Fetch all scores for these matches in one query
        scores = db.session.query(
            Score.match_id,
            Score.team_id,
            Score.score,
            Score.tournament_id
        ).filter(
            Score.match_id.in_(match_ids),
            Score.tournament_id == tournament_id
        ).all()

        # Create a dictionary for quick score lookup
        score_lookup = {}
        for score in scores:
            if score.match_id not in score_lookup:
                score_lookup[score.match_id] = {}
            score_lookup[score.match_id][score.team_id] = score.score

        # Get all player UUIDs
        all_player_uuids = set()
        for match_data in matches_data:
            for uuid in [
                match_data.team1_player1_uuid,
                match_data.team1_player2_uuid,
                match_data.team2_player1_uuid,
                match_data.team2_player2_uuid
            ]:
                if uuid:
                    all_player_uuids.add(uuid)

        # Fetch all players in one query and create lookup
        players = {
            p.uuid: f"{p.first_name} {p.last_name}".strip()
            for p in Player.query.filter(Player.uuid.in_(all_player_uuids)).all()
        } if all_player_uuids else {}

        # Build response
        match_fixtures = []
        for match_data in matches_data:
            match = match_data[0]  # Get the Match object
            match_id = match.id  # Keep as integer
            
            # Skip if we've already processed this match
            if match_id in processed_match_ids:
                continue
            processed_match_ids.add(match_id)
            
            # Get scores from lookup
            team1_score = score_lookup.get(match_id, {}).get(match.team1_id, 0)
            team2_score = score_lookup.get(match_id, {}).get(match.team2_id, 0)
            # "a-b" for scored matches, "Walkover" (etc.) for unscored outcomes
            match_result = result_label(match.outcome_type, team1_score, team2_score)

            # Get players from lookup
            team1_players = []
            if match_data.team1_player1_uuid:
                team1_players.append(players.get(match_data.team1_player1_uuid, 'Unknown'))
            if match_data.team1_player2_uuid:
                team1_players.append(players.get(match_data.team1_player2_uuid, 'Unknown'))
            team1_players_str = ", ".join(team1_players)

            team2_players = []
            if match_data.team2_player1_uuid:
                team2_players.append(players.get(match_data.team2_player1_uuid, 'Unknown'))
            if match_data.team2_player2_uuid:
                team2_players.append(players.get(match_data.team2_player2_uuid, 'Unknown'))
            team2_players_str = ", ".join(team2_players)

            fixture = {
                'match_id': match.id,
                'match_name': match.match_name,
                'round_id': match.round_id,
                'round_name': match_data.round_name,
                'pool': match.pool,
                'court_number': match.court_number,
                'court_order': match.court_order,
                'team1': {
                    'team_id': match_data.team1_id or "TBD",
                    'name': match_data.team1_name or "TBD",
                    'checked_in': match_data.team1_checked_in or False
                },
                'team2': {
                    'team_id': match_data.team2_id or "TBD",
                    'name': match_data.team2_name or "TBD",
                    'checked_in': match_data.team2_checked_in or False
                },
                'team1_players': team1_players_str or "TBD",
                'team2_players': team2_players_str or "TBD",
                'match_result': match_result,
                'match_status': {
                    'winner_team_id': match.winner_team_id,
                    'is_final': match.is_final,
                    'status': match.status,
                    'outcome_type': match.outcome_type
                },
                'bracket_info': {
                    'round_number': match.round_number,
                    'bracket_position': match.bracket_position,
                    'predecessor_1': {
                        'id': match.predecessor_1,
                        'name': match_data.predecessor_1_name
                    } if match.predecessor_1 else None,
                    'predecessor_2': {
                        'id': match.predecessor_2,
                        'name': match_data.predecessor_2_name
                    } if match.predecessor_2 else None,
                    'successor': {
                        'id': match.successor,
                        'name': match_data.successor_name
                    } if match.successor else None
                } if match.pool == "knockout" else None
            }
            match_fixtures.append(fixture)

        response = {
            'matches': match_fixtures,
            'total_matches': len(match_fixtures)
        }
        return jsonify(response), 200

    except Exception as e:
        print(f"Error in get_match_fixtures: {str(e)}")
        return jsonify({'error': str(e)}), 500

@match_bp.route('/get-match-fixtures/csv', methods=['GET'])
def get_match_fixtures_csv():
    tournament_id = request.args.get('tournament_id')
    round_id = request.args.get('round_id')

    if not tournament_id:
        return jsonify({'error': 'tournament_id is required'}), 400

    # Check if the tournament exists
    tournament = Tournament.query.filter_by(id=tournament_id).first()
    if not tournament:
        return jsonify({'error': 'Tournament not found'}), 404

    try:
        # If round_id is provided, filter by round_id, otherwise return all rounds for the tournament
        match_query = Match.query.filter_by(tournament_id=tournament_id)
        
        if round_id:
            if not round_id.isdigit():
                return jsonify({'error': 'Round ID must be a number'}), 400
            match_query = match_query.filter_by(round_id=round_id)

        # Fetch all matches that match the query
        matches = match_query.all()

        if not matches:
            return jsonify({'error': 'No matches found for the provided criteria'}), 404

        # Use an in-memory stream for CSV data
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write CSV header
        writer.writerow(['Round ID', 'Pool', 'Match ID', 'Match Name', 'Team 1 ID', 'Team 1 Players', 'Team 2 ID', 'Team 2 Players', 'Result'])

        for match in matches:
            # Get teams
            team1 = Team.query.get(match.team1_id)
            team2 = Team.query.get(match.team2_id)
            
            # Format player names for team1
            team1_players = []
            if team1:
                if team1.player1_uuid:
                    player1 = Player.query.filter_by(uuid=team1.player1_uuid).first()
                    if player1:
                        team1_players.append(f"{player1.first_name} {player1.last_name}".strip())
                if team1.player2_uuid:
                    player2 = Player.query.filter_by(uuid=team1.player2_uuid).first()
                    if player2:
                        team1_players.append(f"{player2.first_name} {player2.last_name}".strip())
            team1_players_str = ', '.join(team1_players) if team1_players else 'N/A'
            
            # Format player names for team2
            team2_players = []
            if team2:
                if team2.player1_uuid:
                    player1 = Player.query.filter_by(uuid=team2.player1_uuid).first()
                    if player1:
                        team2_players.append(f"{player1.first_name} {player1.last_name}".strip())
                if team2.player2_uuid:
                    player2 = Player.query.filter_by(uuid=team2.player2_uuid).first()
                    if player2:
                        team2_players.append(f"{player2.first_name} {player2.last_name}".strip())
            team2_players_str = ', '.join(team2_players) if team2_players else 'N/A'

            # Get match result if the match is final
            match_result = "TBD"
            if match.is_final:
                team1_score = Score.query.filter_by(match_id=match.id, team_id=match.team1_id).first()
                team2_score = Score.query.filter_by(match_id=match.id, team_id=match.team2_id).first()
                if not uses_scores(match.outcome_type):
                    # Walkover etc.: label only (winner is in winner_team_id), never 0-0
                    match_result = result_label(match.outcome_type, 0, 0)
                elif team1_score and team2_score:
                    match_result = f"{team1_score.score}-{team2_score.score}"

            # Write match details to CSV
            writer.writerow([
                match.round_id,
                match.pool,
                match.id,
                match.match_name,
                team1.team_id if team1 else 'Unknown',
                team1_players_str,
                team2.team_id if team2 else 'Unknown',
                team2_players_str,
                match_result
            ])

        output.seek(0)
        return Response(
            output,
            mimetype='text/csv',
            headers={"Content-Disposition": f"attachment;filename=match_fixtures_{tournament_id}.csv"}
        )

    except Exception as e:
        return jsonify({'error': str(e)}), 500 