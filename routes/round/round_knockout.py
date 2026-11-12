from flask import request, jsonify
from models import db, Tournament, Match, Team, Score, Round
from sqlalchemy import func
from services.match_outcomes import decided_result
from . import round_bp
from flask_cors import cross_origin
import csv
import io
import math

def is_power_of_two(n):
    return n != 0 and (n & (n - 1)) == 0

def get_pool_standings(tournament_id, round_id):
    """Get standings for each pool"""
    pool_standings = {}
    
    # Get all matches for the round
    matches = Match.query.filter_by(
        tournament_id=tournament_id,
        round_id=str(round_id)
    ).all()
    
    # Get all scores for these matches
    match_ids = [match.id for match in matches]
    scores = Score.query.filter(Score.match_id.in_(match_ids)).all()
    
    # Get all teams in each pool
    pools = db.session.query(Round.pool, Round.team_id).filter_by(
        tournament_id=tournament_id,
        round_id=round_id
    ).distinct().all()
    
    # Clean pool names (remove extra spaces)
    pools = [(pool.strip(), team_id) for pool, team_id in pools]
    
    # Sort pools by name to ensure consistent ordering
    pools.sort(key=lambda x: x[0])
    
    for pool, team_id in pools:
        if pool not in pool_standings:
            pool_standings[pool] = {}
            
        if team_id not in pool_standings[pool]:
            pool_standings[pool][team_id] = {
                'matches_played': 0,
                'matches_won': 0,
                'points_scored': 0,
                'points_against': 0
            }
    
    # Calculate standings
    for match in matches:
        team1_score = next((s.score for s in scores if s.match_id == match.id and s.team_id == match.team1_id), 0)
        team2_score = next((s.score for s in scores if s.match_id == match.id and s.team_id == match.team2_id), 0)
        
        decided = decided_result(match.outcome_type, match.team1_id, match.team2_id, match.winner_team_id)
        if decided and match.team1_id and match.team2_id:
            # Walkover etc.: played + a win for the stored winner, no points
            pool = match.pool.strip()
            for team_id in (match.team1_id, match.team2_id):
                if team_id in pool_standings.get(pool, {}):
                    pool_standings[pool][team_id]['matches_played'] += 1
                    if team_id == decided.winner_team_id:
                        pool_standings[pool][team_id]['matches_won'] += 1
        elif match.team1_id and match.team2_id:  # Only count completed matches
            pool = match.pool.strip()  # Clean pool name
            # Update team 1 stats
            if match.team1_id in pool_standings.get(pool, {}):
                pool_standings[pool][match.team1_id]['matches_played'] += 1
                pool_standings[pool][match.team1_id]['points_scored'] += team1_score
                pool_standings[pool][match.team1_id]['points_against'] += team2_score
                if team1_score > team2_score:
                    pool_standings[pool][match.team1_id]['matches_won'] += 1
                
            # Update team 2 stats
            if match.team2_id in pool_standings.get(pool, {}):
                pool_standings[pool][match.team2_id]['matches_played'] += 1
                pool_standings[pool][match.team2_id]['points_scored'] += team2_score
                pool_standings[pool][match.team2_id]['points_against'] += team1_score
                if team2_score > team1_score:
                    pool_standings[pool][match.team2_id]['matches_won'] += 1
    
    return pool_standings

