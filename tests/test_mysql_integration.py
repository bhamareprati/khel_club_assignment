"""
Real-MySQL checks for what SQLite cannot prove: the SELECT ... FOR UPDATE row locking
under concurrent requests, and the migration on InnoDB.

Opt-in. Point MYSQL_TEST_URL at a MySQL 8 server whose user may CREATE/DROP databases;
the tests create and drop their own scratch schemas (`<name>` and `<name>_migration`):

    $env:MYSQL_TEST_URL = "mysql://root@127.0.0.1:3307/khel_test"
    python -m pytest -q tests/test_mysql_integration.py

Without the variable every test here is skipped (the default suite stays offline).
"""
import importlib.util
import os
import threading

import pytest
from flask import Flask
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

MYSQL_TEST_URL = os.environ.get('MYSQL_TEST_URL')
pytestmark = pytest.mark.skipif(not MYSQL_TEST_URL, reason='set MYSQL_TEST_URL to run MySQL integration tests')

if MYSQL_TEST_URL:
    from models import db, SuperTournament, Season, Tournament, Team, Match, Score
    import services.match_outcomes as outcomes

THREADS = 8


def _server_engine():
    """Connection to the server itself (no default database) for CREATE/DROP DATABASE.
    Note URL.set(database=None) means "unchanged", hence _replace."""
    return create_engine(make_url(MYSQL_TEST_URL)._replace(database=None))


