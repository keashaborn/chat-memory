\set ON_ERROR_STOP on

BEGIN READ ONLY;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $review$
DECLARE
  target_owner constant uuid :=
    '1240822d-ac9a-4096-95aa-e2b24d36ef50';
  compiler_sha constant text :=
    'f82e6f4339dfe4aada7e5c3edb71fde8125a819f33677b3f47b98f3726b60419';
  caregiving_job constant uuid :=
    'd0585d84-7386-5b5f-b7ff-ac97949883cc';
  profession_job constant uuid :=
    '360dbb78-69e3-5a3d-ba9b-2512acca21b8';
  caregiving_packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  profession_packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  caregiving_source text;
  profession_source text;
  caregiving_recorded_at timestamptz;
  profession_recorded_at timestamptz;
  item jsonb;
  span jsonb;
  span_text text;
  predicate_names text[];
  role_names text[];
BEGIN
  SELECT packet.* INTO STRICT caregiving_packet
  FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id=target_owner
    AND packet.job_id=caregiving_job;
  SELECT evidence.content,evidence.recorded_at
  INTO STRICT caregiving_source,caregiving_recorded_at
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=target_owner
    AND evidence.evidence_id=caregiving_packet.evidence_id;

  IF caregiving_packet.policy_compiler_sha256<>compiler_sha
     OR caregiving_packet.evidence_content_sha256<>
        '895146b94431f7e0ec3292e757e30fc4c782af222bd39598e610654adf08ccec'
     OR caregiving_packet.manual_review_required IS DISTINCT FROM true
     OR caregiving_packet.local_model_calls<>0
     OR caregiving_packet.external_model_calls<>0
     OR caregiving_packet.entity_mention_count<>2
     OR caregiving_packet.observation_count<>2
     OR caregiving_packet.comparison_hint_count<>0
     OR caregiving_packet.deferral_count<>2
     OR (SELECT status FROM memory.evidence_extraction_job
         WHERE owner_user_id=target_owner AND job_id=caregiving_job)
        <>'review_required' THEN
    RAISE EXCEPTION 'compiler-v8 caregiving packet envelope failed';
  END IF;

  SELECT array_agg(value->>'predicate' ORDER BY value->>'predicate')
  INTO predicate_names
  FROM jsonb_array_elements(
    caregiving_packet.normalized_packet->'observations'
  ) AS value;
  IF predicate_names<>ARRAY[
       'relationship.caregiver_for','relationship.spouse_of'
     ]::text[] THEN
    RAISE EXCEPTION 'compiler-v8 caregiving predicates failed';
  END IF;

  IF (
    SELECT count(*)
    FROM jsonb_array_elements(
      caregiving_packet.normalized_packet->'entity_mentions'
    ) AS value
    WHERE value->>'name_text'='Monika'
      AND value->>'entity_type'='person'
      AND value->>'relationship_role'
          ='relationship:care_recipient|relationship:wife'
  )<>1 THEN
    RAISE EXCEPTION 'compiler-v8 caregiving entity failed';
  END IF;

  FOR item IN
    SELECT value
    FROM jsonb_array_elements(
      caregiving_packet.normalized_packet->'observations'
    ) AS value
  LOOP
    IF jsonb_array_length(item->'source_spans')<>1 THEN
      RAISE EXCEPTION 'compiler-v8 caregiving span count failed';
    END IF;
    span:=item->'source_spans'->0;
    span_text:=substring(
      caregiving_source
      FROM (span->>'start')::integer+1
      FOR (span->>'end')::integer-(span->>'start')::integer
    );
    IF span_text NOT ILIKE '%Monika%'
       OR span_text ILIKE '%psychotic break%'
       OR span_text ILIKE '%Driving my cybertruck%'
       OR length(span_text)>=length(caregiving_source) THEN
      RAISE EXCEPTION
        'compiler-v8 caregiving bounded source span failed';
    END IF;
    IF item->>'predicate'='relationship.caregiver_for'
       AND span_text NOT ILIKE '%caring for%' THEN
      RAISE EXCEPTION 'compiler-v8 caregiver entailment span failed';
    END IF;
    IF item->>'predicate'='relationship.spouse_of'
       AND span_text NOT ILIKE '%wife%' THEN
      RAISE EXCEPTION 'compiler-v8 spouse entailment span failed';
    END IF;
  END LOOP;

  SELECT value->'source_spans'->0 INTO STRICT span
  FROM jsonb_array_elements(
    caregiving_packet.normalized_packet->'deferrals'
  ) AS value
  WHERE value->>'reason_code'='sensitive_manual_review';
  span_text:=substring(
    caregiving_source
    FROM (span->>'start')::integer+1
    FOR (span->>'end')::integer-(span->>'start')::integer
  );
  IF span_text NOT ILIKE '%caring for%'
     OR span_text NOT ILIKE '%Monika%'
     OR span_text ILIKE '%psychotic break%'
     OR length(span_text)>=length(caregiving_source) THEN
    RAISE EXCEPTION 'compiler-v8 sensitive deferral span failed';
  END IF;

  SELECT packet.* INTO STRICT profession_packet
  FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id=target_owner
    AND packet.job_id=profession_job;
  SELECT evidence.content,evidence.recorded_at
  INTO STRICT profession_source,profession_recorded_at
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=target_owner
    AND evidence.evidence_id=profession_packet.evidence_id;

  IF profession_packet.policy_compiler_sha256<>compiler_sha
     OR profession_packet.evidence_content_sha256<>
        'fb65fe592059bace88a4daea571ac1ba7df2b5aeb5136233dd01071cb6085182'
     OR profession_packet.manual_review_required IS DISTINCT FROM true
     OR profession_packet.local_model_calls<>1
     OR profession_packet.external_model_calls<>0
     OR profession_packet.entity_mention_count<>3
     OR profession_packet.observation_count<>2
     OR profession_packet.comparison_hint_count<>0
     OR (SELECT status FROM memory.evidence_extraction_job
         WHERE owner_user_id=target_owner AND job_id=profession_job)
        <>'review_required' THEN
    RAISE EXCEPTION 'compiler-v8 profession packet envelope failed';
  END IF;

  SELECT array_agg(value->>'predicate' ORDER BY value->>'observation_ref')
  INTO predicate_names
  FROM jsonb_array_elements(
    profession_packet.normalized_packet->'observations'
  ) AS value;
  IF predicate_names<>ARRAY[
       'occupation.works_as','occupation.works_as'
     ]::text[]
     OR jsonb_path_exists(
       profession_packet.normalized_packet,
       '$.observations[*] ? (@.predicate == "credential.reported")'
     ) THEN
    RAISE EXCEPTION 'compiler-v8 former-role predicates failed';
  END IF;

  SELECT array_agg(value->>'name_text' ORDER BY value->>'name_text')
  INTO role_names
  FROM jsonb_array_elements(
    profession_packet.normalized_packet->'entity_mentions'
  ) AS value
  WHERE value->>'entity_type'='concept';
  IF role_names<>ARRAY['BCBA','clinical psychologist']::text[] THEN
    RAISE EXCEPTION 'compiler-v8 former-role entities failed';
  END IF;

  FOR item IN
    SELECT observation.value
    FROM jsonb_array_elements(
      profession_packet.normalized_packet->'observations'
    ) AS observation(value)
  LOOP
    IF item->'object'->>'kind'<>'entity'
       OR item->'temporal'->>'shape'<>'open_interval'
       OR item->'temporal'->>'semantic'<>'state_validity'
       OR item->'temporal'->'instant_range'->>'lower' IS NOT NULL
       OR (item->'temporal'->'instant_range'->>'upper')::timestamptz
          <>profession_recorded_at
       OR NOT (
         item->'temporal'->'reason_codes'
         @> '["historical_relationship_ended_before_source"]'::jsonb
       ) THEN
      RAISE EXCEPTION 'compiler-v8 former-role temporal semantics failed';
    END IF;
    span:=item->'source_spans'->0;
    span_text:=substring(
      profession_source
      FROM (span->>'start')::integer+1
      FOR (span->>'end')::integer-(span->>'start')::integer
    );
    IF NOT (
      span_text ILIKE '%clinical psychologist%'
      OR span_text ILIKE '%BCBA%'
    ) OR length(span_text)>=length(profession_source) THEN
      RAISE EXCEPTION 'compiler-v8 former-role source span failed';
    END IF;
  END LOOP;
END
$review$;

ROLLBACK;

SELECT
  'memory_v1_v5_2_semantic_compiler_v8_live_packets: PASS' AS result;
