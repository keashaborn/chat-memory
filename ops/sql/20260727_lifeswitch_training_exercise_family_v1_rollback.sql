BEGIN;

-- Rollback removes only the additive browse taxonomy introduced by v1.
-- Existing catalog exercises and all LifeSwitch training history remain intact.

DROP TABLE IF EXISTS catalog_dev.exercise_family_member;
DROP TABLE IF EXISTS catalog_dev.exercise_family;

COMMIT;
