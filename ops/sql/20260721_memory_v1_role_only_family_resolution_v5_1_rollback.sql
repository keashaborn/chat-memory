BEGIN;

DO $guard$
BEGIN
  IF to_regclass('memory.entity_role_resolution_v5_1') IS NOT NULL
     AND EXISTS (SELECT 1 FROM memory.entity_role_resolution_v5_1) THEN
    RAISE EXCEPTION 'role-only family resolution rollback refused: durable rows exist';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS memory.apply_role_only_family_resolution_v5_1(
  uuid, uuid, uuid, text
);
DROP FUNCTION IF EXISTS memory.preflight_role_only_family_apply_v5_1(
  uuid, uuid
);
DROP FUNCTION IF EXISTS memory.reconcile_role_only_family_resolution_v5_1(
  uuid, uuid, uuid, text, text
);
DROP FUNCTION IF EXISTS memory.preflight_role_only_family_resolution_v5_1(
  uuid, uuid, text
);
DROP TABLE IF EXISTS memory.entity_role_resolution_v5_1;

COMMIT;
