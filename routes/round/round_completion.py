from flask import request, jsonify
from models import Tournament, Team, Player, Match, Score, Round, db
from routes.tournament.tournament_core import show_standings
from services.match_outcomes import decided_result
from . import round_bp

def get_round_standings(tournament_id, round_id):
    """
    Get standings for a specific round in a tournament.
    """
    try:
        # Get all matches for the round
        matches = Match.query.filter_by(
            tournament_id=tournament_id,
            round_id=round_id
        ).all()

        # Get all scores for these matches
        match_ids = [match.id for match in matches]
        scores = Score.query.filter(
            Score.match_id.in_(match_ids),
            Score.tournament_id == tournament_id
        ).all()

        # Organize scores by pool
        pool_standings = {}
        for match in matches:
            if match.pool not in pool_standings:
                pool_standings[match.pool] = {}

            # Get scores for this match
            match_scores = [s for s in scores if s.match_id == match.id]
            
            for score in match_scores:
                if score.team_id not in pool_standings[match.pool]:
                    team = Team.query.get(score.team_id)
                    pool_standings[match.pool][score.team_id] = {
                        'team_id': score.team_id,
                        'name': team.name if team else 'Unknown',
                        'total_scores': 0,
                        'points_scored': 0,
                        'points_lost': 0,
                        'points_difference': 0,
                        'matches_played': set(),
                        'matches_won': 0
                    }

                team_stats = pool_standings[match.pool][score.team_id]

                decided = decided_result(match.outcome_type, match.team1_id, match.team2_id, match.winner_team_id)
                if decided:
                    # Walkover etc.: played + a win for the stored winner, no points.
                    # Credited once per team per match even if score rows are duplicated.
                    if match.id not in team_stats['matches_played']:
                        team_stats['matches_played'].add(match.id)
                        if score.team_id == decided.winner_team_id:
                            team_stats['matches_won'] += 1
                else:
                    team_stats['points_scored'] += score.score
                    team_stats['matches_played'].add(match.id)

                    # Find opponent's score
                    opponent_score = next(
                        (s for s in match_scores if s.team_id != score.team_id),
                        None
                    )
                    if opponent_score:
                        team_stats['points_lost'] += opponent_score.score
                        if score.score > opponent_score.score:
                            team_stats['matches_won'] += 1

                team_stats['total_scores'] = team_stats['matches_won']
                team_stats['points_difference'] = (
                    team_stats['points_scored'] - team_stats['points_lost']
                )

        # Convert to list format for each pool
        result = {
            str(round_id): {
                'pools': {
                    pool: list(teams.values())
                    for pool, teams in pool_standings.items()
                }
            }
        }

        return result

    except Exception as e:
        print(f"Error getting standings: {str(e)}")
        raise e

