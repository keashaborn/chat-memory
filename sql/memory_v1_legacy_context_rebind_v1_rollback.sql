BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $guard$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'legacy context rebind rollback requires sage';
  END IF;
  IF to_regclass('memory.evidence_context_rebind_v1') IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM memory.evidence_context_rebind_v1
     ) THEN
    RAISE EXCEPTION
      'legacy context rebind rollback refuses durable rows'
      USING ERRCODE = '23514';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS
  memory.finalize_owner_legacy_context_rebind_v1(
    uuid,uuid,uuid,uuid,uuid,text,text
  );
DROP TABLE IF EXISTS memory.evidence_context_rebind_v1;
DROP FUNCTION IF EXISTS
  memory.guard_evidence_context_rebind_append_only_v1();

DO $role$
BEGIN
  IF to_regrole('memory_context_rebind_maintainer') IS NOT NULL THEN
    DROP ROLE memory_context_rebind_maintainer;
  END IF;
END
$role$;

COMMIT;
