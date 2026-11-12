from flask import request, jsonify
from models import Match, Team, Player, Tournament, Score, db, SuperTournament
from sqlalchemy import or_
from services.match_outcomes import MatchOutcomeError, ensure_status_change_allowed
from . import match_bp

@match_bp.route('/create-match', methods=['POST'])
def create_match():
    data = request.json
    
    # Extract data from request
    tournament_id = data.get('tournament_id')
    team1_id = data.get('team1_id')
    team2_id = data.get('team2_id')
    round_id = data.get('round_id')
    pool = data.get('pool')

    # Validate required fields
    if not tournament_id or not team1_id or not team2_id or not round_id or not pool:
        return jsonify({
            'error': 'tournament_id, team1_id, team2_id, round_id, and pool are required',
            'tournament_id': tournament_id,
            'team1_id': team1_id,
            'team2_id': team2_id,
            'round_id': round_id,
            'pool': pool
        }), 400

    # Check if the tournament exists
    tournament = Tournament.query.filter_by(id=tournament_id).first()
    if not tournament:
        return jsonify({'error': 'Tournament not found'}), 404

    # Check if teams exist under this tournament
    team1 = Team.query.filter_by(team_id=team1_id, tournament_id=tournament_id).first()
    team2 = Team.query.filter_by(team_id=team2_id, tournament_id=tournament_id).first()

    if not team1 or not team2:
        return jsonify({'error': 'One or both teams not found in this tournament'}), 404

    try:
        # Create the match name
        match_name = f"Round {round_id} - {team1.name} vs {team2.name}"

        # Create a new match entry associated with the tournament
        match = Match(
            round_id=round_id,
            pool=pool,
            team1_id=team1_id,
            team2_id=team2_id,
            match_name=match_name,
            tournament_id=tournament_id
        )

        # Add the match and scores to the database
        db.session.add(match)
        db.session.flush()  # Get match ID before committing

        # Initialize the scores for both teams
        score1 = Score(
            match_id=match.id,
            team_id=team1_id,
            score=0,
            tournament_id=tournament_id
        )
        score2 = Score(
            match_id=match.id,
            team_id=team2_id,
            score=0,
            tournament_id=tournament_id
        )

        db.session.add(score1)
        db.session.add(score2)
        db.session.commit()

        return jsonify({
            'message': 'Match created successfully',
            'match_id': match.id,
            'match_name': match_name
        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@match_bp.route('/check-player-checkins/<int:match_id>', methods=['GET'])
def check_player_checkins(match_id):
    try:
        # Get the match
        match = Match.query.get(match_id)
        if not match:
            return jsonify({'error': 'Match not found'}), 404

        # Get both teams
        team1 = Team.query.get(match.team1_id)
        team2 = Team.query.get(match.team2_id)

        if not team1 or not team2:
            return jsonify({'error': 'One or both teams not found'}), 404

        # Get all players from both teams using UUIDs
        team1_players = []
        team2_players = []
        
        if team1.player1_uuid:
            player = Player.query.filter_by(uuid=team1.player1_uuid).first()
            if player:
                team1_players.append(player)
        if team1.player2_uuid:
            player = Player.query.filter_by(uuid=team1.player2_uuid).first()
            if player:
                team1_players.append(player)
                
        if team2.player1_uuid:
            player = Player.query.filter_by(uuid=team2.player1_uuid).first()
            if player:
                team2_players.append(player)
        if team2.player2_uuid:
            player = Player.query.filter_by(uuid=team2.player2_uuid).first()
            if player:
                team2_players.append(player)

        # Check if all players are checked in
        all_checked_in = True
        unchecked_players = []

        for player in team1_players + team2_players:
            if not player.checked_in:
                all_checked_in = False
                unchecked_players.append({
                    'name': f"{player.first_name} {player.last_name}".strip(),
                    'team_id': team1.team_id if player.uuid in [team1.player1_uuid, team1.player2_uuid] else team2.team_id
                })

        # Update the match's all_players_checked_in status
        match.all_players_checked_in = all_checked_in
        db.session.commit()

        return jsonify({
            'match_id': match_id,
            'all_players_checked_in': all_checked_in,
            'unchecked_players': unchecked_players
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@match_bp.route('/update-checkin-status/<int:match_id>', methods=['POST'])
def update_checkin_status(match_id):
    try:
        # Get the match
        match = Match.query.get(match_id)
        if not match:
            return jsonify({'error': 'Match not found'}), 404

        data = request.json
        player_id = data.get('player_id')
        checked_in = data.get('checked_in', True)

        if not player_id:
            return jsonify({'error': 'player_id is required'}), 400

        # Update player's check-in status
        player = Player.query.get(player_id)
        if not player:
            return jsonify({'error': 'Player not found'}), 404

        # Verify player belongs to one of the teams in the match
        if player.team_id not in [match.team1_id, match.team2_id]:
            return jsonify({'error': 'Player is not part of this match'}), 400

        player.checked_in = checked_in
        db.session.commit()

        # Check if all players are now checked in
        return check_player_checkins(match_id)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@match_bp.route('/assign-pool', methods=['POST'])
def assign_pool():
    data = request.json
    match_id = data.get('match_id')
    pool = data.get('pool')
    tournament_id = data.get('tournament_id')
    
    if not all([match_id, pool, tournament_id]):
        return jsonify({'error': 'match_id, pool, and tournament_id are required'}), 400
    
    # Check if match exists
    match = Match.query.filter_by(id=match_id, tournament_id=tournament_id).first()
    if not match:
        return jsonify({'error': 'Match not found'}), 404
    
    try:
        # Update pool assignment
        match.pool = pool
        db.session.commit()
        
        return jsonify({
            'message': f'Pool {pool} assigned to match {match.match_name}',
            'match': {
                'id': match.id,
                'match_name': match.match_name,
                'court_number': match.court_number,
                'pool': match.pool,
                'team1_id': match.team1_id,
                'team2_id': match.team2_id
            }
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@match_bp.route('/assign-court-pool', methods=['POST'])
def assign_court_and_pool():
    data = request.json
    match_id = data.get('match_id')
    court_number = data.get('court_number')
    pool = data.get('pool')
    tournament_id = data.get('tournament_id')
    
    if not all([match_id, court_number, pool, tournament_id]):
        return jsonify({'error': 'match_id, court_number, pool, and tournament_id are required'}), 400
    
    # Check if match exists
    match = Match.query.filter_by(id=match_id, tournament_id=tournament_id).first()
    if not match:
        return jsonify({'error': 'Match not found'}), 404
    
    try:
        # Update both court and pool assignments
        match.court_number = court_number
        match.pool = pool
        db.session.commit()
        
        return jsonify({
            'message': f'Court {court_number} and Pool {pool} assigned to match {match.match_name}',
            'match': {
                'id': match.id,
                'match_name': match.match_name,
                'court_number': match.court_number,
                'pool': match.pool,
                'team1_id': match.team1_id,
                'team2_id': match.team2_id
            }
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@match_bp.route('/update-match-status/<int:match_id>', methods=['POST'])
def update_match_status(match_id):
    try:
        data = request.json
        new_status = data.get('status')
        tournament_id = data.get('tournament_id')

        if not new_status or not tournament_id:
            return jsonify({'error': 'status and tournament_id are required'}), 400

        # Validate status value
        valid_statuses = ['pending', 'on-going', 'completed']
        if new_status not in valid_statuses:
            return jsonify({'error': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}), 400

        # Get the match
        match = Match.query.filter_by(id=match_id, tournament_id=tournament_id).first()
        if not match:
            return jsonify({'error': 'Match not found'}), 404

        # A match decided without scores (walkover...) stays completed
        ensure_status_change_allowed(match, new_status)

        # Update match status
        match.status = new_status

        # If status is completed, ensure the match is marked as final
        if new_status == 'completed':
            match.is_final = True

        db.session.commit()

        return jsonify({
            'message': 'Match status updated successfully',
            'match': {
                'id': match.id,
                'match_name': match.match_name,
                'status': match.status,
                'is_final': match.is_final,
                'outcome_type': match.outcome_type,
                'court_number': match.court_number,
                'pool': match.pool,
                'team1_id': match.team1_id,
                'team2_id': match.team2_id
            }
        }), 200

    except MatchOutcomeError as e:
        db.session.rollback()
        return jsonify({'error': e.message, 'code': e.code}), e.status_code

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

def verify_player_checkins(match):
    """Helper function to verify all players are checked in"""
    team1 = Team.query.get(match.team1_id)
    team2 = Team.query.get(match.team2_id)
    
    if not (team1 and team2):
        return False, "One or both teams not found"
        
    if not (team1.checked_in and team2.checked_in):
        return False, "Cannot assign court - all players must be checked in"
        
    return True, None

@match_bp.route('/player-checkin', methods=['POST'])
def player_checkin():
    try:
        data = request.json
        player_id = data.get('player_id')
        tournament_id = data.get('tournament_id')
        checked_in = data.get('checked_in', True)

        if not all([player_id, tournament_id]):
            return jsonify({'error': 'player_id and tournament_id are required'}), 400

        # Get the player
        player = Player.query.get(player_id)
        if not player:
            return jsonify({'error': 'Player not found'}), 404
            
        # Get the tournament and verify it's part of player's super tournament
        tournament = Tournament.query.get(tournament_id)
        if not tournament or not tournament.season or tournament.season.super_tournament_id != player.super_tournament_id:
            return jsonify({'error': 'Tournament not found or not part of player\'s super tournament'}), 404

        # Update player's check-in status for the entire super tournament
        player.checked_in = checked_in
        
        # Get all tournaments in this super tournament through seasons
        super_tournament = SuperTournament.query.get(player.super_tournament_id)
        tournaments = []
        for season in super_tournament.seasons:
            tournaments.extend(season.tournaments)
            
        # Update team statuses in all tournaments
        updated_teams = []
        for tournament in tournaments:
            # Find team in this tournament where player is either player1 or player2
            team = Team.query.filter(
                (Team.player1_uuid == player.uuid) | (Team.player2_uuid == player.uuid),
                Team.tournament_id == tournament.id
            ).first()
            
            if team:
                # Get the other player in the team
                other_player = None
                if team.player1_uuid == player.uuid and team.player2_uuid:
                    other_player = Player.query.filter_by(uuid=team.player2_uuid).first()
                elif team.player2_uuid == player.uuid and team.player1_uuid:
                    other_player = Player.query.filter_by(uuid=team.player1_uuid).first()
                
                # Check if all players in the team are checked in
                all_players_checked_in = player.checked_in and (not other_player or other_player.checked_in)
                
                # Update team check-in status
                team.checked_in = all_players_checked_in
                
                updated_teams.append({
                    'tournament_id': tournament.id,
                    'tournament_name': tournament.tournament_name,
                    'team': {
                        'team_id': team.team_id,
                        'name': team.name,
                        'checked_in': team.checked_in,
                        'all_players_checked_in': all_players_checked_in
                    }
                })
        
        db.session.commit()

        return jsonify({
            'message': 'Player check-in status updated successfully',
            'player': {
                'id': player.id,
                'name': f"{player.first_name} {player.last_name}",
                'checked_in': player.checked_in
            },
            'super_tournament': {
                'id': super_tournament.id,
                'name': super_tournament.name
            },
            'updated_teams': updated_teams
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500