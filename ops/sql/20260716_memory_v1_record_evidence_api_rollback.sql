BEGIN;

DO $guard$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'record evidence API rollback requires sage';
  END IF;
END
$guard$;

DROP FUNCTION memory.record_owner_evidence_v1(
  memory.evidence_kind,text,text,text,timestamptz,numeric,numeric,text,
  memory.sensitivity_level,jsonb
);
REVOKE INSERT ON memory.evidence FROM memory_evidence_maintainer;

COMMIT;
