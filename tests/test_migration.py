"""migrations/bul/add_match_outcome_type.py against a legacy-shaped database (SQLite stand-in for MySQL)."""
import importlib.util
import os

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

MIGRATION_PATH = os.path.join(os.path.dirname(__file__), '..', 'migrations', 'bul', 'add_match_outcome_type.py')


@pytest.fixture(scope='module')
def migration():
    spec = importlib.util.spec_from_file_location('add_match_outcome_type', MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def legacy_engine():
    """A database as it exists today: no outcome_type column, some historical matches."""
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    with engine.begin() as c:
        c.execute(text("CREATE TABLE `match` (id INTEGER PRIMARY KEY, team1_id VARCHAR(50), "
                       "team2_id VARCHAR(50), winner_team_id VARCHAR(50), is_final BOOLEAN, status VARCHAR(20))"))
        c.execute(text("CREATE TABLE score (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER, "
                       "team_id VARCHAR(50), score INTEGER)"))
        matches = [  # id, winner, is_final, (score A, score B)
            (1, 'A', 1, (11, 7)),    # reliable regular
            (2, 'B', 1, (5, 11)),    # reliable regular (team2 won)
            (3, None, 1, (5, 5)),    # draw -> unknown
            (4, None, 1, (0, 0)),    # final, no winner, 0-0 -> unknown
            (5, None, 0, (3, 2)),    # not final -> unknown
            (6, 'A', 1, (0, 0)),     # winner without scores -> unknown (could be anything)
            (8, 'B', 1, (11, 7)),    # winner contradicts scores -> unknown
        ]
        for mid, winner, final, (sa, sb) in matches:
            c.execute(text("INSERT INTO `match` VALUES (:i, 'A', 'B', :w, :f, 'completed')"),
                      {'i': mid, 'w': winner, 'f': final})
            c.execute(text("INSERT INTO score (match_id, team_id, score) VALUES (:i, 'A', :s)"), {'i': mid, 's': sa})
            c.execute(text("INSERT INTO score (match_id, team_id, score) VALUES (:i, 'B', :s)"), {'i': mid, 's': sb})
        # 7: reliable regular even with a stale duplicate score row for A
        c.execute(text("INSERT INTO `match` VALUES (7, 'A', 'B', 'A', 1, 'completed')"))
        for team, s in (('A', 11), ('A', 0), ('B', 7)):
            c.execute(text("INSERT INTO score (match_id, team_id, score) VALUES (7, :t, :s)"), {'t': team, 's': s})
    return engine


def outcomes(engine):
    with engine.connect() as c:
        return dict(c.execute(text("SELECT id, outcome_type FROM `match` ORDER BY id")).fetchall())


def test_upgrade_adds_nullable_column_and_backfills_only_reliable_history(migration, legacy_engine):
    before = {c['name'] for c in inspect(legacy_engine).get_columns('match')}
    assert 'outcome_type' not in before
    with legacy_engine.connect() as c:
        rows_before = c.execute(text("SELECT COUNT(*) FROM `match`")).scalar()

    migration.upgrade(legacy_engine)

    col = next(c for c in inspect(legacy_engine).get_columns('match') if c['name'] == 'outcome_type')
    assert col['nullable'] is True and 'VARCHAR(20)' in str(col['type']).upper()
    assert outcomes(legacy_engine) == {1: 'regular', 2: 'regular', 3: None, 4: None,
                                       5: None, 6: None, 7: 'regular', 8: None}
    with legacy_engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM `match`")).scalar() == rows_before   # nothing lost


def test_upgrade_is_idempotent(migration, legacy_engine):
    migration.upgrade(legacy_engine)
    first = outcomes(legacy_engine)
    migration.upgrade(legacy_engine)
    assert outcomes(legacy_engine) == first


def test_upgrade_never_overwrites_an_existing_outcome(migration, legacy_engine):
    migration.upgrade(legacy_engine, backfill=False)
    with legacy_engine.begin() as c:
        c.execute(text("UPDATE `match` SET outcome_type='walkover' WHERE id=1"))
    migration.upgrade(legacy_engine)          # backfill runs again
    assert outcomes(legacy_engine)[1] == 'walkover'


def test_no_backfill_flag_leaves_history_null(migration, legacy_engine):
    migration.upgrade(legacy_engine, backfill=False)
    assert set(outcomes(legacy_engine).values()) == {None}


def test_rollback_is_dry_run_without_confirm_and_drops_with_it(migration, legacy_engine):
    migration.upgrade(legacy_engine)
    migration.rollback(legacy_engine, confirm=False)
    assert any(c['name'] == 'outcome_type' for c in inspect(legacy_engine).get_columns('match'))

    migration.rollback(legacy_engine, confirm=True)
    assert all(c['name'] != 'outcome_type' for c in inspect(legacy_engine).get_columns('match'))
    with legacy_engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM `match`")).scalar() == 8     # rows survive the rollback
    migration.rollback(legacy_engine, confirm=True)                                # second rollback is a no-op


def test_dump_loader_converts_mysql_string_escapes():
    """scripts/load_dump_sqlite.py (dev tool): MySQL backslash-escaped quotes and backslashes
    become standard SQL (a doubled quote, a single backslash)."""
    path = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'load_dump_sqlite.py')
    spec = importlib.util.spec_from_file_location('load_dump_sqlite', path)
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    convert = loader.convert_mysql_string_escapes
    bs = chr(92)   # a backslash, spelled out to keep the escaping unambiguous
    assert convert("(1,'Men" + bs + "'s Singles~Delhi')") == "(1,'Men''s Singles~Delhi')"
    assert convert("('a" + bs + bs + "b')") == "('a" + bs + "b')"
    assert convert("(1,2,'plain')") == "(1,2,'plain')"
