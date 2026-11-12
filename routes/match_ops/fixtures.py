from flask import request, jsonify
from models import Tournament, Team, Round, Match, Score, db, Player
from . import match_ops_bp
from sqlalchemy.orm import aliased
import logging
from .teams import generate_team_id, generate_phone_number, validate_player_data, generate_uuid, find_existing_player

logger = logging.getLogger(__name__)

def check_player_in_tournament(player_uuid, tournament_id):
    """
    Check if a player is already part of any team in the tournament
    Returns True if player is found in tournament, False otherwise
    """
    return Team.query.filter(
        (Team.player1_uuid == player_uuid) | (Team.player2_uuid == player_uuid),
        Team.tournament_id == tournament_id
    ).first() is not None

# This is the old endpoint for generating fixtures
@match_ops_bp.route('/pools/<pool_name>/fixtures', methods=['POST'])
def generate_pool_fixtures(pool_name):
    """Generate round-robin fixtures for a pool"""
    logger.debug(f"Generating fixtures for pool {pool_name}")
    data = request.json
    tournament_id = data.get('tournament_id')

    if not tournament_id:
        return jsonify({
            'error': 'tournament_id is required'
        }), 400

    # Check if tournament exists
    tournament = Tournament.query.filter_by(id=tournament_id).first()
    if not tournament:
        return jsonify({'error': 'Tournament not found'}), 404

    # Check if pool exists and get teams
    teams_in_pool = Round.query.filter_by(
        tournament_id=tournament_id,
        round_id=1,  # Round Robin is always round 1
        pool=pool_name
    ).all()

    if not teams_in_pool:
        return jsonify({'error': f'Pool {pool_name} not found or has no teams'}), 404

    # Check if fixtures already exist
    existing_fixtures = Match.query.filter_by(
        tournament_id=tournament_id,
        round_id='1',  # Round Robin is always round 1
        pool=pool_name
    ).first()

    if existing_fixtures:
        return jsonify({'error': 'Fixtures already exist for this pool'}), 409

    try:
        # Get all team IDs in the pool
        team_ids = [team.team_id for team in teams_in_pool if team.team_id]
        
        # Get all team details in one query
        teams_dict = {
            team.team_id: team for team in Team.query.filter(
                Team.team_id.in_(team_ids),
                Team.tournament_id == tournament_id
            ).all()
        }

        logger.debug(f"Found {len(team_ids)} teams in pool {pool_name}")

        # Generate round-robin matches
        matches = []
        match_objects = []
        score_objects = []
        pool_matches = set()

        # Generate matchups between teams (round-robin style)
        for i in range(len(team_ids)):
            for j in range(i + 1, len(team_ids)):
                team1_id = team_ids[i]
                team2_id = team_ids[j]

                if team1_id == team2_id:  # Skip if same team
                    continue

                # Create a unique match identifier
                match_key = tuple(sorted([team1_id, team2_id]))
                if match_key in pool_matches:
                    logger.debug(f"Skipping duplicate match between teams {team1_id} and {team2_id}")
                    continue

                pool_matches.add(match_key)

                team1 = teams_dict.get(team1_id)
                team2 = teams_dict.get(team2_id)

                if not team1 or not team2:
                    logger.warning(f"Team not found. Team1: {team1}, Team2: {team2}")
                    continue

                match_name = f"Round Robin Pool {pool_name} - {team1.name} vs {team2.name}"
                logger.debug(f"Creating match: {match_name}")

                # Create match object
                match = Match(
                    round_id='1',  # Round Robin is always round 1
                    pool=pool_name,
                    team1_id=team1_id,
                    team2_id=team2_id,
                    match_name=match_name,
                    tournament_id=tournament_id,
                    status='pending'
                )
                match_objects.append(match)

                # Store match data for response
                matches.append({
                    'pool': pool_name,
                    'team1_id': team1_id,
                    'team1_name': team1.name,
                    'team2_id': team2_id,
                    'team2_name': team2.name,
                    'match_name': match_name
                })

        if not matches:
            return jsonify({'error': 'No valid matches could be generated'}), 400

        logger.debug(f"Generated {len(matches)} matches")

        # Bulk insert all matches
        db.session.bulk_save_objects(match_objects)
        db.session.flush()

        # Create score objects for all matches
        for match in match_objects:
            score_objects.extend([
                Score(
                    match_id=match.id,
                    team_id=match.team1_id,
                    score=0,
                    tournament_id=tournament_id
                ),
                Score(
                    match_id=match.id,
                    team_id=match.team2_id,
                    score=0,
                    tournament_id=tournament_id
                )
            ])

        # Bulk insert all scores
        db.session.bulk_save_objects(score_objects)
        db.session.commit()

        # Update match IDs in the response
        for i, match in enumerate(match_objects):
            matches[i]['match_id'] = match.id

        logger.debug("Successfully committed all fixtures")

        return jsonify({
            'message': 'Fixtures generated successfully',
            'matches': matches,
            'total_matches': len(matches)
        }), 201

    except Exception as e:
        logger.error(f"Error generating fixtures: {str(e)}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@match_ops_bp.route('/pools/<pool_name>/fixtures', methods=['DELETE'])
def clear_pool_fixtures(pool_name):
    """Clear all fixtures for a pool"""
    tournament_id = request.args.get('tournament_id')

    if not tournament_id:
        return jsonify({
            'error': 'tournament_id is required'
        }), 400

    try:
        # Get all matches in the pool
        matches = Match.query.filter_by(
            tournament_id=tournament_id,
            round_id='1',  # Round Robin is always round 1
            pool=pool_name
        ).all()

        if not matches:
            return jsonify({'error': 'No fixtures found for this pool'}), 404

        # Delete scores first
        for match in matches:
            Score.query.filter_by(match_id=match.id).delete()

        # Delete matches
        for match in matches:
            db.session.delete(match)

        db.session.commit()

        return jsonify({
            'message': f'All fixtures cleared for pool {pool_name}'
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@match_ops_bp.route('/pools/<pool_name>/fixtures', methods=['GET'])
def get_pool_fixtures(pool_name):
    """Get all fixtures for a specific pool"""
    tournament_id = request.args.get('tournament_id')

    if not tournament_id:
        return jsonify({
            'error': 'tournament_id is required'
        }), 400

    try:
        # Create aliases for Team table for cleaner joins
        Team1 = aliased(Team)
        Team2 = aliased(Team)

        # Build base query with all necessary joins
        matches = db.session.query(
            Match,
            Team1.team_id.label('team1_id'),
            Team1.name.label('team1_name'),
            Team1.player1_uuid.label('team1_player1_uuid'),
            Team1.player2_uuid.label('team1_player2_uuid'),
            Team2.team_id.label('team2_id'),
            Team2.name.label('team2_name'),
            Team2.player1_uuid.label('team2_player1_uuid'),
            Team2.player2_uuid.label('team2_player2_uuid')
        ).outerjoin(
            Team1, Match.team1_id == Team1.team_id
        ).outerjoin(
            Team2, Match.team2_id == Team2.team_id
        ).filter(
            Match.tournament_id == tournament_id,
            Match.pool == pool_name
        ).all()

        if not matches:
            return jsonify({'error': 'No matches found in this pool'}), 404

        # Get all match IDs (keep as integers)
        match_ids = [match[0].id for match in matches]

        # Fetch all scores for these matches in one query
        scores = db.session.query(Score).filter(
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
        player_uuids = set()
        for match_data in matches:
            for uuid in [
                match_data.team1_player1_uuid,
                match_data.team1_player2_uuid,
                match_data.team2_player1_uuid,
                match_data.team2_player2_uuid
            ]:
                if uuid:
                    player_uuids.add(uuid)

        # Fetch all players in one query
        players = {
            p.uuid: f"{p.first_name} {p.last_name}".strip()
            for p in Player.query.filter(Player.uuid.in_(player_uuids)).all()
        } if player_uuids else {}

        # Build response
        fixtures = []
        for match_data in matches:
            match = match_data[0]  # Get the Match object
            match_id = match.id  # Keep as integer

            # Get scores
            team1_score = score_lookup.get(match_id, {}).get(match.team1_id, 0)
            team2_score = score_lookup.get(match_id, {}).get(match.team2_id, 0)

            # Get player names
            team1_players = []
            if match_data.team1_player1_uuid:
                team1_players.append(players.get(match_data.team1_player1_uuid, 'Unknown'))
            if match_data.team1_player2_uuid:
                team1_players.append(players.get(match_data.team1_player2_uuid, 'Unknown'))

            team2_players = []
            if match_data.team2_player1_uuid:
                team2_players.append(players.get(match_data.team2_player1_uuid, 'Unknown'))
            if match_data.team2_player2_uuid:
                team2_players.append(players.get(match_data.team2_player2_uuid, 'Unknown'))

            fixture = {
                'match_id': match.id,
                'match_name': match.match_name,
                'team1': {
                    'team_id': match_data.team1_id,
                    'name': match_data.team1_name,
                    'players': team1_players
                },
                'team2': {
                    'team_id': match_data.team2_id,
                    'name': match_data.team2_name,
                    'players': team2_players
                },
                'scores': {
                    'team1_score': team1_score,
                    'team2_score': team2_score
                },
                'status': match.status,
                'is_final': match.is_final,
                'winner_team_id': match.winner_team_id,
                'outcome_type': match.outcome_type
            }
            fixtures.append(fixture)

        return jsonify({
            'pool': pool_name,
            'matches': fixtures,
            'total_matches': len(fixtures)
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@match_ops_bp.route('/pools/<pool_name>/wildcard', methods=['POST'])
def add_wildcard_teams(pool_name):
    """Add exactly two teams as a wildcard match to a pool that already has fixtures"""
    logger.debug(f"Adding wildcard teams to pool {pool_name}")
    data = request.json
    tournament_id = data.get('tournament_id')
    teams_data = data.get('teams', [])

    if not tournament_id:
        return jsonify({
            'error': 'tournament_id is required'
        }), 400

    if len(teams_data) != 2:
        return jsonify({
            'error': 'Exactly two teams are required for wildcard entry'
        }), 400

    # Check if tournament exists
    tournament = Tournament.query.filter_by(id=tournament_id).first()
    if not tournament:
        return jsonify({'error': 'Tournament not found'}), 404

    # Check if pool exists
    pool_exists = Round.query.filter_by(
        tournament_id=tournament_id,
        round_id=1,
        pool=pool_name
    ).first()
    if not pool_exists:
        return jsonify({'error': f'Pool {pool_name} not found'}), 404

    # Check if fixtures exist - required for wildcard
    fixtures_exist = Match.query.filter_by(
        tournament_id=tournament_id,
        round_id='1',
        pool=pool_name
    ).first()

    if not fixtures_exist:
        return jsonify({
            'error': 'Pool must have existing fixtures to add wildcard teams'
        }), 400

    try:
        added_teams = []
        team_objects = []

        # Process both teams
        for team_data in teams_data:
            logger.debug(f"Processing wildcard team: {team_data.get('team_name')}")
            
            # Generate team_id
            team_id = generate_team_id(tournament_id)
            logger.debug(f"Generated team_id: {team_id}")
            
            team_name = team_data.get('team_name', f'Team {team_id}')
            player1_data = team_data.get('player1')
            player2_data = team_data.get('player2')  # Now optional

            # Process player1 (mandatory)
            logger.debug("Processing player1")
            player1_info, error = validate_player_data(player1_data, tournament_id)
            if error:
                return jsonify({'error': f'Player 1: {error}'}), 400

            # Check for existing player1
            existing_player1 = find_existing_player(
                player1_info['first_name'],
                player1_info['last_name'],
                tournament.season.super_tournament.id
            )

            if existing_player1:
                # Check if player is already in this tournament
                if check_player_in_tournament(existing_player1.uuid, tournament_id):
                    return jsonify({
                        'error': f"Player '{player1_info['first_name']} {player1_info['last_name']}' is already in another team in this tournament"
                    }), 409
                player1 = existing_player1
                logger.debug(f"Found existing player1: {player1.uuid}")
            else:
                # Create new player1
                player1 = Player(
                    uuid=generate_uuid(),
                    first_name=player1_info['first_name'],
                    last_name=player1_info['last_name'],
                    phone_number=generate_phone_number(tournament_id),
                    email=player1_info['email'],
                    gender=player1_info['gender'],
                    age=player1_info['age'],
                    skill_type=player1_info['skill_type'],
                    dupr_id=player1_info['dupr_id'],
                    super_tournament_id=tournament.season.super_tournament.id
                )
                db.session.add(player1)
                db.session.flush()
                logger.debug(f"Created new player1 with UUID: {player1.uuid}")

            # Process player2 (optional)
            player2 = None
            existing_player2 = None
            if player2_data:
                logger.debug("Processing player2")
                player2_info, error = validate_player_data(player2_data, tournament_id)
                if error:
                    return jsonify({'error': f'Player 2: {error}'}), 400

                # Check for existing player2
                existing_player2 = find_existing_player(
                    player2_info['first_name'],
                    player2_info['last_name'],
                    tournament.season.super_tournament.id
                )

                if existing_player2:
                    # Check if player is already in this tournament
                    if check_player_in_tournament(existing_player2.uuid, tournament_id):
                        return jsonify({
                            'error': f"Player '{player2_info['first_name']} {player2_info['last_name']}' is already in another team in this tournament"
                        }), 409
                    player2 = existing_player2
                    logger.debug(f"Found existing player2: {player2.uuid}")
                else:
                    # Create new player2
                    player2 = Player(
                        uuid=generate_uuid(),
                        first_name=player2_info['first_name'],
                        last_name=player2_info['last_name'],
                        phone_number=generate_phone_number(tournament_id),
                        email=player2_info['email'],
                        gender=player2_info['gender'],
                        age=player2_info['age'],
                        skill_type=player2_info['skill_type'],
                        dupr_id=player2_info['dupr_id'],
                        super_tournament_id=tournament.season.super_tournament.id
                    )
                    db.session.add(player2)
                    db.session.flush()
                    logger.debug(f"Created new player2 with UUID: {player2.uuid}")

                # Prevent same player being added as both player1 and player2
                if player2 and player1.uuid == player2.uuid:
                    return jsonify({
                        'error': f"Cannot add the same player '{player1_info['first_name']} {player1_info['last_name']}' twice in the same team"
                    }), 400

            # Create team
            logger.debug("Creating team")
            team = Team(
                team_id=team_id,
                name=team_name,
                tournament_id=tournament_id,
                player1_uuid=player1.uuid,
                player2_uuid=player2.uuid if player2 else None
            )
            db.session.add(team)
            team_objects.append(team)
            db.session.flush()

            # Add team to pool
            round_entry = Round(
                tournament_id=tournament_id,
                round_id=1,
                team_id=team_id,
                pool=pool_name,
                name='Round Robin'
            )
            db.session.add(round_entry)

            team_info = {
                'team_id': team_id,
                'team_name': team_name,
                'player1': {
                    'name': f"{player1.first_name} {player1.last_name}".strip(),
                    'phone_number': player1.phone_number,
                    'is_existing': existing_player1 is not None
                }
            }

            # Only add player2 info if it exists
            if player2:
                team_info['player2'] = {
                    'name': f"{player2.first_name} {player2.last_name}".strip(),
                    'phone_number': player2.phone_number,
                    'is_existing': existing_player2 is not None
                }

            added_teams.append(team_info)

        # Create a match between the two new teams
        match_name = f"Round Robin Pool {pool_name} - {team_objects[0].name} vs {team_objects[1].name}"
        match = Match(
            round_id='1',
            pool=pool_name,
            team1_id=team_objects[0].team_id,
            team2_id=team_objects[1].team_id,
            match_name=match_name,
            tournament_id=tournament_id,
            status='pending'
        )
        db.session.add(match)
        db.session.flush()

        # Create scores for the new match
        score_objects = [
            Score(
                match_id=match.id,
                team_id=team_objects[0].team_id,
                score=0,
                tournament_id=tournament_id
            ),
            Score(
                match_id=match.id,
                team_id=team_objects[1].team_id,
                score=0,
                tournament_id=tournament_id
            )
        ]
        db.session.bulk_save_objects(score_objects)

        db.session.commit()
        logger.debug("Successfully added wildcard teams and created their match")

        return jsonify({
            'message': 'Wildcard teams added successfully',
            'added_teams': added_teams,
            'match': {
                'match_id': match.id,
                'match_name': match_name,
                'team1_id': team_objects[0].team_id,
                'team2_id': team_objects[1].team_id
            },
            'pool': pool_name
        }), 201

    except Exception as e:
        logger.error(f"Error adding wildcard teams: {str(e)}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500 