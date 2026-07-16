BEGIN;

DO $guard$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'evidence intake selector rollback requires sage';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS
  memory.record_owner_evidence_intake_terminal_v1(
    uuid,text,text,text,text
  );
DROP FUNCTION IF EXISTS
  memory.plan_owner_evidence_intake_v1(text,integer,uuid);
DROP TRIGGER IF EXISTS evidence_intake_terminal_append_only_guard
  ON memory.evidence_intake_terminal;
DROP FUNCTION IF EXISTS
  memory.guard_evidence_intake_terminal_append_only();
DROP TABLE IF EXISTS memory.evidence_intake_terminal;
DROP INDEX IF EXISTS memory.artifact_endorsement_owner_evidence_idx;
DROP INDEX IF EXISTS memory.entity_alias_owner_evidence_idx;
DROP INDEX IF EXISTS memory.user_preference_owner_evidence_idx;

DO $role$
BEGIN
  IF to_regrole('memory_intake_maintainer') IS NOT NULL THEN
    DROP OWNED BY memory_intake_maintainer;
    DROP ROLE memory_intake_maintainer;
  END IF;
END
$role$;

COMMIT;
