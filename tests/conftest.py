"""Test harness: an isolated in-memory SQLite database. No MySQL, no network,
nothing is ever written to a real database."""
import os

# Must be set before `config` is imported anywhere.
os.environ['DATABASE_URL'] = 'sqlite://'

import pytest
from flask import Flask
from sqlalchemy.pool import StaticPool

from models import db, SuperTournament, Season, Tournament, Team, Match, Score
from routes import initialize_routes
from socket_instance import init_socketio


@pytest.fixture(scope='session')
def app():
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_ENGINE_OPTIONS={'poolclass': StaticPool,
                                   'connect_args': {'check_same_thread': False}},
    )
    db.init_app(app)
    initialize_routes(app)
    init_socketio(app)
    return app


@pytest.fixture(autouse=True)
def _fresh_db(app):
    """Every test gets empty tables inside an app context."""
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def emitted(monkeypatch):
    """Capture Socket.IO emits instead of sending them."""
    events = []
    monkeypatch.setattr('services.match_outcomes.socketio.emit',
                        lambda event, payload, **kw: events.append((event, payload, kw)))
    return events


class World:
    """Tiny builder for tournaments, teams, matches and score rows."""

    def __init__(self):
        st = SuperTournament(name='ST')
        db.session.add(st)
        db.session.flush()
        season = Season(name='S1', super_tournament_id=st.id)
        db.session.add(season)
        db.session.flush()
        self.tournament = Tournament(tournament_name='T', type='elimination', season_id=season.id)
        db.session.add(self.tournament)
        db.session.commit()
        self.tid = self.tournament.id

    def team(self, team_id, tournament_id=None):
        t = Team(team_id=team_id, name=f'Team {team_id}', tournament_id=tournament_id or self.tid)
        db.session.add(t)
        db.session.commit()
        return t

    def match(self, team1, team2, *, round_id='1', pool='A', status='pending',
              scores=(0, 0), with_scores=True, **kw):
        m = Match(match_name=f'{team1} vs {team2}', team1_id=team1, team2_id=team2,
                  round_id=round_id, pool=pool, tournament_id=self.tid, status=status, **kw)
        db.session.add(m)
        db.session.flush()
        if with_scores and team1 not in (None, 'TBD') and team2 not in (None, 'TBD'):
            db.session.add(Score(match_id=m.id, team_id=team1, score=scores[0], tournament_id=self.tid))
            db.session.add(Score(match_id=m.id, team_id=team2, score=scores[1], tournament_id=self.tid))
        db.session.commit()
        return m

    def bracket(self, teams):
        """4-team knockout: SF1(m1) SF2(m2) -> F(m3), built like round_knockout does."""
        for t in teams:
            self.team(t)
        sf1 = self.match(teams[0], teams[1], round_id='2', pool='knockout', round_number=1, bracket_position=0)
        sf2 = self.match(teams[2], teams[3], round_id='2', pool='knockout', round_number=1, bracket_position=1)
        final = self.match('TBD', 'TBD', round_id='3', pool='knockout', round_number=2, bracket_position=0)
        sf1.successor = sf2.successor = final.id
        final.predecessor_1, final.predecessor_2 = sf1.id, sf2.id
        db.session.commit()
        return sf1, sf2, final


@pytest.fixture()
def world():
    return World()


def outcome_url(match_id):
    return f'/matches/{match_id}/outcome'


def walkover(client, match_id, tournament_id, winner, **extra):
    body = {'tournament_id': tournament_id, 'outcome_type': 'walkover', 'winner_team_id': winner}
    body.update(extra)
    return client.post(outcome_url(match_id), json=body)


def score_rows(match_id):
    return Score.query.filter_by(match_id=match_id).all()
