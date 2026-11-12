#!/usr/bin/env python3
"""
Migration: add `outcome_type` to the `match` table (Walkover support).

    python migrations/bul/add_match_outcome_type.py                 # upgrade
    python migrations/bul/add_match_outcome_type.py --no-backfill   # column only
    python migrations/bul/add_match_outcome_type.py --rollback --confirm

What it does
  1. Adds `match.outcome_type VARCHAR(20) NULL` (non-destructive; every
     existing row gets NULL, nothing else is touched).
  2. Backfills 'regular' ONLY for historical matches whose outcome can be
     established reliably: finalized, has a winner, and that winner is the team
     with the strictly higher recorded score. Draws, 0-0 finals, finals without
     a winner and unfinished matches stay NULL ("unknown") - we do not guess.
  3. Verifies the column exists and prints a summary.

It is idempotent: running it twice is safe. The same statements are available
as plain SQL in add_match_outcome_type.sql / rollback_match_outcome_type.sql.

NOTE: app.py's db.create_all() does NOT alter existing tables, so this script
must be run on every existing database before the new backend code is started.
"""

import argparse
import os
import sys

# Add repo root to path to import config (same convention as the other scripts)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from sqlalchemy import create_engine, inspect, text

from config import Config

ADD_COLUMN_SQL = "ALTER TABLE `match` ADD COLUMN `outcome_type` VARCHAR(20) NULL"

# Correlated sub-selects (instead of UPDATE ... JOIN) so the statement is valid
# on MySQL and SQLite alike. MAX() tolerates historical duplicate score rows.
BACKFILL_SQL = """
UPDATE `match`
SET `outcome_type` = 'regular'
WHERE `outcome_type` IS NULL
  AND `is_final` = 1
  AND `winner_team_id` IS NOT NULL
  AND (
        (`winner_team_id` = `team1_id`
         AND (SELECT MAX(s.`score`) FROM `score` s
               WHERE s.`match_id` = `match`.`id` AND s.`team_id` = `match`.`team1_id`)
           > (SELECT MAX(s.`score`) FROM `score` s
               WHERE s.`match_id` = `match`.`id` AND s.`team_id` = `match`.`team2_id`))
     OR (`winner_team_id` = `team2_id`
         AND (SELECT MAX(s.`score`) FROM `score` s
               WHERE s.`match_id` = `match`.`id` AND s.`team_id` = `match`.`team2_id`)
           > (SELECT MAX(s.`score`) FROM `score` s
               WHERE s.`match_id` = `match`.`id` AND s.`team_id` = `match`.`team1_id`))
  )
"""

DROP_COLUMN_SQL = "ALTER TABLE `match` DROP COLUMN `outcome_type`"


def column_exists(engine) -> bool:
    return any(c['name'] == 'outcome_type' for c in inspect(engine).get_columns('match'))


def upgrade(engine, backfill: bool = True) -> None:
    if column_exists(engine):
        print("Column match.outcome_type already exists - skipping ALTER TABLE.")
    else:
        print(f"Executing: {ADD_COLUMN_SQL}")
        with engine.begin() as conn:
            conn.execute(text(ADD_COLUMN_SQL))

    if backfill:
        with engine.begin() as conn:
            result = conn.execute(text(BACKFILL_SQL))
        print(f"Backfilled outcome_type='regular' on {result.rowcount} historical match(es).")
    else:
        print("Backfill skipped (--no-backfill).")

    if not column_exists(engine):
        raise RuntimeError("Verification failed: match.outcome_type is missing after migration")

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT COALESCE(`outcome_type`, '(NULL)') AS outcome, COUNT(*) AS n "
            "FROM `match` GROUP BY `outcome_type` ORDER BY n DESC"
        )).fetchall()
    print("Verified: match.outcome_type exists. Rows per outcome_type:")
    for outcome, n in rows:
        print(f"  {outcome}: {n}")
    print("Migration completed successfully!")


def rollback(engine, confirm: bool) -> None:
    if not column_exists(engine):
        print("Column match.outcome_type does not exist - nothing to roll back.")
        return

    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT COUNT(*) FROM `match` WHERE `outcome_type` IS NOT NULL AND `outcome_type` <> 'regular'"
        )).scalar()
    print(f"{n} match(es) carry a non-regular outcome (e.g. walkover). Dropping the column "
          "loses that marker (winner_team_id / status / is_final are kept, scores stay 0-0).")
    if not confirm:
        print("Dry run only. Re-run with --confirm to drop the column.")
        return

    with engine.begin() as conn:
        conn.execute(text(DROP_COLUMN_SQL))
    if column_exists(engine):
        raise RuntimeError("Rollback verification failed: column still present")
    print("Rollback completed: match.outcome_type dropped.")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--rollback', action='store_true', help='drop the column instead of adding it')
    parser.add_argument('--confirm', action='store_true', help='required together with --rollback')
    parser.add_argument('--no-backfill', action='store_true', help='add the column but leave history as NULL')
    args = parser.parse_args(argv)

    engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
    if args.rollback:
        rollback(engine, confirm=args.confirm)
    else:
        upgrade(engine, backfill=not args.no_backfill)


if __name__ == '__main__':
    main()