@round_bp.route('/knockout-top-teams', methods=['GET'])
def get_top_teams_for_knockout():
    """Get top N teams from each pool for knockout stage"""
    tournament_id = request.args.get('tournament_id')
    round_id = request.args.get('round_id')
    teams_per_pool = request.args.get('teams_per_pool', type=int)
    
    if not all([tournament_id, round_id, teams_per_pool]):
        return jsonify({
            'error': 'tournament_id, round_id, and teams_per_pool are required'
        }), 400
    
    try:
        pool_standings = get_pool_standings(tournament_id, round_id)
        top_teams = {}
        
        for pool, teams in pool_standings.items():
            # Sort teams by matches won, then points difference
            sorted_teams = sorted(
                teams.items(),
                key=lambda x: (
                    x[1]['matches_won'],
                    x[1]['points_scored'] - x[1]['points_against']
                ),
                reverse=True
            )
            
            # Get top N teams from this pool
            top_teams[pool] = [
                {
                    'team_id': team_id,
                    'matches_won': stats['matches_won'],
                    'points_difference': stats['points_scored'] - stats['points_against'],
                    'matches_played': stats['matches_played'],
                    'points_scored': stats['points_scored'],
                    'points_against': stats['points_against']
                }
                for team_id, stats in sorted_teams[:teams_per_pool]
            ]
        
        return jsonify({
            'top_teams': top_teams,
            'total_teams': len(pool_standings) * teams_per_pool
        }), 200
        
    except Exception as e:
        print(f"Error in get_top_teams_for_knockout: {str(e)}")
        return jsonify({'error': str(e)}), 500

def create_knockout_structure(team_ids, tournament_id, starting_round_id):
    """Creates a knockout bracket structure from a list of team IDs"""
    if not is_power_of_two(len(team_ids)):
        raise ValueError("Number of teams must be a power of 2")

    total_rounds = int(math.log2(len(team_ids)))
    matches = []
    rounds = []
    match_positions = {}  # To store matches by position for easy reference

    def get_round_name(round_num, total_rounds, num_teams):
        """Helper function to get appropriate round name based on position and bracket size"""
        if round_num == total_rounds:
            return "Finals"
        elif round_num == total_rounds - 1:
            return "Semi Finals"
        elif round_num == total_rounds - 2:
            return "Quarter Finals"
        elif round_num == 1:
            if num_teams == 16:
                return "Round of 16"
            elif num_teams == 32:
                return "Round of 32"
            else:
                return f"Round {round_num}"
        else:
            remaining_teams = num_teams // (2 ** (round_num - 1))
            return f"Round of {remaining_teams}"

    # Create rounds for each bracket stage
    for round_num in range(1, total_rounds + 1):
        current_round_id = starting_round_id + round_num - 1
        round_name = get_round_name(round_num, total_rounds, len(team_ids))

        # Create Round entries for teams in this round
        if round_num == 1:  # Only first round has known teams
            for team_id in team_ids:
                rounds.append(Round(
                    tournament_id=tournament_id,
                    round_id=current_round_id,
                    team_id=team_id,
                    pool="knockout",
                    name=round_name
                ))
        else:
            # Create placeholder round entries for future rounds with TBD
            rounds.append(Round(
                tournament_id=tournament_id,
                round_id=current_round_id,
                team_id="TBD",
                pool="knockout",
                name=round_name
            ))

    # Create first round matches
    first_round_count = len(team_ids) // 2
    for i in range(first_round_count):
        match_name = get_match_name(1, total_rounds, i + 1, len(team_ids))
        match = Match(
            tournament_id=tournament_id,
            match_name=match_name,
            round_id=str(starting_round_id),
            pool="knockout",
            team1_id=team_ids[i*2],
            team2_id=team_ids[i*2+1],
            round_number=1,
            bracket_position=i,
            status='pending'
        )
        matches.append(match)
        match_positions[f"1_{i}"] = match

    # Create subsequent round matches (empty/placeholder matches)
    for round_num in range(2, total_rounds + 1):
        current_round_id = starting_round_id + round_num - 1
        matches_in_round = 2 ** (total_rounds - round_num)
        
        for i in range(matches_in_round):
            match_name = get_match_name(round_num, total_rounds, i + 1, len(team_ids))
            match = Match(
                tournament_id=tournament_id,
                match_name=match_name,
                round_id=str(current_round_id),
                pool="knockout",
                team1_id="TBD",
                team2_id="TBD",
                round_number=round_num,
                bracket_position=i,
                status='pending'
            )
            matches.append(match)
            match_positions[f"{round_num}_{i}"] = match

    return matches, rounds, match_positions

