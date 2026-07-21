BEGIN;
REVOKE EXECUTE ON FUNCTION memory.v5_jsonb_exact_keys(jsonb,text[])
  FROM memory_evidence_maintainer;
DROP FUNCTION IF EXISTS memory.apply_owner_atomic_evidence_split_v1(
  uuid,text,text,jsonb,text
);
DROP FUNCTION IF EXISTS memory.preflight_owner_atomic_evidence_split_v1(
  uuid,text,text,jsonb
);
DROP FUNCTION IF EXISTS memory.atomic_span_uuid_v1(text);
DROP TABLE IF EXISTS memory.evidence_atomic_span_v1;
COMMIT;
