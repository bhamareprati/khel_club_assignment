from flask import request, jsonify, current_app, render_template
from models import Tournament, Team, Player, Match, Score, Round, db, Season
from sqlalchemy import text, or_, and_, func, case, distinct
from services.match_outcomes import decided_result
from . import tournament_bp


def _credit_decided_match(stats_by_team, match, decided):
    """Standings policy for a match decided WITHOUT scores (e.g. walkover):
    it counts as a played match for both teams and one win for the winner
    (the loser's loss follows from played - won). No points are scored or
    conceded, so points_difference is untouched. `match` is a result row that
    carries match_id/team1_id/team2_id; `decided` comes from decided_result()."""
    stats_by_team[match.team1_id]['matches'].add(match.match_id)
    stats_by_team[match.team2_id]['matches'].add(match.match_id)
    stats_by_team[decided.winner_team_id]['matches_won'] += 1


@tournament_bp.route('/tournaments', methods=['POST'])
def create_tournament():
    # Parse request body
    data = request.get_json()

    # Check if required fields are present
    if 'name' not in data or 'type' not in data or 'season_id' not in data:
        return jsonify({"error": "Name, type, and season_id are required."}), 400

    # Validate tournament type
    if data['type'] not in ["elimination", "regular"]:
        return jsonify({"error": "Invalid tournament type. Must be 'elimination' or 'regular'."}), 400

    # Verify season exists
    season = Season.query.get_or_404(data['season_id'])

    # Get number of courts (optional, default to 1)
    num_courts = data.get('num_courts', 1)
    
    # Validate number of courts
    if not isinstance(num_courts, int) or num_courts < 1:
        return jsonify({"error": "Number of courts must be a positive integer"}), 400

    # Create a new tournament
    new_tournament = Tournament(
        tournament_name=data['name'], 
        type=data['type'],
        num_courts=num_courts,
        season_id=season.id
    )

    # Add to the database
    db.session.add(new_tournament)
    db.session.commit()
    return jsonify({
        "message": "Tournament created successfully.", 
        "tournament_id": new_tournament.id
    }), 201

@tournament_bp.route('/tournaments/<int:tournament_id>', methods=['GET'])
def get_tournament(tournament_id):
    try:
        # Log the request
        current_app.logger.info(f"Fetching tournament with ID: {tournament_id}")
        
        tournament = Tournament.query.get_or_404(tournament_id)
        current_app.logger.info(f"Found tournament: {tournament.tournament_name}")
        
        # Build response
        tournament_data = {
            'tournament_id': tournament.id,
            'name': tournament.tournament_name,
            'type': tournament.type,
            'teams': []
        } 

        # Iterate over teams in the tournament
        current_app.logger.debug(f"Fetching teams for tournament {tournament_id}")
        for team in tournament.teams:
            current_app.logger.debug(f"Processing team: {team.team_id}")
            team_data = {
                'team_id': team.team_id,
                'name': team.name,
                'points': team.points,
                'checked_in': team.checked_in,
                'players': []
            }
            
            # Add players to the team using the new relationships
            if team.player1:
                player_data = {
                    'player_id': team.player1.id,
                    'uuid': team.player1.uuid,
                    'first_name': team.player1.first_name,
                    'last_name': team.player1.last_name,
                    'gender': team.player1.gender,
                    'age': team.player1.age,
                    'phone_number': team.player1.phone_number,
                    'email': team.player1.email,
                    'skill_type': team.player1.skill_type,
                    'dupr_id': team.player1.dupr_id,
                    'checked_in': team.player1.checked_in
                }
                team_data['players'].append(player_data)
            
            if team.player2:
                player_data = {
                    'player_id': team.player2.id,
                    'uuid': team.player2.uuid,
                    'first_name': team.player2.first_name,
                    'last_name': team.player2.last_name,
                    'gender': team.player2.gender,
                    'age': team.player2.age,
                    'phone_number': team.player2.phone_number,
                    'email': team.player2.email,
                    'skill_type': team.player2.skill_type,
                    'dupr_id': team.player2.dupr_id,
                    'checked_in': team.player2.checked_in
                }
                team_data['players'].append(player_data)
            
            # Calculate if all players are checked in
            team_data['all_players_checked_in'] = all(p.get('checked_in', False) for p in team_data['players'])
            tournament_data['teams'].append(team_data)

        current_app.logger.info(f"Successfully retrieved tournament data for ID: {tournament_id}")
        return jsonify(tournament_data), 200

    except Exception as e:
        current_app.logger.error(f"Error fetching tournament {tournament_id}: {str(e)}")
        return jsonify({
            'error': 'An error occurred while fetching tournament data',
            'details': str(e)
        }), 500

