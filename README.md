# Khel Club tournament backend: Walkover match outcome

Flask + Flask-SQLAlchemy + Flask-SocketIO backend of the Khel Club pickleball tournament system (v0).
This branch (`feature/walkover-match-outcome`) adds a **Walkover** match outcome, designed so that
**Forfeit**, **Bye** and similar outcomes can be added later without refactoring.
The matching UI work is in the frontend repository (`v0_frontend`, same branch name).

Contents: [1 Overview](#1-overview) · [2 How scoring works today](#2-how-scoring-worked-before-this-change) ·
[3 Problem](#3-why-a-walkover-cannot-be-a-score) · [4 Design](#4-solution-architecture) ·
[5 Backend changes](#5-backend-changes) · [6 Frontend changes](#6-frontend-changes-summary) ·
[7 Database](#7-database-changes) · [8 API](#8-api) · [9 Setup](#9-setup-and-running) ·
[10 Testing](#10-testing) · [11 Assumptions](#11-assumptions) · [12 Future work](#12-what-i-would-improve-with-more-time) ·
[13 Known limitations](#13-known-limitations)

---

## 1. Overview

The system manages pickleball tournaments: pools (round robin), knockout brackets, referee score entry,
standings, exports and a live score overlay. Until now a match could only end by comparing two numbers.
A **walkover** is a result where the match is awarded to one team because the opponent did not play.
This branch records that explicitly (`match.outcome_type = 'walkover'`), finalizes the match, advances the
winner in knockout brackets, counts it correctly in standings, and exposes it to the UI and the live overlay.

## 2. How scoring worked before this change

(All of this was read from the code, not assumed.)

| Topic | Behaviour |
|---|---|
| **Match** (`models.py`) | `team1_id`/`team2_id` (strings such as `T001`; knockout placeholders are the literal string `'TBD'`), `winner_team_id`, `is_final`, `status` (`pending` / `on-going` / `completed`), `pool`, `round_id`, and for knockouts `predecessor_1/2`, `successor`, `bracket_position`, `round_number`. |
| **Score storage** | Table `score`: **one row per (match, team)** with an integer `score`. Rows are created with `0` when a match is created (`/create-match`, round/pool fixture generation, knockout builder when both teams are known). There is no per-game history. |
| **Score API** | `POST /update-score {match_id, tournament_id, score:"a-b", final}` upserts the two rows. With `final=true` it sets `is_final`, picks the winner by comparing the numbers (a draw gives `winner_team_id = NULL`) and, for knockout matches, calls `update_successor_match`, which writes the winner into the successor's `team1` (if this match is its `predecessor_1`) or `team2` (`predecessor_2`) and creates the successor's zero score rows once both teams are known. |
| **Status** | Changed only by `POST /update-match-status/<id>` (validated against the three values, no transition rules; `completed` also forces `is_final = true`). `update-score` never touched `status`: the referee screen fired **both requests concurrently** to finalize a match. |
| **Completed-match protection** | None on the backend. The UI sends an `override` flag the backend ignores and shows an "Override score?" dialog for completed matches. |
| **Standings** | Computed on every request from the score rows, in **five** places with the same idea: `/standings`, `/overall-standings`, `/second-place-standings` (raw SQL join of the two score rows; 0-0 means "not played") and `get_round_standings` / `get_pool_standings` (round completion and knockout seeding). A win is `score_a > score_b`; `total_scores = wins * 2`; ties broken by point difference. |
| **Results to clients** | `GET /get-match-fixtures` (`match_result` is the string `"a-b"` plus `match_status{winner_team_id,is_final,status}`), `GET /score/match`, `GET /match-ops/pools/<pool>/fixtures`, two CSV exports (fixtures, DUPR report: finalized matches only). |
| **Live updates** | Socket.IO namespace `/scores`; `update-score` broadcasts the event `score_update` (scores + `is_final`) to every client; the overlay page listens for it. |
| **Migrations** | No Alembic. Raw SQL plus Python helper scripts in `migrations/bul/`. `db.create_all()` at start-up **only creates missing tables; it never alters existing ones.** |
| **Auth** | The backend has no authentication/authorization layer, so there is no 403 path. |

Defects I found while reading (relevant to this work, not unrelated refactors):
`update_successor_match` committed internally and **re-created successor score rows on every call**
(re-finalizing a match duplicated them; on the first predecessor it also wrote rows for the `'TBD'` slot);
the two concurrent finalize requests were an unsynchronised race; nothing prevented a match from being
marked completed without a result.

## 3. Why a walkover cannot be a score

* `0-0` is already the value of every match that has **not been played**, so a score cannot tell
  "walkover" from "not started".
* Putting a fake score such as `11-0` in the rows would invent points: it distorts point difference,
  the `/points` reports and the DUPR export, and the data could never be told apart from a real result later.
* The winner of a walkover is a **decision**, not something derivable from numbers. Every consumer
  (five standings implementations, fixtures, CSV, overlay) that compares scores would get it wrong.

So the result needs its own explicit field.

## 4. Solution architecture

**State and outcome are separate.**

| | status (lifecycle, unchanged) | outcome_type (new: *how* it was decided) |
|---|---|---|
| Scheduled | `pending` | `NULL` |
| Being played | `on-going` | `NULL` |
| Finished by score | `completed` | `regular` |
| Finished by walkover | `completed` | `walkover` |

`NULL` also means "historical match whose outcome was never recorded". Nothing assumes it is regular.

**One module owns the rules: [`services/match_outcomes.py`](services/match_outcomes.py).**

* An **outcome registry** (`OUTCOMES`) describes each outcome with `uses_scores`, `declarable`, `requires_winner`
  and a label. Everything else asks the registry (`uses_scores()`, `result_label()`, `decided_result()`)
  instead of comparing strings, so there is no scattered `if walkover`.
* `finalize_regular_match()` (used by `/update-score`), `declare_outcome()` (used by the new endpoint) and
  `advance_winner()` (knockout progression, shared by both) are the only places that write results.
* **Transactions:** nothing in the service commits except `declare_outcome`, which commits **once** after
  the outcome, any missing zero score rows and the bracket advance are all written, and rolls back on any
  error. `/update-score` also commits once. The old internally-committing `update_successor_match` is gone.
  `advance_winner` is idempotent: same slot value, no duplicate score rows, never advances a null/`TBD` winner.
* **Idempotency/concurrency:** the match row is read with `SELECT ... FOR UPDATE` (InnoDB). An identical
  repeated request returns `200` ("already recorded") and changes/broadcasts nothing; a different one is a `409`.
* Live events are emitted only **after** the commit, and a walkover event carries **no scores**.

**Adding Forfeit later** is one registry entry (a test, `test_adding_a_new_declarable_outcome_needs_only_a_registry_entry`,
proves the claim end to end: accepted by the endpoint, labelled in fixtures, counted in standings, locked
against score edits):

```python
FORFEIT = OutcomeDefinition('forfeit', 'Forfeit', uses_scores=False, declarable=True, requires_winner=True)
OUTCOMES = {o.name: o for o in (REGULAR, WALKOVER, FORFEIT)}
```

A **Bye** (one team, no opponent) would need one additional validator (single team, auto-winner) but reuses the
registry, finalization and bracket progression. I did not implement it (out of scope).

## 5. Backend changes

| File | Change |
|---|---|
| `models.py` | `Match.outcome_type` (`String(20)`, nullable). |
| `config.py` | Optional `DATABASE_URL` override (unset = unchanged MySQL URI). Used for the local SQLite run and the tests. |
| `services/match_outcomes.py` | **New.** Registry, validation, `declare_outcome`, `finalize_regular_match`, `advance_winner`, `ensure_score_rows`, `decided_result`, `result_label`, event helper. |
| `routes/match/match_outcome.py` | **New.** `POST /matches/<id>/outcome`. |
| `routes/match/__init__.py` | Registers the new route module. |
| `routes/score/score_core.py` | `update_score` finalizes through the service (sets `outcome_type='regular'` and `status='completed'`, single commit), rejects score edits on walkover matches (409), response/event gain `status`, `outcome_type`, `winner_team_id` (additive). `get_match_score` returns `outcome_type`. `update_successor_match` removed (moved to the service, now non-committing). |
| `routes/match/match_core.py` | `update_match_status`: a walkover match can never be re-opened (409); `completed` is idempotent; response includes `outcome_type`. Regular matches behave as before. |
| `routes/match/match_fixtures.py` | `match_result` is `"Walkover"` for unscored outcomes, `match_status.outcome_type` added; fixtures CSV prints `Walkover`. |
| `routes/match_ops/fixtures.py` | Pool fixtures return `outcome_type`. |
| `routes/tournament/tournament_core.py` | Three standings endpoints credit the stored winner of an unscored match. |
| `routes/round/round_completion.py`, `routes/round/round_knockout.py` | Same for `get_round_standings` and `get_pool_standings`. |
| `routes/tournament/tournament_export.py` | DUPR export skips matches decided without scores. |
| `migrations/bul/add_match_outcome_type.{py,sql}`, `rollback_match_outcome_type.sql` | **New** migration + rollback. |
| `scripts/load_dump_sqlite.py` | **New, dev only.** Loads the provided MySQL dump into SQLite. |
| `tests/`, `pytest.ini`, `requirements-dev.txt` | **New** test suite. |

## 6. Frontend changes (summary)

Full list in the `v0_frontend` README. In short: a **Declare Walkover** button + confirmation dialog on the referee
match screen (`Match.js`), the completed-walkover view, a **Walkover** badge and winner display in `MatchTable`,
referee table, fixture cards, bracket views and the live overlay, and a new endpoint entry in `store/urls.js`.
The referee screens no longer send the redundant concurrent `/update-match-status` request when finalizing.

## 7. Database changes

One additive column, **no destructive change**:

```sql
ALTER TABLE `match` ADD COLUMN `outcome_type` VARCHAR(20) NULL;
```

`db.create_all()` will **not** add it to an existing database, so run the migration **before** starting the
new backend on any existing database (the code selects this column).

```bash
# Option A: Python script (idempotent, verifies, prints a summary). Uses DB_* env vars from .env
python migrations/bul/add_match_outcome_type.py
python migrations/bul/add_match_outcome_type.py --no-backfill      # column only

# Option B: plain SQL
mysql -h <host> -u <user> -p <database> < migrations/bul/add_match_outcome_type.sql
```

**Backfill policy.** Existing rows get `NULL`. Only matches whose outcome can be established reliably become
`'regular'`: finalized, has a winner, and the winner has the **strictly higher recorded score**. Draws, 0-0 finals,
winner without scores, winner contradicting the scores and unfinished matches stay `NULL`. The statement
never overwrites an existing value and tolerates historical duplicate score rows (`MAX` per team).

**Rollback** (confirm-gated; dropping the column loses the walkover marker, so back up first):

```bash
python migrations/bul/add_match_outcome_type.py --rollback            # dry run: reports what would be lost
python migrations/bul/add_match_outcome_type.py --rollback --confirm
# or: mysql ... < migrations/bul/rollback_match_outcome_type.sql
```

Redeploy the previous backend build with/before a rollback, because the new code reads the column.

**Execution status.** Verified on a real **MySQL 8.0.40 Community Server** (InnoDB, default strict `sql_mode`; the
official Windows zip, run locally on 127.0.0.1) and on SQLite:

* The provided dump was loaded into two scratch databases. **Option A** (`python migrations/bul/add_match_outcome_type.py`,
  `DB_*` environment variables) added `outcome_type varchar(20) NULL`, left all 6 existing matches `NULL`, and a second run was a
  no-op. **Option B** (`mysql < migrations/bul/add_match_outcome_type.sql`) gave the same column and result.
* `tests/test_mysql_integration.py` (opt-in, section 10) runs the backfill rules, idempotency, the confirm-gated rollback and
  the plain `.sql` file against a legacy-shaped InnoDB schema: same outcomes as on SQLite.
* On SQLite the same script was also exercised against the dump's data and in 5 automated tests.

Still worth doing before production: run it on a **copy of the production database** (different server version, data volume,
other schema drift than the dump).

## 8. API

### New: `POST /matches/<match_id>/outcome`

Registered on the existing `match` blueprint (no prefix, like the neighbouring routes).

```http
POST /matches/25/outcome
Content-Type: application/json

{ "tournament_id": 1, "outcome_type": "walkover", "winner_team_id": "T001" }
```

`200 OK`

```json
{
  "match_id": 25, "tournament_id": 1, "status": "completed", "is_final": true,
  "outcome_type": "walkover", "winner_team_id": "T001",
  "message": "Walkover recorded successfully"
}
```

Repeating the identical request returns `200` with `"message": "Walkover was already recorded"` and does nothing else.

Errors are always `{"error": "<message>", "code": "<machine_code>"}`; stack traces are never returned (logged server-side).

| Status | When (`code`) |
|---|---|
| 400 | body is not a JSON object (`invalid_json`); `outcome_type` missing/unknown/not declarable (`invalid_outcome_type`); `tournament_id` missing/not an integer (`invalid_tournament_id`); `winner_team_id` missing/blank (`winner_required`/`invalid_winner`); winner is not one of the two teams (`invalid_winner`) |
| 404 | tournament not found (`tournament_not_found`); match not found (`match_not_found`); match belongs to another tournament (`match_not_in_tournament`) |
| 409 | a team is not assigned yet / `TBD` (`teams_not_assigned`); match already completed, or completed with a different result (`already_completed`); scores already recorded, so it was played (`scores_recorded`); bracket data inconsistent (`bracket_inconsistent`) |
| 500 | unexpected error, everything rolled back (`internal_error`) |

(403 is not used: there is no auth layer in this codebase.)

### Changed (existing contracts only gain fields, plus the two guards noted)

| Endpoint | Change |
|---|---|
| `GET /get-match-fixtures` | `match_status.outcome_type` added. **`match_result` is the string `"Walkover"`** for a walkover instead of `"0-0"` (this is the one value change: any client that parses `match_result` as `"a-b"` must tolerate it; the bundled frontend does). Winner is `match_status.winner_team_id`. |
| `GET /score/match` | `outcome_type` added. |
| `POST /update-score` | Response/event add `status`, `outcome_type`, `winner_team_id`. `final=true` now also sets `status='completed'`, `outcome_type='regular'`. **New 409** (`outcome_locked`) on a walkover match. |
| `POST /update-match-status/<id>` | **New 409** (`outcome_locked`) when trying to move a walkover match out of `completed`. |
| `GET /match-ops/pools/<pool>/fixtures` | `outcome_type` added. |
| `GET /get-match-fixtures/csv` | Result column is `Walkover` for a walkover. |
| `GET /export-tournament-csv` (DUPR) | Walkovers are **not exported** (no match was played). |
| `/standings`, `/overall-standings`, `/second-place-standings` | Walkover counted per the policy in section 11. |

### Live updates

Same Socket.IO event `score_update` on namespace `/scores`, emitted after the commit:

```json
{ "message": "Walkover recorded successfully", "match_id": 25, "tournament_id": 1,
  "team1_id": "T001", "team2_id": "T002", "status": "completed", "is_final": true,
  "outcome_type": "walkover", "winner_team_id": "T001" }
```

No `team1_score`/`team2_score` keys: a walkover has no score.

## 9. Setup and running

Requirements: Python 3.12 (the machine used here had 3.12.13 via `uv`), MySQL 8 for a real deployment.

```bash
python -m venv .venv
.venv\Scripts\activate                      # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt             # includes mysqlclient (needs MySQL client libraries / a compiler on some systems)
pip install pytest                          # or: pip install -r requirements-dev.txt
```

**With MySQL** (as the project is meant to run):

```bash
# 1. create a database and load the provided dump
mysql -u <user> -p -e "CREATE DATABASE khel"; mysql -u <user> -p khel < dump-test-202512221924.sql
# 2. .env (git-ignored) next to app.py:  DB_HOST=... DB_USER=... DB_PASSWORD=... DB_NAME=khel DB_PORT=3306
# 3. migrate, then run
python migrations/bul/add_match_outcome_type.py
python app.py                               # http://localhost:5001
```

* `.env` values are interpolated into the URI as-is, so **set `DB_PASSWORD` even if it is empty** (an unset variable becomes the
  literal password `None`; this is how `config.py` already behaved). Note PowerShell cannot hold an empty variable: use `.env`.
* The dump contains `SET @@GLOBAL.GTID_PURGED=...`, which a server without GTIDs rejects; delete that line (and the two
  `SQL_LOG_BIN` lines) from a copy before loading it into a local MySQL.
* **Pre-existing, unrelated to this change:** `POST /knockout` stores the literal team id `'TBD'` in unfilled slots, and
  `match.team1_id/team2_id` are foreign keys to `team`, so on MySQL a team row `TBD` must exist or the insert fails with
  error 1452 (SQLite does not enforce foreign keys, so the demo below does not need it). I verified this on the dump.
  Inserting a placeholder team row (`INSERT INTO team (team_id,name,tournament_id) VALUES ('TBD','TBD',1)`) fixes it, and that is what I did for the MySQL run.

**Without MySQL (what I used for the browser demo):**

```powershell
# use an ABSOLUTE path in DATABASE_URL (Flask-SQLAlchemy resolves relative SQLite paths against ./instance)
python scripts/load_dump_sqlite.py dump-test-202512221924.sql D:/work/dev.db
$env:DATABASE_URL = "sqlite:///D:/work/dev.db"      # bash: export DATABASE_URL=sqlite:///D:/work/dev.db
python migrations/bul/add_match_outcome_type.py
python app.py
```

The frontend (`v0_frontend`) talks to `http://localhost:5001` automatically when served from `localhost`.

## 10. Testing

```bash
python -m pytest -q
```

Tests run on an in-memory SQLite database (nothing touches a real database, no network).
They are grouped as: `tests/test_walkover_api.py` (endpoint, validation, idempotency, bracket, rollback, live events, score/status guards, regression),
`tests/test_walkover_results.py` (standings, fixtures, CSVs, registry/extensibility), `tests/test_migration.py`.

### Results (run on 2026-11-12; Python 3.12.13, Flask 3.1.3, SQLAlchemy 2.1.8, pytest 9.2.0)

```
python -m pytest -q
68 passed
```

| Assignment case | Test(s) |
|---|---|
| 1 Regular match 11-7 | `test_regular_final_score_sets_winner_outcome_and_status` (+ non-final, draw, override, bad-input regressions) |
| 2, 3 Walkover for team A / B | `test_walkover_for_either_team[A]`, `[B]`; from `on-going`; zero score rows only |
| 4 Invalid winner, no mutation | `test_invalid_winner_rejected_without_mutation`, `test_missing_or_malformed_winner_is_400` |
| 5 Missing match | `test_missing_match_is_404` (+ missing tournament, match of another tournament) |
| 6 Invalid outcome | `test_invalid_outcome_type_is_400`, `test_invalid_tournament_id_is_400`, `test_non_json_body_is_400` |
| 7 Already completed | `test_completed_regular_match_conflicts`, `test_walkover_with_different_winner_conflicts`, `test_walkover_rejected_when_scores_already_recorded`, `test_teams_must_be_assigned` |
| 8 Repeated submission | `test_repeated_identical_walkover_is_idempotent` (one broadcast only), `test_repeat_after_both_slots_filled_creates_no_duplicate_score_rows` |
| 9 Knockout advancement | `test_walkover_winner_advances_to_correct_slots` (slot 1 and 2), final round, walkover then regular final, no duplicates on regular override |
| 10 Pool standings | `test_pool_standings_credit_walkover_win_without_points`, overall/second-place, `get_round_standings`/`get_pool_standings`, duplicate-row guard |
| 11 Transaction rollback | `test_failure_during_advancement_rolls_everything_back` (crash after a half-applied advance: nothing persisted, no event, no internals in the 500 body), `test_inconsistent_bracket_rolls_back_with_409` |
| 12 Existing scoring regression | the regular-scoring tests above, `test_status_endpoint_unchanged_for_regular_matches`, `test_unfinished_zero_zero_matches_still_not_counted` |
| Live events | `test_socket_event_after_commit_without_scores`, `test_no_event_for_rejected_requests`, `test_regular_final_event_still_carries_scores` |
| Extensibility | `test_adding_a_new_declarable_outcome_needs_only_a_registry_entry` |
| Migration | `tests/test_migration.py` (5 tests + dump-loader escape test) |

Sanity check that the tests are meaningful: with the standings/fixtures changes temporarily reverted, 8 of the 11 tests in `test_walkover_results.py` fail.

### Manual end-to-end run (backend + frontend, real browser)

Backend on the provided dump loaded into SQLite (`dev.db`, migration applied), frontend `npm run dev`; the checks below were done in a browser and confirmed against the database and API:

* Referee screen on a **pending** match: "Match Not Started" prompt has **Declare Walkover**; dialog lists both teams, Confirm is disabled until a winner is chosen, shows "Recording..." while submitting; then the screen shows status Completed, outcome Walkover and the winner highlighted. DB row: `outcome_type='walkover'`, `winner_team_id`, `status='completed'`, `is_final=1`, both score rows still `0`. Reloading the page shows the same result.
* Match table tab: "Walkover" badge + "Winner: <players>"; Standings tab: winner played 1 / won 1 / 2 points / point difference 0, loser played 1 / lost 1.
* Knockout (4 teams): a walkover on SF1 and SF2 filled the final's `team1` and `team2` (final has exactly 2 score rows; replaying the same request changed nothing); the bracket shows `W/O` next to both winners.
* Replay -> `200 "already recorded"`; different winner -> `409`; `update-score` on a walkover match -> `409 outcome_locked`; stale winner choice in the dialog -> the backend error is displayed in the dialog.
* Regular scoring on another match (11-7, Final) still works: `outcome_type='regular'`, winner, `completed`, and the screen returns to the match list as before.
* Live overlay: with the overlay open on a pending match, declaring a walkover from the API updated it from `0 0` to `- W` + `WALKOVER` without a reload.

### Real MySQL 8.0.40 (InnoDB)

The default suite is SQLite-only. `tests/test_mysql_integration.py` runs against a real MySQL server and is skipped unless
`MYSQL_TEST_URL` is set; it creates and drops its own scratch schemas, so give it a user that may `CREATE DATABASE`:

```bash
MYSQL_TEST_URL=mysql://root@127.0.0.1:3307/khel_test python -m pytest -q tests/test_mysql_integration.py
```

Run on 2026-11-12 against MySQL Community Server 8.0.40 (`mysql-8.0.40-winx64.zip` from MySQL's official CDN, MD5 checked against the
published value) with `mysqlclient` 2.3.1: **7 passed** (three consecutive runs, all green):

| Test | What it proves on InnoDB |
|---|---|
| `test_walkover_roundtrip_on_mysql` | outcome fields + successor slot persisted |
| `test_identical_concurrent_walkovers_record_once` | 8 threads (8 sessions) send the same request at the same instant: exactly 1 writes, 7 are replays, no duplicate rows |
| `test_conflicting_concurrent_walkovers_only_one_wins` | 8 threads with two different winners: exactly one result is stored; the losers get `409 already_completed`; the bracket follows the stored winner |
| `test_both_semis_advancing_at_once_fill_the_final_with_exactly_two_score_rows` | the two predecessors write the same successor row simultaneously; the successor lock serializes them (the final ends with both teams **and** exactly 2 score rows) |
| `test_failure_rolls_back_on_mysql` | a crash after a half-applied advance leaves nothing behind |
| `test_migration_on_mysql_matches_the_sqlite_semantics`, `test_plain_sql_file_runs_on_mysql` | the migration, rollback and SQL file on InnoDB |

Sanity check: with `.with_for_update()` removed from the service, the three concurrency tests **fail** (two runs); with it they pass.

The whole app was also run on that server (`python app.py` with `DB_*` pointing at the migrated dump) and exercised over HTTP:
knockout creation, walkover + replay (`200`), different winner (`409`), invalid winner (`400`), score edit on a walkover (`409`),
standings (winner played 1 / won 1 / 2 pts, loser played 1 / lost 1, point difference 0), fixtures (`"Walkover"` / `"0-0"`),
fixtures CSV, DUPR export (only the played match), bracket advancement (final holds both winners with exactly 2 score rows),
a regular `11-7` final (`outcome_type='regular'`), and 10 simultaneous identical HTTP requests (1 "recorded", 9 "already recorded",
2 score rows). Caveat: the dev server is a single gevent process with a blocking C driver, so those HTTP requests may not have
truly overlapped; the thread-based tests above are the real concurrency proof.

### Not verified

* A **screen-recording video file** is not part of this repository (see section 14).
* The browser UI was exercised against the SQLite copy, not against MySQL (the API behaviour is identical, shown above).
* A production MySQL server of another version or with different data (see the end of section 7).

## 11. Assumptions

These are product decisions the assignment did not specify; each is isolated in one place so it is easy to change.

* **Winner selection.** The organizer/referee explicitly picks the winner (the team that showed up). The backend validates it is one of the match's two teams; it never infers it.
* **When a walkover can be declared.** Both teams assigned (not `NULL`/`TBD`), status `pending` or `on-going`, not already finalized, and **no score recorded yet** (all score rows are 0). A match with points on the board has been played, so a walkover would contradict the data; that case is a future "forfeit/retirement" outcome and returns `409` instead of silently discarding scores.
* **Completion.** A walkover sets `outcome_type='walkover'`, `winner_team_id`, `status='completed'`, `is_final=true` in one transaction, together with knockout advancement. No competitive score is written; zero rows are only created if missing (so joins/standings keep working).
* **Pool standings.** A walkover counts as a **match played for both teams**, **one win** for the winner, **one loss** for the other team (`played - won`), and contributes **no points scored or conceded**, so point difference is unchanged. The winner gets the usual 2 table points per win. This follows the existing rules (win = 2 points, ranking by wins then point difference) and is **my assumption**: Khel Club's official rules were not provided. `complete-round-2` and the `/points*` reports sum raw scores, so a walkover contributes 0 there.
* **Point difference.** Unchanged by walkovers (see above).
* **Historical matches.** Not reclassified unless provably regular (section 7). `NULL` is treated as the legacy score-based behaviour everywhere.
* **Result corrections.** There is no correction workflow for a walkover: a completed walkover is rejected by `update-score`, can't be re-opened by `update-match-status`, and a different winner is a `409`. Regular matches keep their pre-existing re-score behaviour. (A correction needs authorization and an audit trail; see section 12.)
* **DUPR export.** Walkovers are excluded: no game was played, so there is nothing to rate.
* **No auth.** There is no authentication layer in this codebase, so the endpoint is as open as every other route.

## 12. What I would improve with more time

* **Authorization + audit log** for result changes (who declared a walkover, when, and why), then an authorized **correction** flow (reverse a walkover, un-advance the bracket safely).
* **Forfeit** (mid-match, partial scores kept, reason) and **Bye** (single team, auto-advance) as registry entries + validators; store `reason`/`declared_by`.
* Remove the remaining duplicated result logic: the five standings implementations should become one function fed by `decided_result`/score rows (I only made them walkover-aware and did not rewrite them).
* A real migration tool (Alembic) instead of loose scripts, and CI that runs the suite plus `tests/test_mysql_integration.py` against a MySQL service container on every push.
* Frontend automated tests (the repo has no test setup) and a lint configuration (there is none, so `next lint` cannot run).
* Rooms in the Socket.IO layer so a client only receives events for its tournament/match.

## 13. Known limitations

* Verified on MySQL 8.0.40 and SQLite, **not** on your production server (section 7). No video recording is included.
* A regular match can still be re-scored after completion (pre-existing behaviour kept on purpose); overriding a knockout match with a *different* winner after the successor was already filled replaces the slot but leaves the old team's zero score row (pre-existing; it was previously also duplicated).
* `update_successor_match`'s old behaviour of writing `'TBD'`-slot score rows is gone, but stale `'TBD'` rows that may exist in older data are not cleaned up.
* No authentication, so no `403`.

## 14. Demo and screen recording

**No video file is included in this repository**; the end-to-end flow was exercised in a browser (section 10) but a
recording still has to be made by the submitter. A reproducible 3-minute script (Windows; record with Xbox Game Bar
`Win+Alt+R`, or OBS):

```powershell
# terminal 1: fresh demo database + backend (from v0_backend, venv active)
python scripts/load_dump_sqlite.py dump-test-202512221924.sql D:/work/demo.db
$env:DATABASE_URL = "sqlite:///D:/work/demo.db"
python migrations/bul/add_match_outcome_type.py
python app.py                                   # wait for "Server initialized"; serves on :5001

# terminal 2: one 4-team knockout bracket on top of the 4-team pool (SF1 = match 13, SF2 = 14, Final = 15)
Invoke-RestMethod -Method Post -Uri http://localhost:5001/knockout -ContentType 'application/json' `
  -Body '{"tournament_id":1,"team_ids":["T001","T002","T003","T004"],"current_round_id":1}'

# terminal 3: frontend (from v0_frontend)
npm ci --legacy-peer-deps; npm run dev          # http://localhost:3000
```

Storyboard (every URL below is on `http://localhost:3000`):

1. `/tournament/1` > **Table** tab: all matches pending. Show **Standings** (all zeros).
2. `/referee/1/matches/7` (pending): the prompt offers **Declare Walkover** > pick *Team T002 wins* > **Confirm Walkover**.
3. The screen shows Completed + Walkover + winner. **Refresh** the page: same result.
4. `/tournament/1` > **Table**: *Walkover, Winner: Mike Wilson, Emma Davis*; **Standings**: T002 played 1 / won 1 / 2 pts, T001 played 1 / lost 1, point difference 0.
5. `/referee/1/matches/13` (SF1) > walkover for T001; then declare SF2 (`/referee/1/matches/14`) for T004. **Fixtures > Knockouts**: `W/O` beside both winners and the final already shows T001 vs T004.
6. Regular scoring is intact: `/referee/1/matches/8` > Start Match > enter 11 and 7 > **Final** > Confirm; the Table tab shows `11-7`.
7. (Optional) Open `/tournament/overlay?tournament=1&match=9` in another window, then declare a walkover on match 9: the overlay switches to `W` / `WALKOVER` live.

## Pool pairing reference (pre-existing content of this README)

| **# Pools** | **Same-Pool Pairing**                        | **Near-pool Pairing**                       | **Half-Pool Pairing**                     | **Far-Pool Pairing**                     |
|-------------|----------------------------------------------|---------------------------------------------|-------------------------------------------|------------------------------------------|
| **2 Pools** | A1 vs A2, B1 vs B2                           | A1 vs B2, A2 vs B1                         | A1 vs A2, B1 vs B2 (same as "same-pool")  | A1 vs B2, A2 vs B1                       |
| **4 Pools** | A1 vs A2, B1 vs B2, C1 vs C2, D1 vs D2       | A1 vs B2, A2 vs B1, C1 vs D2, C2 vs D1     | A1 vs B2, A2 vs B1, C1 vs D2, C2 vs D1    | A1 vs D2, A2 vs D1, B1 vs C2, B2 vs C1   |
| **8 Pools** | A1 vs A2, B1 vs B2, C1 vs C2, D1 vs D2, E1 vs E2, F1 vs F2, G1 vs G2, H1 vs H2 | A1 vs B2, A2 vs B1, C1 vs D2, C2 vs D1, E1 vs F2, E2 vs F1, G1 vs H2, G2 vs H1 | A1 vs D2, A2 vs D1, E1 vs H2, H1 vs E2    | A1 vs H2, A2 vs H1, B1 vs G2, B2 vs G1, C1 vs F2, C2 vs F1, D1 vs E2, D2 vs E1 |
