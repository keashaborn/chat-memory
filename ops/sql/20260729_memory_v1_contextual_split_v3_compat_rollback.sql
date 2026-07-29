BEGIN;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM memory.evidence_contextual_span_v2
     WHERE splitter_version =
       'memory_v1_contextual_span_splitter_20260728_v3'
        OR span_origin = 'contextual_split_v3'
  ) THEN
    RAISE EXCEPTION 'v3 contextual data prevents schema rollback';
  END IF;
END
$$;

DROP FUNCTION IF EXISTS memory.finalize_owner_contextual_split_v3(
  uuid,text,text,text,integer
);
DROP FUNCTION IF EXISTS memory.apply_owner_contextual_split_v3(
  uuid,text,text,jsonb,text
);
DROP FUNCTION IF EXISTS memory.preflight_owner_contextual_split_v3(
  uuid,text,text,jsonb
);

ALTER TABLE memory.evidence_contextual_span_v2
  DROP CONSTRAINT evidence_contextual_span_v2_boundary_reason_check,
  DROP CONSTRAINT evidence_contextual_span_v2_span_origin_check,
  DROP CONSTRAINT evidence_contextual_span_v2_splitter_version_check;

ALTER TABLE memory.evidence_contextual_span_v2
  ADD CONSTRAINT evidence_contextual_span_v2_boundary_reason_check
    CHECK (boundary_reason IN (
      'sentence_boundary','clause_boundary','length_boundary','terminal_span'
    )),
  ADD CONSTRAINT evidence_contextual_span_v2_span_origin_check
    CHECK (span_origin = 'contextual_split_v2'),
  ADD CONSTRAINT evidence_contextual_span_v2_splitter_version_check
    CHECK (
      splitter_version =
      'memory_v1_contextual_span_splitter_20260728_v2'
    );

COMMIT;