@tournament_bp.route('/standings/<int:tournament_id>')
def show_standings(tournament_id):
    print("\n=== Starting show_standings endpoint ===")
    print(f"Getting standings for tournament_id={tournament_id}")

    # Get all match scores using direct SQL for better control
    score_sql = text("""
        SELECT 
            m.id as match_id,
            m.team1_id,
            m.team2_id,
            m.round_id,
            m.pool,
            s1.team_id as team1_id,
            s1.score as team1_score,
            s2.team_id as team2_id,
            s2.score as team2_score,
            m.outcome_type as outcome_type,
            m.winner_team_id as winner_team_id
        FROM `match` m
        JOIN score s1 ON s1.match_id = m.id AND s1.team_id = m.team1_id
        JOIN score s2 ON s2.match_id = m.id AND s2.team_id = m.team2_id
        WHERE m.tournament_id = :tournament_id
    """)

    matches = db.session.execute(
        score_sql,
        {'tournament_id': tournament_id}
    ).fetchall()

    print(f"Found {len(matches)} matches")

    # Create a dictionary to store scores by team and round
    team_scores = {}
    
    # Process all matches
    for match in matches:
        round_id = match.round_id
        if round_id not in team_scores:
            team_scores[round_id] = {}
            
        # Initialize team data if not exists
        for team_id in [match.team1_id, match.team2_id]:
            if team_id not in team_scores[round_id]:
                team_scores[round_id][team_id] = {
                    'points_scored': 0,
                    'points_lost': 0,
                    'matches': set(),
                    'matches_won': 0
                }
        
        print(f"\nProcessing match {match.match_id}:")
        print(f"Team1 ({match.team1_id}): {match.team1_score}")
        print(f"Team2 ({match.team2_id}): {match.team2_score}")

        # Walkover etc.: the stored winner decides, never the (0-0) score rows
        decided = decided_result(match.outcome_type, match.team1_id, match.team2_id, match.winner_team_id)
        if decided:
            _credit_decided_match(team_scores[round_id], match, decided)
            print(f"Decided by {match.outcome_type}: Team {decided.winner_team_id}")
            continue

        # Add scores
        team_scores[round_id][match.team1_id]['points_scored'] += match.team1_score
        team_scores[round_id][match.team1_id]['points_lost'] += match.team2_score
        team_scores[round_id][match.team2_id]['points_scored'] += match.team2_score
        team_scores[round_id][match.team2_id]['points_lost'] += match.team1_score
        
        # Track match and determine winner
        if match.team1_score == 0 and match.team2_score == 0:
            print("Match not played yet")
            continue
            
        team_scores[round_id][match.team1_id]['matches'].add(match.match_id)
        team_scores[round_id][match.team2_id]['matches'].add(match.match_id)
        
        if match.team1_score > match.team2_score:
            team_scores[round_id][match.team1_id]['matches_won'] += 1
            print(f"Winner: Team {match.team1_id}")
        elif match.team2_score > match.team1_score:
            team_scores[round_id][match.team2_id]['matches_won'] += 1
            print(f"Winner: Team {match.team2_id}")
        else:
            print("Match tied or not completed")

    # Get teams by pool and round
    print("\n=== Getting teams from matches ===")
    teams_query = text("""
        SELECT DISTINCT
            m.pool,
            m.round_id,
            t.team_id,
            t.name,
            t.player1_uuid,
            t.player2_uuid
        FROM team t
        JOIN `match` m ON (t.team_id = m.team1_id OR t.team_id = m.team2_id)
        WHERE m.tournament_id = :tournament_id
        AND t.tournament_id = :tournament_id
    """)
    
    print(f"Tournament ID being queried: {tournament_id}")
    
    # Get teams that have played matches
    teams_by_pool_round = db.session.execute(
        teams_query,
        {'tournament_id': tournament_id}
    ).fetchall()
    
    print(f"\nFound {len(teams_by_pool_round)} teams that have played matches")

    # Get all player UUIDs
    player_uuids = set()
    for team in teams_by_pool_round:
        if team.player1_uuid:
            player_uuids.add(team.player1_uuid)
        if team.player2_uuid:
            player_uuids.add(team.player2_uuid)

    # Fetch all players in one query
    players = Player.query.filter(Player.uuid.in_(player_uuids)).all()
    player_lookup = {player.uuid: f"{player.first_name} {player.last_name}".strip() for player in players}

    # Build standings
    standings_by_round = {}
    
    for team in teams_by_pool_round:
        round_id = team.round_id
        pool = team.pool
        
        if round_id not in standings_by_round:
            standings_by_round[round_id] = {'round_name': None, 'pools': {}}
        if pool not in standings_by_round[round_id]['pools']:
            standings_by_round[round_id]['pools'][pool] = []
            
        # Get team's scores for this round
        team_data = team_scores.get(str(round_id), {}).get(team.team_id, {
            'points_scored': 0,
            'points_lost': 0,
            'matches': set(),
            'matches_won': 0
        })
        
        matches_played = len(team_data['matches'])
        matches_won = team_data['matches_won']
        points_scored = team_data['points_scored']
        points_lost = team_data['points_lost']
        
        print(f"\nTeam {team.team_id} stats:")
        print(f"Matches played: {matches_played}")
        print(f"Matches won: {matches_won}")
        print(f"Points scored: {points_scored}")
        print(f"Points lost: {points_lost}")
        
        # Get player names from lookup
        player_names = []
        if team.player1_uuid and team.player1_uuid in player_lookup:
            player_names.append(player_lookup[team.player1_uuid])
        if team.player2_uuid and team.player2_uuid in player_lookup:
            player_names.append(player_lookup[team.player2_uuid])
        player_names_str = ' / '.join(player_names)
        
        standings_by_round[round_id]['pools'][pool].append({
            'team_id': team.team_id,
            'players': player_names_str,
            'matches_played': matches_played,
            'matches_won': matches_won,
            'matches_lost': matches_played - matches_won,
            'points_scored': points_scored,
            'points_lost': points_lost,
            'points_difference': points_scored - points_lost,
            'total_scores': matches_won * 2  # 2 points per win
        })
    
    # Sort standings
    for round_id in standings_by_round:
        for pool in standings_by_round[round_id]['pools']:
            standings_by_round[round_id]['pools'][pool].sort(
                key=lambda x: (x['total_scores'], x['points_difference']),
                reverse=True
            )

    print("\nSending response")
    return jsonify(standings_by_round)

