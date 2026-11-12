-- Rollback for add_match_outcome_type.sql
--
-- Back up first, because dropping the column loses the walkover marker
-- (winner_team_id / status / is_final remain, scores stay 0-0):
--   SELECT id, outcome_type FROM `match` WHERE outcome_type IS NOT NULL;
--
-- Also redeploy the previous backend build BEFORE/WITH this rollback, because
-- the new code selects this column.

ALTER TABLE `match`
DROP COLUMN `outcome_type`;