def get_match_name(round_num, total_rounds, match_num, num_teams):
    """Helper function to get appropriate match name based on round and position"""
    if round_num == total_rounds:
        return "F1"
    elif round_num == total_rounds - 1:
        return f"SF{match_num}"
    elif round_num == total_rounds - 2:
        return f"QF{match_num}"
    else:
        remaining_teams = num_teams // (2 ** (round_num - 1))
        return f"R{remaining_teams}-M{match_num}"

@round_bp.route('/knockout', methods=['POST', 'OPTIONS'])
def create_knockout_bracket():
    if request.method == 'OPTIONS':
        # Handled by global CORS
        return '', 200
        
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    tournament_id = data.get('tournament_id')
    team_ids = data.get('team_ids', [])
    current_round_id = data.get('current_round_id', 1)  # The round ID after which knockout starts

    if not tournament_id:
        return jsonify({'error': 'tournament_id is required'}), 400

    if not team_ids:
        return jsonify({'error': 'team_ids array is required'}), 400

    try:
        # Validate number of teams
        if not is_power_of_two(len(team_ids)):
            return jsonify({
                'error': f'Number of teams must be a power of 2. Got {len(team_ids)} teams'
            }), 400

        # Validate all non-TBD teams exist in tournament
        real_team_ids = [tid for tid in team_ids if tid != "TBD"]
        if real_team_ids:
            existing_teams = Team.query.filter(
                Team.team_id.in_(real_team_ids),
                Team.tournament_id == tournament_id
            ).all()
            if len(existing_teams) != len(real_team_ids):
                return jsonify({'error': 'Some teams not found in tournament'}), 400

        # Create knockout structure starting from next round
        starting_round_id = current_round_id + 1
        matches, rounds, match_positions = create_knockout_structure(team_ids, tournament_id, starting_round_id)

        # First save rounds
        db.session.bulk_save_objects(rounds)
        db.session.flush()

        # Save matches and get their IDs
        for match in matches:
            db.session.add(match)
        db.session.flush()

        # Refresh each match to ensure they have IDs
        for match in matches:
            db.session.refresh(match)

        # Now set up predecessor and successor relationships
        total_rounds = int(math.log2(len(team_ids)))
        for round_num in range(1, total_rounds):
            matches_in_round = 2 ** (total_rounds - round_num)
            for i in range(matches_in_round):
                current_match = match_positions[f"{round_num}_{i}"]
                successor_match = match_positions[f"{round_num+1}_{i//2}"]
                
                # Update the relationships using the model
                current_match.successor = successor_match.id
                if i % 2 == 0:
                    successor_match.predecessor_1 = current_match.id
                else:
                    successor_match.predecessor_2 = current_match.id

        # Flush to ensure relationships are saved
        db.session.flush()

        # Create initial scores for first round matches with known teams
        scores = []
        for match in matches:
            if match.team1_id != "TBD" and match.team2_id != "TBD":  # Only for matches with known teams
                scores.extend([
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

        if scores:
            db.session.bulk_save_objects(scores)
        
        # Final commit of all changes
        db.session.commit()

        # Verify the relationships were saved
        saved_matches = Match.query.filter(
            Match.tournament_id == tournament_id,
            Match.round_id >= str(starting_round_id),
            Match.round_id < str(starting_round_id + total_rounds)
        ).all()

        return jsonify({
            'message': 'Knockout bracket created successfully',
            'matches_created': len(matches),
            'rounds_created': len(rounds),
            'starting_round': starting_round_id,
            'total_rounds': total_rounds,
            'verification': {
                'matches_with_successors': len([m for m in saved_matches if m.successor is not None]),
                'matches_with_predecessors': len([m for m in saved_matches if m.predecessor_1 is not None or m.predecessor_2 is not None])
            }
        }), 201

    except Exception as e:
        db.session.rollback()
        print(f"Error in create_knockout_bracket: {str(e)}")
        return jsonify({'error': str(e)}), 500

def get_round_name(round_num, total_rounds, num_teams):
    """Helper function to get appropriate round name based on position and bracket size"""
    if round_num == total_rounds:
        return "Finals"
    elif round_num == total_rounds - 1:
        return "Semi Finals"
    elif round_num == total_rounds - 2:
        return "Quarter Finals"
    elif round_num == 1:
        if num_teams == 16:
            return "Round of 16"
        elif num_teams == 32:
            return "Round of 32"
        else:
            return f"Round {round_num}"
    else:
        remaining_teams = num_teams // (2 ** (round_num - 1))
        return f"Round of {remaining_teams}"

@round_bp.route('/knockout-from-matches', methods=['POST', 'OPTIONS'])
def create_knockout_from_matches():
    if request.method == 'OPTIONS':
        return '', 200
        
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    tournament_id = data.get('tournament_id')
    matches = data.get('matches', [])  # List of {team1_id, team2_id} objects
    current_round_id = data.get('current_round_id', 1)  # Optional, defaults to 1

    print(f"\n=== Starting knockout bracket creation for tournament {tournament_id} ===")
    print(f"Received {len(matches)} matches")

    if not tournament_id or not matches:
        return jsonify({'error': 'tournament_id and matches are required'}), 400

    # Check if knockout bracket already exists
    existing_knockout = Match.query.filter_by(
        tournament_id=tournament_id,
        pool="knockout"
    ).first()
    
    if existing_knockout:
        return jsonify({
            'error': 'Knockout bracket already exists for this tournament. Delete it first.'
        }), 400

    # Determine round type based on number of matches
    num_matches = len(matches)
    round_types = {
        16: ('round_of_32', 32),
        8: ('round_of_16', 16),
        4: ('quarter_finals', 8),
        2: ('semi_finals', 4),
        1: ('finals', 2)
    }

    if num_matches not in round_types:
        return jsonify({
            'error': f'Invalid number of matches. Must be one of: {list(round_types.keys())} matches. Got {num_matches} matches'
        }), 400

    round_type, total_teams = round_types[num_matches]
    print(f"Determined round type: {round_type} with {total_teams} total teams")

    try:
        # Validate all teams exist in tournament
        team_ids = []
        for match in matches:
            team1_id = match.get('team1_id')
            team2_id = match.get('team2_id')
            if not team1_id or not team2_id:
                return jsonify({'error': 'Each match must have team1_id and team2_id'}), 400
            team_ids.extend([team1_id, team2_id])

        # Check for duplicate teams
        if len(set(team_ids)) != len(team_ids):
            return jsonify({'error': 'Duplicate teams found in matches. Each team can only play once per round'}), 400

        # Validate all non-TBD teams exist in tournament
        real_team_ids = [tid for tid in team_ids if tid != "TBD"]
        if real_team_ids:
            existing_teams = Team.query.filter(
                Team.team_id.in_(real_team_ids),
                Team.tournament_id == tournament_id
            ).all()
            if len(existing_teams) != len(real_team_ids):
                return jsonify({'error': 'Some teams not found in tournament'}), 400

        print(f"All {len(team_ids)} teams validated successfully")

        # Calculate total rounds needed to reach finals
        total_rounds = int(math.log2(num_matches * 2))  # If 8 matches (R16) -> 4 rounds (R16, QF, SF, F)
        starting_round_id = current_round_id + 1
        rounds = []
        all_matches = []
        match_positions = {}

        print(f"Creating {total_rounds} rounds starting from round {starting_round_id}")

        # Create rounds
        for round_num in range(1, total_rounds + 1):
            current_round_id = starting_round_id + round_num - 1
            teams_in_round = num_matches * 2 // (2 ** (round_num - 1))  # For R16: 16->8->4->2
            round_name = get_round_name(round_num, total_rounds, num_matches * 2)  # num_matches * 2 gives total teams
            print(f"Creating round {round_num}: {round_name} with {teams_in_round} teams")

            if round_num == 1:
                # First round with provided matches
                for match in matches:
                    rounds.extend([
                        Round(
                            tournament_id=tournament_id,
                            round_id=current_round_id,
                            team_id=match['team1_id'],
                            pool="knockout",
                            name=round_name
                        ),
                        Round(
                            tournament_id=tournament_id,
                            round_id=current_round_id,
                            team_id=match['team2_id'],
                            pool="knockout",
                            name=round_name
                        )
                    ])
            else:
                # Future rounds with TBD teams
                for _ in range(teams_in_round):
                    rounds.append(Round(
                        tournament_id=tournament_id,
                        round_id=current_round_id,
                        team_id="TBD",
                        pool="knockout",
                        name=round_name
                    ))

        # Create first round matches from provided matches
        print("\nCreating first round matches:")
        for i, match_data in enumerate(matches):
            match_name = get_match_name(1, total_rounds, i + 1, num_matches * 2)
            print(f"Creating match {match_name}: {match_data['team1_id']} vs {match_data['team2_id']}")
            match = Match(
                tournament_id=tournament_id,
                match_name=match_name,
                round_id=str(starting_round_id),
                pool="knockout",
                team1_id=match_data['team1_id'],
                team2_id=match_data['team2_id'],
                round_number=1,
                bracket_position=i,
                status='pending'
            )
            all_matches.append(match)
            match_positions[f"1_{i}"] = match

        # Create subsequent round matches (empty/placeholder matches)
        print("\nCreating subsequent round matches:")
        for round_num in range(2, total_rounds + 1):
            current_round_id = starting_round_id + round_num - 1
            matches_in_round = num_matches // (2 ** (round_num - 1))  # For R16: 8->4->2->1
            print(f"Round {round_num}: Creating {matches_in_round} matches")
            
            for i in range(matches_in_round):
                match_name = get_match_name(round_num, total_rounds, i + 1, num_matches * 2)
                match = Match(
                    tournament_id=tournament_id,
                    match_name=match_name,
                    round_id=str(current_round_id),
                    pool="knockout",
                    team1_id="TBD",
                    team2_id="TBD",
                    round_number=round_num,
                    bracket_position=i,
                    status='pending'
                )
                all_matches.append(match)
                match_positions[f"{round_num}_{i}"] = match

        # Save rounds and matches
        print("\nSaving rounds and matches to database")
        db.session.bulk_save_objects(rounds)
        db.session.flush()

        for match in all_matches:
            db.session.add(match)
        db.session.flush()

        # Refresh matches to get IDs
        for match in all_matches:
            db.session.refresh(match)

        # Set up predecessor and successor relationships
        print("\nSetting up match relationships")
        for round_num in range(1, total_rounds):
            matches_in_round = num_matches // (2 ** (round_num - 1))  # For R16: 8->4->2->1
            for i in range(matches_in_round):
                current_match = match_positions[f"{round_num}_{i}"]
                successor_match = match_positions[f"{round_num+1}_{i//2}"]
                
                current_match.successor = successor_match.id
                if i % 2 == 0:
                    successor_match.predecessor_1 = current_match.id
                else:
                    successor_match.predecessor_2 = current_match.id

        # Flush to ensure relationships are saved
        db.session.flush()

        # Create initial scores for first round matches
        print("\nCreating initial scores for first round matches")
        scores = []
        for match in all_matches:
            if match.team1_id != "TBD" and match.team2_id != "TBD":
                scores.extend([
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

        if scores:
            db.session.bulk_save_objects(scores)
        
        db.session.commit()

        # Verify the relationships were saved
        saved_matches = Match.query.filter(
            Match.tournament_id == tournament_id,
            Match.round_id >= str(starting_round_id),
            Match.round_id < str(starting_round_id + total_rounds)
        ).all()

        # Prepare match details for response
        match_details = []
        for match in all_matches:
            match_details.append({
                'id': match.id,
                'name': match.match_name,
                'round_id': match.round_id,
                'round_number': match.round_number,
                'team1_id': match.team1_id,
                'team2_id': match.team2_id,
                'bracket_position': match.bracket_position,
                'status': match.status,
                'predecessor_1': match.predecessor_1,
                'predecessor_2': match.predecessor_2,
                'successor': match.successor
            })

        print("\nKnockout bracket creation completed successfully")
        return jsonify({
            'message': f'Knockout bracket created successfully starting from {round_type}',
            'round_type': round_type,
            'matches_created': len(all_matches),
            'rounds_created': len(rounds),
            'starting_round': starting_round_id,
            'total_rounds': total_rounds,
            'matches': match_details,
            'verification': {
                'matches_with_successors': len([m for m in saved_matches if m.successor is not None]),
                'matches_with_predecessors': len([m for m in saved_matches if m.predecessor_1 is not None or m.predecessor_2 is not None])
            }
        }), 201

    except Exception as e:
        db.session.rollback()
        print(f"Error in create_knockout_from_matches: {str(e)}")
        return jsonify({'error': str(e)}), 500

@round_bp.route('/check-knockout/<tournament_id>', methods=['GET'])
def check_knockout_exists(tournament_id):
    try:
        # Check if any knockout matches exist for this tournament
        knockout_matches = Match.query.filter_by(
            tournament_id=tournament_id,
            pool="knockout"
        ).first()
        
        if knockout_matches:
            # Get details about the knockout stage
            matches = Match.query.filter_by(
                tournament_id=tournament_id,
                pool="knockout"
            ).all()
            
            rounds = set()
            for match in matches:
                rounds.add(match.round_id)
            
            return jsonify({
                'exists': True,
                'total_matches': len(matches),
                'rounds': list(rounds),
                'message': 'Knockout bracket already exists for this tournament'
            }), 200
        
        return jsonify({
            'exists': False,
            'message': 'No knockout bracket found for this tournament'
        }), 200
        
    except Exception as e:
        print(f"Error checking knockout bracket: {str(e)}")
        return jsonify({'error': str(e)}), 500

@round_bp.route('/delete-knockout/<tournament_id>', methods=['DELETE'])
def delete_knockout_bracket(tournament_id):
    try:
        # Start a transaction
        db.session.begin()
        
        print(f"Deleting knockout bracket for tournament {tournament_id}")
        
        # Delete scores first due to foreign key constraints
        scores_deleted = Score.query.filter(
            Score.match_id.in_(
                db.session.query(Match.id).filter_by(
                    tournament_id=tournament_id,
                    pool="knockout"
                )
            )
        ).delete(synchronize_session=False)
        print(f"Deleted {scores_deleted} scores")
        
        # Delete matches
        matches_deleted = Match.query.filter_by(
            tournament_id=tournament_id,
            pool="knockout"
        ).delete(synchronize_session=False)
        print(f"Deleted {matches_deleted} matches")
        
        # Delete rounds
        rounds_deleted = Round.query.filter_by(
            tournament_id=tournament_id,
            pool="knockout"
        ).delete(synchronize_session=False)
        print(f"Deleted {rounds_deleted} rounds")
        
        db.session.commit()
        
        return jsonify({
            'message': 'Knockout bracket deleted successfully',
            'scores_deleted': scores_deleted,
            'matches_deleted': matches_deleted,
            'rounds_deleted': rounds_deleted
        }), 200
        
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting knockout bracket: {str(e)}")
        return jsonify({'error': str(e)}), 500

