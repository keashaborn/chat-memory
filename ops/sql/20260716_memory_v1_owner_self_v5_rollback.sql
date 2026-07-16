BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'owner self V5 rollback requires sage';
  END IF;
  IF to_regclass('memory.owner_self_bootstrap_v5') IS NOT NULL
     AND EXISTS (SELECT 1 FROM memory.owner_self_bootstrap_v5 LIMIT 1) THEN
    RAISE EXCEPTION 'owner self V5 rollback refuses nonempty audit state';
  END IF;
END
$block$;

DROP FUNCTION IF EXISTS memory.bootstrap_owner_self_v5(uuid,text);
DROP FUNCTION IF EXISTS memory.preflight_owner_self_v5();
DROP TABLE IF EXISTS memory.owner_self_bootstrap_v5;
DROP INDEX IF EXISTS memory.entity_one_active_self_v5;

COMMIT;
