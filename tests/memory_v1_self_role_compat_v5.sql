\set ON_ERROR_STOP on

CREATE OR REPLACE FUNCTION pg_temp.assert_invalid_self_role_denied()
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO memory.entity_mention(
    owner_user_id,evidence_id,packet_sha256,entity_ref,entity_type,
    mention_kind,name_text,relationship_role,source_spans,
    extraction_confidence,reason_codes,extractor,extractor_version,
    mention_sha256
  ) VALUES (
    '11111111-1111-4111-8111-111111111111',
    'aeeeeeee-1111-4111-8111-111111111111',repeat('1',64),
    'e02','self','self_reference',NULL,'owner:self',
    jsonb_build_array(jsonb_build_object(
      'start',0,'end',1,'span_sha256',repeat('2',64)
    )),1.0,'[]','compat_test','v5',repeat('4',64)
  );
  RAISE EXCEPTION 'noncanonical self role unexpectedly accepted';
EXCEPTION WHEN check_violation THEN NULL;
END
$$;

BEGIN;

SELECT set_config(
  'app.user_id','11111111-1111-4111-8111-111111111111',true
);

INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,
  content,content_sha256,recorded_at,sensitivity
) VALUES (
  'aeeeeeee-1111-4111-8111-111111111111',
  '11111111-1111-4111-8111-111111111111',
  'user_statement','compat_test','owner-a-source','I',repeat('a',64),
  '2026-07-16T03:00:00Z','medium'
);

INSERT INTO memory.entity_mention(
  owner_user_id,evidence_id,packet_sha256,entity_ref,entity_type,
  mention_kind,name_text,relationship_role,source_spans,
  extraction_confidence,reason_codes,extractor,extractor_version,
  mention_sha256
) VALUES (
  '11111111-1111-4111-8111-111111111111',
  'aeeeeeee-1111-4111-8111-111111111111',repeat('1',64),
  'e01','self','self_reference',NULL,'user:self',
  jsonb_build_array(jsonb_build_object(
    'start',0,'end',1,'span_sha256',repeat('2',64)
  )),1.0,'[]','compat_test','v5',repeat('3',64)
);

SELECT 1 / ((
  SELECT count(*)=1
    FROM memory.entity_mention
   WHERE mention_kind='self_reference'
     AND relationship_role='user:self'
)::integer);

SELECT pg_temp.assert_invalid_self_role_denied();

ROLLBACK;

SELECT 1 / ((
  SELECT count(*)=0 FROM memory.entity_mention
)::integer);
SELECT 'memory_v1_self_role_compat_v5: PASS' AS result;
