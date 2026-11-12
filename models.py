from flask_sqlalchemy import SQLAlchemy
from enum import Enum

db = SQLAlchemy()

class MatchType(str, Enum):
    SINGLES = 'singles'
    DOUBLES = 'doubles'

class SkillType(str, Enum):
    BEGINNER = 'beginner'
    INTERMEDIATE = 'intermediate'
    ADVANCED = 'advanced'
    PROFESSIONAL = 'professional'

class SuperTournament(db.Model):
    __tablename__ = 'super_tournament'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    # Relationship
    seasons = db.relationship('Season', backref='super_tournament', lazy=True)

class Season(db.Model):
    __tablename__ = 'season'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    super_tournament_id = db.Column(db.Integer, db.ForeignKey('super_tournament.id'), nullable=False)
    # Relationship
    tournaments = db.relationship('Tournament', backref='season', lazy=True)

class Tournament(db.Model):
    __tablename__ = 'tournament'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tournament_name = db.Column(db.String(255), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    teams = db.relationship('Team', backref='tournament', lazy=True)
    matches = db.relationship('Match', backref='tournament', lazy=True)
    scores = db.relationship('Score', backref='tournament', lazy=True)
    rounds = db.relationship('Round', backref='tournament', lazy=True)
    num_courts = db.Column(db.Integer, default=1)
    season_id = db.Column(db.Integer, db.ForeignKey('season.id'), nullable=False) #make it nullable

class Team(db.Model):
    team_id = db.Column(db.String(50), primary_key=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    points = db.Column(db.Integer, default=0)
    checked_in = db.Column(db.Boolean, default=False)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournament.id'), nullable=False)
    player1_uuid = db.Column(db.String(36), db.ForeignKey('player.uuid'), nullable=True)
    player2_uuid = db.Column(db.String(36), db.ForeignKey('player.uuid'), nullable=True)
    # Add relationships for players
    player1 = db.relationship('Player', foreign_keys=[player1_uuid])
    player2 = db.relationship('Player', foreign_keys=[player2_uuid])

class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    uuid = db.Column(db.String(36), unique=True)
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=True)
    gender = db.Column(db.String(10), nullable=False)
    age = db.Column(db.Integer, nullable=False)
    phone_number = db.Column(db.String(15), nullable=False, index=True)
    email = db.Column(db.String(120), nullable=False)
    skill_type = db.Column(db.String(20), nullable=False)
    dupr_id = db.Column(db.String(50), nullable=True)
    super_tournament_id = db.Column(db.Integer, db.ForeignKey('super_tournament.id'), nullable=False)
    checked_in = db.Column(db.Boolean, default=False)
    # Add relationships for teams where player is player1 or player2
    teams_as_player1 = db.relationship('Team', 
                                     foreign_keys=[Team.player1_uuid],
                                     backref=db.backref('player1_rel', lazy=True))
    teams_as_player2 = db.relationship('Team', 
                                     foreign_keys=[Team.player2_uuid],
                                     backref=db.backref('player2_rel', lazy=True))
    # Add relationship to super tournament
    super_tournament = db.relationship('SuperTournament', backref='players', lazy=True)

class Match(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    match_name = db.Column(db.String(50), nullable=False)
    team1_id = db.Column(db.String(50), db.ForeignKey('team.team_id'), nullable=True)
    team2_id = db.Column(db.String(50), db.ForeignKey('team.team_id'), nullable=True)
    round_id = db.Column(db.String(50), nullable=False)
    pool = db.Column(db.String(50), nullable=False)
    winner_team_id = db.Column(db.String(50), nullable=True)
    is_final = db.Column(db.Boolean, default=False)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournament.id'), nullable=False)
    court_number = db.Column(db.Integer)
    court_order = db.Column(db.Integer)
    status = db.Column(db.String(20), default='pending')
    predecessor_1 = db.Column(db.Integer, db.ForeignKey('match.id'), nullable=True)
    predecessor_2 = db.Column(db.Integer, db.ForeignKey('match.id'), nullable=True)
    successor = db.Column(db.Integer, db.ForeignKey('match.id'), nullable=True)
    bracket_position = db.Column(db.Integer, nullable=True)
    round_number = db.Column(db.Integer, nullable=True)
    # HOW the result was decided (see services/match_outcomes.py): 'regular',
    # 'walkover', ... NULL = not decided yet, or a historical match whose
    # outcome could not be established reliably. `status` stays the lifecycle
    # (pending / on-going / completed); this column never replaces it.
    outcome_type = db.Column(db.String(20), nullable=True)

class Score(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    match_id = db.Column(db.Integer, db.ForeignKey('match.id'), nullable=False)
    team_id = db.Column(db.String(50), db.ForeignKey('team.team_id'), nullable=False)
    score = db.Column(db.Integer, nullable=False, default=0)
    points = db.Column(db.Integer, default=0)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournament.id'), nullable=False)

class Round(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    round_id = db.Column(db.Integer, nullable=False)
    team_id = db.Column(db.String(50), db.ForeignKey('team.team_id'), nullable=True)
    pool = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(100), nullable=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournament.id'), nullable=False)
    name = db.Column(db.String(255))