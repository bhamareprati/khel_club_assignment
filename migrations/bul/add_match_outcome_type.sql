-- Walkover support: record HOW a match result was decided.
-- Run once per database (MySQL). Non-destructive: existing rows get NULL.
-- (db.create_all() in app.py does not alter existing tables.)

ALTER TABLE `match`
ADD COLUMN `outcome_type` VARCHAR(20) NULL;

-- Optional backfill: mark historical matches as 'regular' ONLY when the outcome
-- can be established reliably (finalized, has a winner, and the winner has the
-- strictly higher recorded score). Everything else stays NULL (unknown).
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
  );

-- Verify
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'match' AND COLUMN_NAME = 'outcome_type';