@round_bp.route('/complete-round', methods=['POST'])
def complete_round():
    data = request.json
    tournament_id = data.get('tournament_id')
    round_id = data.get('round_id')
    next_round_name = data.get('next_round_name')
    promotion_type = data.get('promotion_type')  # 'pool_based', 'leaderboard_based', or 'custom'
    
    # For non-custom promotion
    teams_to_promote = data.get('teams_to_promote')
    
    # Only needed for pool-based promotion
    matchmaking_type = data.get('matchmaking_type')  # 'nearpool', 'samepool', 'farpool'
    
    # For custom matchmaking
    custom_matches = data.get('custom_matches')

    # Validate required fields
    if not all([tournament_id, round_id, promotion_type]):
        return jsonify({
            'error': 'Missing required fields: tournament_id, round_id, promotion_type'
        }), 400

    # Validate promotion-specific requirements
    if promotion_type == 'custom':
        if not custom_matches:
            return jsonify({'error': 'custom_matches is required for custom promotion'}), 400
    elif promotion_type == 'pool_based':
        if not all([teams_to_promote, matchmaking_type]):
            return jsonify({
                'error': 'teams_to_promote and matchmaking_type are required for pool-based promotion'
            }), 400
    elif promotion_type == 'leaderboard_based':
        if not teams_to_promote:
            return jsonify({
                'error': 'teams_to_promote is required for leaderboard-based promotion'
            }), 400

    try:
        new_round_id = int(round_id) + 1
        next_round_name = next_round_name or f"Round {new_round_id}"

        if promotion_type == 'custom':
            # For custom matches, directly create matches from provided pairs
            matches = []
            for team1_id, team2_id in custom_matches:
                team1 = Team.query.get(team1_id)
                team2 = Team.query.get(team2_id)
                if team1 and team2:
                    matches.append((team1, team2))
                    
        else:
            # Get standings data
            standings_data = get_round_standings(tournament_id, round_id)
            round_data = standings_data.get(str(round_id))
            if not round_data:
                return jsonify({'error': 'No standings data found'}), 404

            if promotion_type == 'leaderboard_based':
                # For leaderboard, teams are already in correct order for matches
                promoted_teams = get_leaderboard_promoted_teams(round_data, teams_to_promote)
                # Create matches by pairing adjacent teams
                matches = [(promoted_teams[i], promoted_teams[i+1]) 
                          for i in range(0, len(promoted_teams)-1, 2)]
            else:  # pool_based
                promoted_teams = get_pool_promoted_teams(round_data, teams_to_promote)
                if len(promoted_teams) < 2:
                    return jsonify({'error': 'Not enough teams to create matches'}), 400
                # Create matches based on matchmaking type
                matches = create_matches_by_type(promoted_teams, matchmaking_type)

        # Create round entries and matches in database
        created_matches = create_round_entries_and_matches(
            tournament_id, new_round_id, next_round_name, matches
        )

        # Prepare response with new round details and fixtures
        fixtures = get_round_fixtures(tournament_id, new_round_id)
        
        return jsonify({
            'message': f'Round {new_round_id} created successfully',
            'new_round_id': new_round_id,
            'new_round_name': next_round_name,
            'matches_created': len(created_matches),
            'fixtures': fixtures
        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

def get_leaderboard_promoted_teams(round_data, teams_to_promote):
    """Get top N teams from overall leaderboard"""
    all_teams = []
    for pool_standings in round_data['pools'].values():
        all_teams.extend(pool_standings)
    
    # Sort by total_scores and points_difference
    sorted_teams = sorted(
        all_teams,
        key=lambda x: (x['total_scores'], x['points_difference']),
        reverse=True
    )
    
    return [Team.query.get(t['team_id']) for t in sorted_teams[:teams_to_promote]]

def get_pool_promoted_teams(round_data, teams_to_promote):
    """Get top teams from each pool evenly"""
    num_pools = len(round_data['pools'])
    teams_per_pool = teams_to_promote // num_pools
    promoted_teams = []
    
    # Sort pools alphabetically to ensure consistent order (Pool A before Pool B)
    sorted_pools = sorted(round_data['pools'].keys())
    
    for pool in sorted_pools:
        pool_standings = round_data['pools'][pool]
        sorted_pool = sorted(
            pool_standings,
            key=lambda x: (x['total_scores'], x['points_difference']),
            reverse=True
        )
        promoted_teams.extend([
            Team.query.get(t['team_id']) 
            for t in sorted_pool[:teams_per_pool]
        ])
    
    return promoted_teams

def create_matches_by_type(teams, matchmaking_type):
    """Create matches based on matchmaking type"""
    if matchmaking_type == 'samepool':
        # Pair adjacent teams
        return [(teams[i], teams[i+1]) for i in range(0, len(teams)-1, 2)]
    
    elif matchmaking_type == 'farpool':
        # Get the number of teams per pool
        total_teams = len(teams)
        teams_per_pool = total_teams // 2  # Since we have 2 pools
        
        # Split teams into their respective pools
        pool_a = teams[:teams_per_pool]  # First half is Pool A
        pool_b = teams[teams_per_pool:]  # Second half is Pool B
        
        # Reverse pool B to match highest ranked from A with lowest from B
        pool_b.reverse()
        
        # Create matches by pairing teams from pool A with reversed pool B
        # Always keep pool A teams as team1 (first in the tuple)
        matches = []
        for i in range(min(len(pool_a), len(pool_b))):
            matches.append((pool_a[i], pool_b[i]))  # Ensure Pool A team is first
        
        return matches
    
    elif matchmaking_type == 'nearpool':
        total_teams = len(teams)
        teams_per_pool = 2  # Each pool has top 2 teams
        num_pools = total_teams // teams_per_pool
        matches = []
        
        # Process pools in pairs (A-B, C-D, E-F, G-H)
        for i in range(0, num_pools, 2):
            if i + 1 >= num_pools:  # If we have an odd number of pools
                break
                
            # Get teams from current pair of pools
            pool1_start = i * teams_per_pool
            pool2_start = (i + 1) * teams_per_pool
            
            pool1_teams = teams[pool1_start:pool1_start + teams_per_pool]  # e.g., Pool A teams
            pool2_teams = teams[pool2_start:pool2_start + teams_per_pool]  # e.g., Pool B teams
            
            # Create matches for this pair of pools following the pattern:
            # First team from first pool vs Second team from second pool
            # Second team from first pool vs First team from second pool
            if len(pool1_teams) >= 1 and len(pool2_teams) >= 2:
                matches.append((pool1_teams[0], pool2_teams[1]))  # e.g., A1 vs B2
            if len(pool1_teams) >= 2 and len(pool2_teams) >= 1:
                matches.append((pool1_teams[1], pool2_teams[0]))  # e.g., A2 vs B1
        
        return matches
    
    else:
        # Default to samepool matching
        return [(teams[i], teams[i+1]) for i in range(0, len(teams)-1, 2)]

def create_custom_matches(custom_matches, promoted_teams):
    """Create matches from custom team pairings"""
    team_dict = {team.team_id: team for team in promoted_teams}
    matches = []
    
    for team1_id, team2_id in custom_matches:
        if team1_id in team_dict and team2_id in team_dict:
            matches.append((team_dict[team1_id], team_dict[team2_id]))
    
    return matches

def create_round_entries_and_matches(tournament_id, new_round_id, round_name, matches):
    """Create round entries and matches in database"""
    created_matches = []
    pool_name = 'A'
    
    for team1, team2 in matches:
        # Create round entries
        for team in [team1, team2]:
            round_entry = Round(
                round_id=new_round_id,
                team_id=team.team_id,
                pool=pool_name,
                tournament_id=tournament_id,
                name=round_name
            )
            db.session.add(round_entry)

        # Create match
        match = Match(
            round_id=new_round_id,
            pool=pool_name,
            team1_id=team1.team_id,
            team2_id=team2.team_id,
            match_name=f"{round_name} - {team1.name} vs {team2.name}",
            tournament_id=tournament_id,
            status='pending'
        )
        db.session.add(match)
        db.session.flush()
        created_matches.append(match)

        # Create initial scores
        for team in [team1, team2]:
            score = Score(
                match_id=match.id,
                team_id=team.team_id,
                score=0,
                tournament_id=tournament_id
            )
            db.session.add(score)

    db.session.commit()
    return created_matches

def get_round_fixtures(tournament_id, round_id):
    """Get fixtures for the newly created round"""
    matches = Match.query.filter_by(
        tournament_id=tournament_id,
        round_id=round_id
    ).all()
    
    return [{
        'match_id': match.id,
        'match_name': match.match_name,
        'team1': Team.query.get(match.team1_id).name,
        'team2': Team.query.get(match.team2_id).name,
        'pool': match.pool,
        'status': match.status
    } for match in matches]

@round_bp.route('/complete-round-2', methods=['POST'])
def complete_round2():
    data = request.json
    tournament_id = data.get('tournament_id')
    round_id = data.get('round_id')
    num_promoted = data.get('num_promoted')
    round_name = data.get('round_name')  # Optional, if provided

    if not tournament_id or not num_promoted or not round_id:
        return jsonify({'error': 'tournament_id, num_promoted and round_id are required'}), 400

    # Check if the tournament exists and is elimination-type
    tournament = Tournament.query.filter_by(id=tournament_id).first()
    if not tournament:
        return jsonify({'error': 'Tournament not found'}), 404

    if tournament.type != 'elimination':
        return jsonify({'error': 'This tournament is not elimination type'}), 400

    try:
        # Get all matches for the current round
        matches = Match.query.filter_by(round_id=round_id, tournament_id=tournament_id).all()
        if not matches:
            return jsonify({'error': f'No matches found for round {round_id}'}), 404

        # Get all scores for these matches
        match_ids = [match.id for match in matches]
        scores = Score.query.filter(Score.match_id.in_(match_ids)).all()

        # Create a dictionary to store team scores by pool
        pool_scores = {}
        for match in matches:
            if match.pool not in pool_scores:
                pool_scores[match.pool] = {}

            # Get scores for this match
            match_scores = [s for s in scores if s.match_id == match.id]
            for score in match_scores:
                if score.team_id not in pool_scores[match.pool]:
                    pool_scores[match.pool][score.team_id] = 0
                pool_scores[match.pool][score.team_id] += score.score

        # Sort teams by score in each pool and get top teams
        promoted_teams = []
        for pool, team_scores in pool_scores.items():
            sorted_teams = sorted(team_scores.items(), key=lambda x: x[1], reverse=True)
            top_teams = sorted_teams[:num_promoted]
            promoted_teams.extend([team_id for team_id, _ in top_teams])

        # Create new round with promoted teams
        new_round_id = int(round_id) + 1
        round_name = round_name or f"Round {new_round_id}"

        # Create round entries for promoted teams
        for team_id in promoted_teams:
            new_round = Round(
                tournament_id=tournament_id,
                round_id=new_round_id,
                team_id=team_id,
                name=round_name
            )
            db.session.add(new_round)

        db.session.commit()

        return jsonify({
            'message': 'New round complete!',
            'new_round_id': new_round_id,
        }), 200

    except Exception as e:
        db.session.rollback()
        raise e 