@tournament_bp.route('/overall-standings/<int:tournament_id>')
def show_overall_standings(tournament_id):
    print("\n=== Starting show_overall_standings endpoint ===")
    print(f"Getting overall standings for tournament_id={tournament_id}")

    # Get all match scores using direct SQL
    score_sql = text("""
        SELECT 
            m.id as match_id,
            m.team1_id,
            m.team2_id,
            s1.team_id as team1_id,
            s1.score as team1_score,
            s2.team_id as team2_id,
            s2.score as team2_score,
            m.outcome_type as outcome_type,
            m.winner_team_id as winner_team_id
        FROM `match` m
        JOIN score s1 ON s1.match_id = m.id AND s1.team_id = m.team1_id
        JOIN score s2 ON s2.match_id = m.id AND s2.team_id = m.team2_id
        WHERE m.tournament_id = :tournament_id
    """)

    matches = db.session.execute(
        score_sql,
        {'tournament_id': tournament_id}
    ).fetchall()

    print(f"Found {len(matches)} matches")

    # Create a dictionary to store overall scores by team
    team_scores = {}
    
    # Process all matches
    for match in matches:
        # Initialize team data if not exists
        for team_id in [match.team1_id, match.team2_id]:
            if team_id not in team_scores:
                team_scores[team_id] = {
                    'points_scored': 0,
                    'points_lost': 0,
                    'matches': set(),
                    'matches_won': 0
                }
        
        print(f"\nProcessing match {match.match_id}:")
        print(f"Team1 ({match.team1_id}): {match.team1_score}")
        print(f"Team2 ({match.team2_id}): {match.team2_score}")

        # Walkover etc.: the stored winner decides, never the (0-0) score rows
        decided = decided_result(match.outcome_type, match.team1_id, match.team2_id, match.winner_team_id)
        if decided:
            _credit_decided_match(team_scores, match, decided)
            print(f"Decided by {match.outcome_type}: Team {decided.winner_team_id}")
            continue

        # Skip unplayed matches
        if match.team1_score == 0 and match.team2_score == 0:
            print("Match not played yet")
            continue

        # Add scores
        team_scores[match.team1_id]['points_scored'] += match.team1_score
        team_scores[match.team1_id]['points_lost'] += match.team2_score
        team_scores[match.team2_id]['points_scored'] += match.team2_score
        team_scores[match.team2_id]['points_lost'] += match.team1_score
        
        # Track match and determine winner
        team_scores[match.team1_id]['matches'].add(match.match_id)
        team_scores[match.team2_id]['matches'].add(match.match_id)
        
        if match.team1_score > match.team2_score:
            team_scores[match.team1_id]['matches_won'] += 1
            print(f"Winner: Team {match.team1_id}")
        elif match.team2_score > match.team1_score:
            team_scores[match.team2_id]['matches_won'] += 1
            print(f"Winner: Team {match.team2_id}")
        else:
            print("Match tied or not completed")

    # Get all teams and their players
    teams_query = text("""
        SELECT DISTINCT
            t.team_id,
            t.name,
            t.player1_uuid,
            t.player2_uuid
        FROM team t
        JOIN `match` m ON (t.team_id = m.team1_id OR t.team_id = m.team2_id)
        WHERE m.tournament_id = :tournament_id
        AND t.tournament_id = :tournament_id
    """)
    
    teams = db.session.execute(
        teams_query,
        {'tournament_id': tournament_id}
    ).fetchall()

    # Get all player UUIDs
    player_uuids = set()
    for team in teams:
        if team.player1_uuid:
            player_uuids.add(team.player1_uuid)
        if team.player2_uuid:
            player_uuids.add(team.player2_uuid)

    # Fetch all players in one query
    players = Player.query.filter(Player.uuid.in_(player_uuids)).all()
    player_lookup = {player.uuid: f"{player.first_name} {player.last_name}".strip() for player in players}

    # Build overall standings
    overall_standings = []
    
    for team in teams:
        # Get team's overall scores
        team_data = team_scores.get(team.team_id, {
            'points_scored': 0,
            'points_lost': 0,
            'matches': set(),
            'matches_won': 0
        })
        
        matches_played = len(team_data['matches'])
        matches_won = team_data['matches_won']
        points_scored = team_data['points_scored']
        points_lost = team_data['points_lost']
        
        # Get player names from lookup
        player_names = []
        if team.player1_uuid and team.player1_uuid in player_lookup:
            player_names.append(player_lookup[team.player1_uuid])
        if team.player2_uuid and team.player2_uuid in player_lookup:
            player_names.append(player_lookup[team.player2_uuid])
        player_names_str = ' / '.join(player_names)
        
        overall_standings.append({
            'team_id': team.team_id,
            'team_name': team.name,
            'players': player_names_str,
            'matches_played': matches_played,
            'matches_won': matches_won,
            'matches_lost': matches_played - matches_won,
            'win_percentage': round((matches_won / matches_played * 100) if matches_played > 0 else 0, 2),
            'points_scored': points_scored,
            'points_lost': points_lost,
            'points_difference': points_scored - points_lost,
            'total_scores': matches_won * 2  # 2 points per win
        })
    
    # Sort standings by:
    # 1. Total scores (wins)
    # 2. Win percentage
    # 3. Points difference
    overall_standings.sort(
        key=lambda x: (x['total_scores'], x['win_percentage'], x['points_difference']),
        reverse=True
    )

    response = {
        'tournament_id': tournament_id,
        'standings': overall_standings
    }

    print("\nSending response")
    return jsonify(response)