@pytest.fixture(scope='module')
def mysql_app():
    schema = make_url(MYSQL_TEST_URL).database
    admin = _server_engine()
    with admin.begin() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS `{schema}`'))
        c.execute(text(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4'))
    app = Flask('mysql_it')
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI=MYSQL_TEST_URL,
                      SQLALCHEMY_ENGINE_OPTIONS={'pool_size': THREADS + 2, 'pool_pre_ping': True})
    db.init_app(app)
    with app.app_context():
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    with admin.begin() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS `{schema}`'))


@pytest.fixture()
def seeded(mysql_app):
    """Empty tables + a 4-team knockout (SF1, SF2 -> Final) built the way round_knockout builds it."""
    with mysql_app.app_context():
        db.session.execute(text('SET FOREIGN_KEY_CHECKS=0'))
        for table in reversed(db.metadata.sorted_tables):
            db.session.execute(text(f'DELETE FROM `{table.name}`'))
        db.session.execute(text('SET FOREIGN_KEY_CHECKS=1'))
        db.session.commit()

        st = SuperTournament(name='ST'); db.session.add(st); db.session.flush()
        season = Season(name='S', super_tournament_id=st.id); db.session.add(season); db.session.flush()
        t = Tournament(tournament_name='T', type='elimination', season_id=season.id)
        db.session.add(t); db.session.flush()
        for team in 'ABCD':
            db.session.add(Team(team_id=team, name=team, tournament_id=t.id))
        # the knockout builder stores the literal 'TBD' in unfilled slots: the FK needs that team to exist
        db.session.add(Team(team_id='TBD', name='TBD', tournament_id=t.id))
        db.session.flush()

        def match(t1, t2, rnd):
            m = Match(match_name=f'{t1}-{t2}', team1_id=t1, team2_id=t2, round_id=rnd, pool='knockout',
                      tournament_id=t.id, status='pending')
            db.session.add(m); db.session.flush()
            if 'TBD' not in (t1, t2):
                for team in (t1, t2):
                    db.session.add(Score(match_id=m.id, team_id=team, score=0, tournament_id=t.id))
            return m

        sf1, sf2, final = match('A', 'B', '2'), match('C', 'D', '2'), match('TBD', 'TBD', '3')
        sf1.successor = sf2.successor = final.id
        final.predecessor_1, final.predecessor_2 = sf1.id, sf2.id
        db.session.commit()
        yield {'tid': t.id, 'sf1': sf1.id, 'sf2': sf2.id, 'final': final.id}


def _run_parallel(app, jobs):
    """Run callables at the same moment, each in its own app context (= its own DB session/connection)."""
    barrier = threading.Barrier(len(jobs))
    results = [None] * len(jobs)

    def worker(i, job):
        with app.app_context():
            barrier.wait()
            try:
                results[i] = ('ok', job())
            except Exception as exc:  # noqa: BLE001 - we assert on the type below
                results[i] = ('err', exc)

    threads = [threading.Thread(target=worker, args=(i, j)) for i, j in enumerate(jobs)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    assert all(r is not None for r in results), 'a worker did not finish (deadlock / lock wait?)'
    return results


def test_walkover_roundtrip_on_mysql(mysql_app, seeded):
    with mysql_app.app_context():
        declared = outcomes.declare_outcome(seeded['sf1'], seeded['tid'], 'walkover', 'B')
        assert declared.changed
        db.session.expire_all()
        m, final = db.session.get(Match, seeded['sf1']), db.session.get(Match, seeded['final'])
        assert (m.outcome_type, m.winner_team_id, m.status, bool(m.is_final)) == ('walkover', 'B', 'completed', True)
        assert (final.team1_id, final.team2_id) == ('B', 'TBD')


def test_identical_concurrent_walkovers_record_once(mysql_app, seeded):
    """FOR UPDATE: N simultaneous identical requests -> exactly one writes, the rest are replays."""
    jobs = [lambda: outcomes.declare_outcome(seeded['sf1'], seeded['tid'], 'walkover', 'A')] * THREADS
    results = _run_parallel(mysql_app, jobs)

    assert [r[0] for r in results] == ['ok'] * THREADS, [r for r in results if r[0] == 'err']
    assert sum(1 for _, d in results if d.changed) == 1
    with mysql_app.app_context():
        assert db.session.get(Match, seeded['final']).team1_id == 'A'
        assert Score.query.filter_by(match_id=seeded['sf1']).count() == 2


def test_conflicting_concurrent_walkovers_only_one_wins(mysql_app, seeded):
    def declare(winner):
        d = outcomes.declare_outcome(seeded['sf1'], seeded['tid'], 'walkover', winner)
        return d.changed, d.match.winner_team_id     # read inside the worker's session

    winners = ['A', 'B'] * (THREADS // 2)
    results = _run_parallel(mysql_app, [(lambda w=w: declare(w)) for w in winners])

    ok = [v for kind, v in results if kind == 'ok']
    errors = [v for kind, v in results if kind == 'err']
    # the winner is whoever got the lock first; same-winner requests are replays (200), the others conflict (409)
    first_winner = next(w for changed, w in ok if changed)
    assert sum(1 for changed, _ in ok if changed) == 1
    assert all(w == first_winner for _, w in ok)
    assert len(ok) == THREADS // 2 and len(errors) == THREADS // 2
    assert all(isinstance(e, outcomes.OutcomeConflict) and e.code == 'already_completed' for e in errors), errors
    with mysql_app.app_context():
        assert db.session.get(Match, seeded['sf1']).winner_team_id == first_winner
        assert db.session.get(Match, seeded['final']).team1_id == first_winner


def test_both_semis_advancing_at_once_fill_the_final_with_exactly_two_score_rows(mysql_app, seeded):
    """Both predecessors write the same successor row at the same time; the successor lock
    must serialize them, otherwise the final could end up with both teams and no score rows."""
    jobs = [lambda: outcomes.declare_outcome(seeded['sf1'], seeded['tid'], 'walkover', 'A'),
            lambda: outcomes.declare_outcome(seeded['sf2'], seeded['tid'], 'walkover', 'D')]
    for _ in range(1):
        results = _run_parallel(mysql_app, jobs)
    assert [r[0] for r in results] == ['ok', 'ok'], results
    with mysql_app.app_context():
        final = db.session.get(Match, seeded['final'])
        assert (final.team1_id, final.team2_id) == ('A', 'D')
        assert sorted((s.team_id, s.score) for s in Score.query.filter_by(match_id=final.id)) == [('A', 0), ('D', 0)]


def test_failure_rolls_back_on_mysql(mysql_app, seeded, monkeypatch):
    def boom(match):
        succ = db.session.get(Match, match.successor)
        succ.team1_id = match.winner_team_id
        raise RuntimeError('simulated failure')

    monkeypatch.setattr(outcomes, 'advance_winner', boom)
    with mysql_app.app_context():
        with pytest.raises(RuntimeError):
            outcomes.declare_outcome(seeded['sf1'], seeded['tid'], 'walkover', 'A')
        db.session.expire_all()
        sf1, final = db.session.get(Match, seeded['sf1']), db.session.get(Match, seeded['final'])
        assert (sf1.outcome_type, sf1.status, bool(sf1.is_final)) == (None, 'pending', False)
        assert (final.team1_id, final.team2_id) == ('TBD', 'TBD')


# ---- migration on InnoDB ---------------------------------------------------------------

@pytest.fixture()
def legacy_mysql_engine():
    """A scratch schema shaped like the provided dump (no outcome_type) with historical matches."""
    schema = make_url(MYSQL_TEST_URL).database + '_migration'
    admin = _server_engine()
    with admin.begin() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS `{schema}`'))
        c.execute(text(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4'))
    engine = create_engine(make_url(MYSQL_TEST_URL).set(database=schema))
    with engine.begin() as c:
        c.execute(text("CREATE TABLE `match` (`id` int NOT NULL AUTO_INCREMENT, `team1_id` varchar(50) DEFAULT NULL, "
                       "`team2_id` varchar(50) DEFAULT NULL, `winner_team_id` varchar(50) DEFAULT NULL, "
                       "`is_final` tinyint(1) DEFAULT NULL, `status` varchar(20) DEFAULT NULL, PRIMARY KEY (`id`)) ENGINE=InnoDB"))
        c.execute(text("CREATE TABLE `score` (`id` int NOT NULL AUTO_INCREMENT, `match_id` int NOT NULL, "
                       "`team_id` varchar(50) NOT NULL, `score` int NOT NULL DEFAULT 0, PRIMARY KEY (`id`)) ENGINE=InnoDB"))
        rows = [(1, 'A', 1, (11, 7)), (2, 'B', 1, (5, 11)), (3, None, 1, (5, 5)), (4, None, 1, (0, 0)),
                (5, None, 0, (3, 2)), (6, 'A', 1, (0, 0)), (8, 'B', 1, (11, 7))]
        for mid, winner, final, (sa, sb) in rows:
            c.execute(text("INSERT INTO `match` VALUES (:i,'A','B',:w,:f,'completed')"), {'i': mid, 'w': winner, 'f': final})
            c.execute(text("INSERT INTO `score` (match_id, team_id, score) VALUES (:i,'A',:s)"), {'i': mid, 's': sa})
            c.execute(text("INSERT INTO `score` (match_id, team_id, score) VALUES (:i,'B',:s)"), {'i': mid, 's': sb})
        c.execute(text("INSERT INTO `match` VALUES (7,'A','B','A',1,'completed')"))
        for team, s in (('A', 11), ('A', 0), ('B', 7)):   # stale duplicate score row for A
            c.execute(text("INSERT INTO `score` (match_id, team_id, score) VALUES (7,:t,:s)"), {'t': team, 's': s})
    yield engine
    engine.dispose()
    with admin.begin() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS `{schema}`'))


def _migration_module():
    path = os.path.join(os.path.dirname(__file__), '..', 'migrations', 'bul', 'add_match_outcome_type.py')
    spec = importlib.util.spec_from_file_location('add_match_outcome_type_mysql', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_on_mysql_matches_the_sqlite_semantics(legacy_mysql_engine):
    migration = _migration_module()
    migration.upgrade(legacy_mysql_engine)

    col = next(c for c in inspect(legacy_mysql_engine).get_columns('match') if c['name'] == 'outcome_type')
    assert col['nullable'] is True and 'VARCHAR(20)' in str(col['type']).upper()
    with legacy_mysql_engine.connect() as c:
        got = dict(c.execute(text('SELECT id, outcome_type FROM `match` ORDER BY id')).fetchall())
    assert got == {1: 'regular', 2: 'regular', 3: None, 4: None, 5: None, 6: None, 7: 'regular', 8: None}

    migration.upgrade(legacy_mysql_engine)            # idempotent
    migration.rollback(legacy_mysql_engine, confirm=False)
    assert any(c['name'] == 'outcome_type' for c in inspect(legacy_mysql_engine).get_columns('match'))
    migration.rollback(legacy_mysql_engine, confirm=True)
    assert all(c['name'] != 'outcome_type' for c in inspect(legacy_mysql_engine).get_columns('match'))
    with legacy_mysql_engine.connect() as c:
        assert c.execute(text('SELECT COUNT(*) FROM `match`')).scalar() == 8


def test_plain_sql_file_runs_on_mysql(legacy_mysql_engine):
    """migrations/bul/add_match_outcome_type.sql, statement by statement, as `mysql < file` would."""
    sql_path = os.path.join(os.path.dirname(__file__), '..', 'migrations', 'bul', 'add_match_outcome_type.sql')
    body = '\n'.join(l for l in open(sql_path, encoding='utf-8').read().splitlines() if not l.strip().startswith('--'))
    statements = [s.strip() for s in body.split(';') if s.strip()]
    with legacy_mysql_engine.begin() as c:
        for stmt in statements:
            if 'INFORMATION_SCHEMA' in stmt:
                continue          # the verification SELECT needs DATABASE(); checked below
            c.execute(text(stmt))
    with legacy_mysql_engine.connect() as c:
        regular = c.execute(text("SELECT COUNT(*) FROM `match` WHERE outcome_type='regular'")).scalar()
        info = c.execute(text("SELECT COLUMN_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                              "AND TABLE_NAME='match' AND COLUMN_NAME='outcome_type'")).fetchone()
    assert regular == 3
    assert tuple(info) == ('varchar(20)', 'YES')
