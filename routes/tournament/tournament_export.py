from flask import request, jsonify, current_app, Response
from models import Tournament, Team, Player, Match, Score, db
from sqlalchemy import text
from datetime import datetime
from services.match_outcomes import uses_scores
import csv
import io
from . import tournament_bp

@tournament_bp.route('/export-tournament-csv', methods=['GET'])
def export_tournament_csv():
    tournament_id = request.args.get('tournament_id')

    if not tournament_id:
        return jsonify({'error': 'tournament_id is required'}), 400

    try:
        # Fetch tournament
        tournament = Tournament.query.filter_by(id=tournament_id).first()
        if not tournament:
            return jsonify({'error': 'Tournament not found'}), 404

        # Fetch matches for the tournament
        matches = Match.query.filter_by(tournament_id=tournament_id).all()
        if not matches:
            return jsonify({'error': 'No matches found for this tournament'}), 404

        # Create an in-memory CSV file
        output = io.StringIO()
        writer = csv.writer(output)

        # Write the header row
        writer.writerow([
            "", "", "", "matchType", "event", "date", "playerA1", "playerA1DuprId", "playerA1ExternalId",
            "playerA2", "playerA2DuprId", "playerA2ExternalId", "playerB1", "playerB1DuprId", "playerB1ExternalId",
            "playerB2", "playerB2DuprId", "playerB2ExternalId", "", "teamAGame1", "teamBGame1", "teamAGame2",
            "teamBGame2", "teamAGame3", "teamBGame3", "teamAGame4", "teamBGame4", "teamAGame5", "teamBGame5"
        ])

        # Get the current date in YYYY-MM-DD format
        current_date = datetime.now().strftime('%Y-%m-%d')

        # Populate rows with match data only if the match is finalized
        for match in matches:
            if not match.is_final:
                continue  # Skip matches that are not finalized

            if not uses_scores(match.outcome_type):
                continue  # Walkover etc.: no game was played, nothing to report to DUPR

            # Fetch teams for each match
            team1 = Team.query.filter_by(team_id=match.team1_id).first()
            team2 = Team.query.filter_by(team_id=match.team2_id).first()

            if not team1 or not team2:
                continue  # Skip if team data is missing

            # Get match type ("S" for singles, "D" for doubles)
            match_type = "D" if (team1.player2 or team2.player2) else "S"

            # Prepare scores for each game (assuming up to 5 games)
            scores = Score.query.filter_by(match_id=match.id).all()
            team_a_scores = [score.score for score in scores if score.team_id == team1.team_id]
            team_b_scores = [score.score for score in scores if score.team_id == team2.team_id]

            # Ensure both score lists have up to 5 entries, pad with empty strings if necessary
            while len(team_a_scores) < 5:
                team_a_scores.append("")
            while len(team_b_scores) < 5:
                team_b_scores.append("")

            # Interleave the scores for team A and team B
            interleaved_scores = []
            for i in range(5):
                interleaved_scores.append(team_a_scores[i])  # Add team A score
                interleaved_scores.append(team_b_scores[i])  # Add team B score

            # Write match details to CSV
            writer.writerow([
                "", "", "", match_type, tournament.tournament_name, current_date,
                f"{team1.player1.first_name} {team1.player1.last_name}".strip() if team1.player1 else "", team1.player1.dupr_id if team1.player1 else "", "",
                f"{team1.player2.first_name} {team1.player2.last_name}".strip() if team1.player2 else "", team1.player2.dupr_id if team1.player2 else "", "",
                f"{team2.player1.first_name} {team2.player1.last_name}".strip() if team2.player1 else "", team2.player1.dupr_id if team2.player1 else "", "",
                f"{team2.player2.first_name} {team2.player2.last_name}".strip() if team2.player2 else "", team2.player2.dupr_id if team2.player2 else "", "",
                "",  # Leave empty
                *interleaved_scores  # Add all scores in the correct alternating order
            ])

        # Return CSV as a downloadable response
        output.seek(0)
        return Response(
            output,
            mimetype='text/csv',
            headers={"Content-Disposition": f"attachment;filename=tournament_{tournament_id}_matches.csv"}
        )

    except Exception as e:
        return jsonify({'error': str(e)}), 500 