@tournament_bp.route('/tournament-meta/<int:tournament_id>', methods=['GET'])
def get_tournament_metadata(tournament_id):
    try:
        # Query tournament with eager loading of relationships
        tournament = Tournament.query.get_or_404(tournament_id)
        
        # Get associated season
        season = tournament.season
        
        # Get associated super tournament
        super_tournament = season.super_tournament
        
        # Build response data
        tournament_meta = {
            'tournament': {
                'id': tournament.id,
                'name': tournament.tournament_name,
                'type': tournament.type,
                'num_courts': tournament.num_courts
            },
            'season': {
                'id': season.id,
                'name': season.name
            },
            'super_tournament': {
                'id': super_tournament.id,
                'name': super_tournament.name,
                'description': super_tournament.description
            }
        }
        
        return jsonify(tournament_meta), 200
        
    except Exception as e:
        current_app.logger.error(f"Error fetching tournament metadata for ID {tournament_id}: {str(e)}")
        return jsonify({
            'error': 'An error occurred while fetching tournament metadata',
            'details': str(e)
        }), 500

@tournament_bp.route('/second-place-standings/<int:tournament_id>')
def show_second_place_standings(tournament_id):
    print("\n=== Starting show_second_place_standings endpoint ===")
    print(f"Getting second place standings for tournament_id={tournament_id}")

    # Get all match scores using direct SQL for better control - only for round_id=1
    score_sql = text("""
        SELECT 
            m.id as match_id,
            m.team1_id,
            m.team2_id,
            m.round_id,
            m.pool,
            s1.team_id as team1_id,
            s1.score as team1_score,
            s2.team_id as team2_id,
            s2.score as team2_score,
            m.outcome_type as outcome_type,
            m.winner_team_id as winner_team_id
        FROM `match` m
        JOIN score s1 ON s1.match_id = m.id AND s1.team_id = m.team1_id
        JOIN score s2 ON s2.match_id = m.id AND s2.team_id = m.team2_id
        WHERE m.tournament_id = :tournament_id
        AND m.round_id = '1'  -- Only consider round robin matches
    """)

    matches = db.session.execute(
        score_sql,
        {'tournament_id': tournament_id}
    ).fetchall()
    
    print(f"Found {len(matches)} matches in round robin")
    
    # Create a dictionary to store scores by pool
    pool_scores = {}  # {pool: {team_id: stats}}
    
    # Process all matches
    for match in matches:
        pool = match.pool
        
        if pool not in pool_scores:
            pool_scores[pool] = {}
            
        # Initialize team data if not exists
        for team_id in [match.team1_id, match.team2_id]:
            if team_id not in pool_scores[pool]:
                pool_scores[pool][team_id] = {
                    'points_scored': 0,
                    'points_lost': 0,
                    'matches': set(),
                    'matches_won': 0
                }
        
        print(f"\nProcessing match {match.match_id}:")
        print(f"Team1 ({match.team1_id}): {match.team1_score}")
        print(f"Team2 ({match.team2_id}): {match.team2_score}")

        # Walkover etc.: the stored winner decides, never the (0-0) score rows
        decided = decided_result(match.outcome_type, match.team1_id, match.team2_id, match.winner_team_id)
        if decided:
            _credit_decided_match(pool_scores[pool], match, decided)
            print(f"Decided by {match.outcome_type}: Team {decided.winner_team_id}")
            continue

        # Add scores
        pool_scores[pool][match.team1_id]['points_scored'] += match.team1_score
        pool_scores[pool][match.team1_id]['points_lost'] += match.team2_score
        pool_scores[pool][match.team2_id]['points_scored'] += match.team2_score
        pool_scores[pool][match.team2_id]['points_lost'] += match.team1_score
        
        # Track match and determine winner
        if match.team1_score == 0 and match.team2_score == 0:
            print("Match not played yet")
            continue
            
        pool_scores[pool][match.team1_id]['matches'].add(match.match_id)
        pool_scores[pool][match.team2_id]['matches'].add(match.match_id)
        
        if match.team1_score > match.team2_score:
            pool_scores[pool][match.team1_id]['matches_won'] += 1
            print(f"Winner: Team {match.team1_id}")
        elif match.team2_score > match.team1_score:
            pool_scores[pool][match.team2_id]['matches_won'] += 1
            print(f"Winner: Team {match.team2_id}")
        else:
            print("Match tied or not completed")

    # Get teams by pool for round 1
    print("\n=== Getting teams from round robin ===")
    teams_query = text("""
        SELECT DISTINCT
            m.pool,
            t.team_id,
            t.name,
            t.player1_uuid,
            t.player2_uuid
        FROM team t
        JOIN `match` m ON (t.team_id = m.team1_id OR t.team_id = m.team2_id)
        WHERE m.tournament_id = :tournament_id
        AND m.round_id = '1'  -- Only consider round robin teams
        AND t.tournament_id = :tournament_id
    """)
    
    teams_by_pool = db.session.execute(
        teams_query,
        {'tournament_id': tournament_id}
    ).fetchall()
    
    print(f"\nFound {len(teams_by_pool)} teams in round robin")

    # Get all player UUIDs
    player_uuids = set()
    for team in teams_by_pool:
        if team.player1_uuid:
            player_uuids.add(team.player1_uuid)
        if team.player2_uuid:
            player_uuids.add(team.player2_uuid)

    # Fetch all players in one query
    players = Player.query.filter(Player.uuid.in_(player_uuids)).all()
    player_lookup = {player.uuid: f"{player.first_name} {player.last_name}".strip() for player in players}

    # Build pool standings and get second place teams
    pool_standings = {}  # {pool: [team_standings]}
    second_place_standings = []
    
    for team in teams_by_pool:
        pool = team.pool
        
        if pool not in pool_scores:
            continue
            
        # Get team's scores for this pool
        team_data = pool_scores[pool].get(team.team_id, {
            'points_scored': 0,
            'points_lost': 0,
            'matches': set(),
            'matches_won': 0
        })
        
        matches_played = len(team_data['matches'])
        matches_won = team_data['matches_won']
        points_scored = team_data['points_scored']
        points_lost = team_data['points_lost']
        
        # Get player names from lookup
        player_names = []
        if team.player1_uuid and team.player1_uuid in player_lookup:
            player_names.append(player_lookup[team.player1_uuid])
        if team.player2_uuid and team.player2_uuid in player_lookup:
            player_names.append(player_lookup[team.player2_uuid])
        player_names_str = ' / '.join(player_names)
        
        # Create team standings data
        team_standings = {
            'team_id': team.team_id,
            'team_name': team.name,
            'pool': pool,
            'players': player_names_str,
            'matches_played': matches_played,
            'matches_won': matches_won,
            'matches_lost': matches_played - matches_won,
            'points_scored': points_scored,
            'points_lost': points_lost,
            'points_difference': points_scored - points_lost,
            'total_scores': matches_won * 2,  # 2 points per win
            'win_percentage': round((matches_won / matches_played * 100) if matches_played > 0 else 0, 2)
        }
        
        # Add to pool standings
        if pool not in pool_standings:
            pool_standings[pool] = []
        pool_standings[pool].append(team_standings)
    
    # Sort each pool's standings and get second place teams
    for pool in pool_standings:
        # Sort pool standings
        pool_standings[pool].sort(
            key=lambda x: (x['total_scores'], x['points_difference']),
            reverse=True
        )
        
        # Get second place team if available
        if len(pool_standings[pool]) >= 2:
            second_place_standings.append(pool_standings[pool][1])
    
    # Sort second place standings
    second_place_standings.sort(
        key=lambda x: (x['total_scores'], x['win_percentage'], x['points_difference']),
        reverse=True
    )

    # Add overall rank
    for i, team in enumerate(second_place_standings, 1):
        team['rank'] = i

    response = {
        'tournament_id': tournament_id,
        'total_second_place_teams': len(second_place_standings),
        'standings': second_place_standings
    }

    print("\nSending response")
    return jsonify(response)