# Walkover match outcome: Khel Club tournament system

This is my submission for the Khel Club FDE intern assignment.

**What I built:** a new match result called **Walkover** (one team does not turn up, so the other team is awarded the match). It works in the backend (this repo) and in the frontend, and it is built so that **Forfeit** and **Bye** can be added later without rewriting things.

| | Link |
|---|---|
| Backend (this repo) | https://github.com/bhamareprati/khel_club_assignment/tree/feature/walkover-match-outcome |
| Frontend | https://github.com/bhamareprati/v0_frontend/tree/feature/walkover-match-outcome |
| Screen recording | submitted separately (not stored in the repo) |

**Contents:**
[1. How scoring works today](#1-how-the-current-scoring-works-before-my-change) ·
[2. The problem](#2-why-a-walkover-cannot-just-be-a-score) ·
[3. What I changed and why](#3-what-i-changed-and-why) ·
[4. Database and migration](#4-database-changes-and-migration) ·
[5. The new API](#5-the-new-api-in-short) ·
[6. Assumptions](#6-assumptions-i-made) ·
[7. What I would improve](#7-what-i-would-improve-with-more-time) ·
[8. Run and test it](#8-run-and-test-it-yourself) ·
[9. What I did not check](#9-what-i-did-not-check)

The long, technical version of this document (API tables, every test, MySQL details) is in [`docs/TECHNICAL_DETAILS.md`](docs/TECHNICAL_DETAILS.md). The frontend README explains the screens.

---

## 1. How the current scoring works (before my change)

I read the code to find this out. Nothing here is guessed.

1. **A match and its scores are stored separately.** A match says "Team A vs Team B". The `score` table has **one row per team per match**, and every row starts at `0`.
2. **The referee types a score.** The screen sends text like `"11-7"` to `POST /update-score`. The backend saves `11` for team 1 and `7` for team 2.
3. **"Final" decides the winner.** When the referee presses Final, the backend compares the two numbers. The bigger number wins. If they are equal there is no winner (a draw). The match is then marked `is_final`.
4. **Status is a separate thing.** A match goes `pending` → `on-going` → `completed`. That is changed by a different endpoint, `/update-match-status`. The referee screen called **both** endpoints at the same moment when you pressed Final.
5. **Knockout brackets.** When a knockout match is finalized, its winner is copied into the next match (into `team1` or `team2`, depending on which earlier match it was). The next match gets its own zero score rows once both teams are known.
6. **Standings** are worked out again on every request from the score numbers. A win means a bigger number, a win gives 2 table points, and ties are broken by point difference. A match scored `0-0` counts as "not played". This same idea is written in **5 different places** in the code.
7. **Showing results.** The match list sends each result as text like `"11-7"`. A live overlay gets updates through Socket.IO (an event called `score_update`).
8. **Protection.** The backend did not stop anyone from re-scoring a finished match. Only the screen asked "override?".
9. **Database changes.** There is no migration tool. Changes are plain SQL or Python scripts in `migrations/bul/`. At start-up, `db.create_all()` only creates **missing tables**. It never adds a column to a table that already exists.
10. **No login.** The backend has no users or permissions, so there is no "forbidden" case anywhere.

Two small problems I found on the way:
- Finalizing the same match twice **created duplicate score rows** for the next knockout match.
- The two requests from the Final button could race each other.

## 2. Why a walkover cannot just be a score

- **`0-0` already means "not played yet".** A score cannot tell "walkover" from "match not started".
- **A fake score such as `11-0` would invent points.** It would change point difference, the points reports and the DUPR export, and nobody could later tell it from a real result.
- **A walkover winner is a decision, not a number.** Every place that compares numbers to find a winner would get it wrong.

So the result needs its own field that says how the match was decided.

## 3. What I changed and why

### The main idea: keep two things separate

| | What it tells you | Values |
|---|---|---|
| **status** (already existed, unchanged) | where the match is in its life | `pending`, `on-going`, `completed` |
| **outcome_type** (new) | **how** the result was decided | `regular`, `walkover`, or empty |

Think of a parcel: "Delivered" is the status, and "handed over in person" or "left at the door" is the outcome. A finished walkover is status `completed` with outcome `walkover`. An empty outcome means "not decided yet" or "an old match we could not classify". Nothing assumes empty means regular.

### Backend

| What I did | In simple words | Why |
|---|---|---|
| Added a column `outcome_type` to the `match` table, with a migration and a rollback | One extra optional column. Old data is not changed. | The result needs a place to be stored safely. |
| Created **one rulebook file**, `services/match_outcomes.py` | A small list says what each outcome means (for example "walkover: no score, winner chosen by a person"). Other code asks the list instead of writing `if walkover` in many places. | The assignment asked for no hard-coded, scattered logic. Adding Forfeit later is about **one line**, and a test proves it. |
| New endpoint `POST /matches/<id>/outcome` | You send the winning team. It checks everything, then saves. | A walkover needs its own safe entry point, not the score form. |
| **All-or-nothing saving** | The result and "move the winner to the next round" are saved together. If anything fails, nothing is saved. | Otherwise a match could be finished while the bracket never moved. |
| **Safe repeats** | Sending the same walkover twice changes nothing the second time. Two people sending different winners at once: the first wins, the other gets a clear "already completed" (409) message. | Double taps and two referees happen in real life. |
| Fixed the next-round logic | The winner goes into the right slot, and the next match's score rows are created only once. | The old code created duplicates, and I had to reuse that code. |
| Block edits on finished walkovers | Score updates and re-opening are refused with a 409 message. | Nobody should quietly turn a walkover into a fake score. |
| Standings now understand walkovers | In all 5 places: a walkover counts as a played match, a win for the winner, a loss for the other team, and **adds no points**. | Before, it looked like a `0-0` match and nobody got a win. |
| Results and exports | The match list shows `"Walkover"` instead of `0-0`, and the DUPR export skips walkovers. | No game was played, so there is nothing to rate. |
| Live message | A walkover sends the winner and **no scores**. | The live overlay should never show a fake `0-0`. |
| Finishing a normal match is one step | `/update-score` with Final now also sets the winner, status `completed` and outcome `regular`. | It removes the two-request race. |

The checks a walkover must pass: the match exists, it belongs to that tournament, both teams are known, the winner is one of the two teams, the match is not already finished, and **no score has been entered yet**.

### Frontend (details are in the frontend README)

- A **Declare Walkover** button on the referee screen (also in the "match not started" prompt).
- A **confirmation dialog** with both teams. **Confirm** stays off until you pick a winner, and it shows loading and error messages.
- A finished walkover shows a **Walkover badge** and the **winner**, and it has no score form.
- The badge and winner also show in the match table, the fixture cards, the pool view, the bracket (as `W/O`) and the live overlay (which updates by itself).
- The Final button now sends one request, not two.
- Nothing was redesigned. I reused the existing styles and dialogs.
- While testing I also fixed a small crash that was already in the original code (the Court-wise and Status-wise tabs on the tournament page).

### Things that behave differently now (honest list)

- `match_result` is the word `"Walkover"` for these matches. Any outside tool that splits it on `-` would break. The bundled frontend handles it.
- Finished walkovers cannot be re-scored or re-opened (new 409 messages).
- A normal finished match can **still** be re-scored, as before.
- Existing endpoints only gained extra fields.

## 4. Database changes and migration

**The change:** one new optional column. Nothing is dropped or rewritten.

```sql
ALTER TABLE `match` ADD COLUMN `outcome_type` VARCHAR(20) NULL;
```

**Important:** `db.create_all()` will **not** add this column to an existing database. Run the migration **before** starting the new backend, because the new code reads the column.

```bash
# Option A: Python script (safe to run twice, checks itself, prints a summary)
python migrations/bul/add_match_outcome_type.py

# Option B: plain SQL
mysql -h <host> -u <user> -p <database> < migrations/bul/add_match_outcome_type.sql
```

**Old matches:** they keep an empty `outcome_type`. Only matches I can prove were normal (finished, has a winner, and the winner has the strictly higher recorded score) get `regular`. Draws, `0-0` finals and unfinished matches stay empty, because I do not want to guess.

**Undo it:** dropping the column loses the walkover marker, so back up first.

```bash
python migrations/bul/add_match_outcome_type.py --rollback            # dry run: only reports
python migrations/bul/add_match_outcome_type.py --rollback --confirm  # really drops the column
# or: mysql ... < migrations/bul/rollback_match_outcome_type.sql
```

**Where I ran it:** on a real **MySQL 8.0.40** server using your dump (both options, and twice to prove it is safe to repeat), and on SQLite. Please still try it on a **copy** of your real database first.

## 5. The new API in short

```http
POST /matches/25/outcome
Content-Type: application/json

{ "tournament_id": 1, "outcome_type": "walkover", "winner_team_id": "T001" }
```

```json
{ "match_id": 25, "tournament_id": 1, "status": "completed", "is_final": true,
  "outcome_type": "walkover", "winner_team_id": "T001", "message": "Walkover recorded successfully" }
```

| Code | Meaning |
|---|---|
| 200 | Recorded. Sending the same request again also gives 200 ("already recorded") and changes nothing. |
| 400 | Bad request: wrong outcome type, missing or wrong winner, bad tournament id. |
| 404 | Tournament or match not found, or the match belongs to another tournament. |
| 409 | Not allowed now: a team is not set yet, the match is already finished, or scores were already entered. |
| 500 | Unexpected error. Everything is rolled back and no internal details are shown. |

(There is no 403 because the app has no login.) Errors always look like `{"error": "...", "code": "..."}`. The full API list is in [`docs/TECHNICAL_DETAILS.md`](docs/TECHNICAL_DETAILS.md).

## 6. Assumptions I made

The assignment did not say these, so I chose and wrote them down. Each is in one place in the code, so it is easy to change.

- **Who wins:** a person (the referee or organizer) picks the winner. The backend only checks that it is one of the two teams.
- **When a walkover is allowed:** both teams known, match `pending` or `on-going`, not finished, and **no score entered**. If points are already on the board the match was played, so that would be a different outcome (a forfeit) and the backend says 409 instead of throwing the scores away.
- **Finishing:** a walkover sets the outcome, the winner, status `completed` and `is_final` together, and moves the winner up the bracket. No score is created. Zero rows are only added if they are missing, so standings keep working.
- **Standings:** a walkover counts as a match played for both teams, one win for the winner, one loss for the other, and **no points scored or conceded**, so point difference does not change. The winner gets the usual 2 table points. Khel Club's official rules were not given, so this is my choice.
- **Old matches:** not reclassified unless provably normal. Empty is treated like the old score-based behaviour.
- **Corrections:** there is no way to change a walkover yet. A correction needs login and a history log, so I left it out.
- **DUPR export:** walkovers are left out because no game was played.
- **No login:** the new endpoint is as open as every other route in this app.

## 7. What I would improve with more time

- **Login and history.** Record who declared a walkover, when and why, then add a safe, permission-checked way to **undo or correct** it (including moving the bracket back).
- **Forfeit and Bye.** Forfeit (a team quits mid-match, keeping the score so far, with a reason) and Bye (one team, moves on automatically) as new entries in the rulebook.
- **One standings function.** I made the five standings copies understand walkovers, but I did not merge them into one.
- **Proper migration tool and CI.** Use Alembic, and run the tests (including the MySQL ones) automatically on every push.
- **Frontend tests and lint.** The frontend has neither, so I could only check it by hand and with a build.
- **Live updates for the tournament page.** It loads the match list once and needs a refresh (F5) to show a new result. Only the overlay is live.
- **Socket.IO rooms** so a screen only receives events for its own match.

## 8. Run and test it yourself

Needs Python 3.12 (I used 3.12.13). MySQL 8 is needed for a real run.

```bash
python -m venv .venv
.venv\Scripts\activate                 # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt        # includes mysqlclient
pip install pytest
python -m pytest -q                    # runs on an in-memory database; touches nothing real
```

**Run with MySQL**

```bash
mysql -u <user> -p -e "CREATE DATABASE khel"; mysql -u <user> -p khel < dump-test-202512221924.sql
# put DB_HOST, DB_USER, DB_PASSWORD, DB_NAME, DB_PORT in a .env file next to app.py
python migrations/bul/add_match_outcome_type.py
python app.py                          # http://localhost:5001
```

Three small things I hit on MySQL (all already in the original code, not caused by my change): set `DB_PASSWORD` even if it is empty; delete the `GTID_PURGED` and `SQL_LOG_BIN` lines from a copy of the dump before loading it into a local server; and knockout brackets need a team row with the id `TBD` because of a foreign key. More detail is in the technical doc.

**Run without MySQL** (I used this for the browser demo):

```powershell
python scripts/load_dump_sqlite.py dump-test-202512221924.sql D:/work/dev.db
$env:DATABASE_URL = "sqlite:///D:/work/dev.db"      # use an absolute path
python migrations/bul/add_match_outcome_type.py
python app.py
```

The frontend finds the backend on `http://localhost:5001` by itself when you open it from `localhost`.

**What I ran and what happened (2026-11-12)**

| Check | Result |
|---|---|
| `python -m pytest -q` | **68 passed**. They cover the 12 cases from the assignment: normal 11-7 match, walkover for team A and for team B, wrong winner, missing match, bad outcome, already completed, repeated request, bracket advance, standings, rollback after a crash, and normal scoring still working. |
| MySQL tests (`MYSQL_TEST_URL=mysql://root@127.0.0.1:3307/khel_test python -m pytest -q tests/test_mysql_integration.py`) | **7 passed** on a real MySQL 8.0.40: 8 requests at the same moment record the result once, conflicting winners give one result, and two semi-finals advancing at once give the final exactly 2 score rows. When I removed the locking, these tests failed, so they really test it. |
| Whole app on MySQL | Walkover, repeat, conflict, standings, exports, bracket and a normal 11-7 final all worked over HTTP. |
| Frontend `npm run build` | Succeeded. |
| Real browser run | Declare Walkover, confirm, refresh, match table, standings, bracket `W/O`, live overlay and normal scoring all worked. |

## 9. What I did not check

- **Your real production database.** I tested on your dump, not on live data. Try the migration on a copy first.
- **The browser screens against MySQL.** I checked the screens on SQLite, and the API behaves the same on both.
- **The screen recording** is not in this repo. It is submitted separately.
