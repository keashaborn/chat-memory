BEGIN;

DO $rollback$
DECLARE
  expected_hash constant text :=
    '5cb83e837e38174af4cfda016c20009168ff62974e7d9137b30228d9973256a2';
  change record;
  definition text;
BEGIN
  IF session_user <> 'sage' THEN
    RAISE EXCEPTION 'current-compiler gate rollback requires sage'
      USING ERRCODE = '42501';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.evidence_extraction_packet_v5_local AS packet
    WHERE packet.policy_compiler_sha256 = expected_hash
      AND (
        EXISTS (
          SELECT 1
          FROM memory.v5_local_entity_validation_assessment AS assessment
          WHERE assessment.owner_user_id = packet.owner_user_id
            AND assessment.packet_id = packet.packet_id
        )
        OR EXISTS (
          SELECT 1
          FROM memory.v5_local_packet_stage_admission AS admission
          WHERE admission.owner_user_id = packet.owner_user_id
            AND admission.packet_id = packet.packet_id
        )
      )
  ) THEN
    RAISE EXCEPTION 'refusing compiler gate rollback after v3 downstream writes';
  END IF;

  FOR change IN
    SELECT * FROM (VALUES
      (
        'memory.plan_owner_v5_local_entity_validation_v1(integer)'::regprocedure,
        E'    AND packet.manual_review_required\n'
          || '    AND packet.policy_compiler_sha256=''' || expected_hash
          || E'''\n',
        E'    AND packet.manual_review_required\n'
      ),
      (
        'memory.plan_owner_v5_local_auto_stage_v1(integer)'::regprocedure,
        E'    AND packet.provider_id=''local_llama_cpp''\n'
          || '    AND packet.policy_compiler_sha256=''' || expected_hash
          || E'''\n',
        E'    AND packet.provider_id=''local_llama_cpp''\n'
      ),
      (
        'memory.register_owner_v5_local_entity_validation_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,jsonb,text)'::regprocedure,
        E'    AND value.packet_storage_sha256=p_packet_storage_sha256\n'
          || '    AND value.policy_compiler_sha256=''' || expected_hash
          || E'''\n'
          || E'    AND value.manual_review_required;\n',
        E'    AND value.packet_storage_sha256=p_packet_storage_sha256\n'
          || E'    AND value.manual_review_required;\n'
      ),
      (
        'memory.register_owner_v5_local_auto_stage_v1(uuid,uuid,uuid,text,text,text,text)'::regprocedure,
        E'     OR source.provider_id<>''local_llama_cpp''\n'
          || E'     OR NOT EXISTS (\n'
          || E'       SELECT 1\n'
          || E'       FROM memory.evidence_extraction_packet_v5_local AS governed_packet\n'
          || E'       WHERE governed_packet.owner_user_id=actor\n'
          || E'         AND governed_packet.packet_id=source.packet_id\n'
          || '         AND governed_packet.policy_compiler_sha256='''
          || expected_hash || E'''\n'
          || E'     )\n',
        E'     OR source.provider_id<>''local_llama_cpp''\n'
      )
    ) AS changes(signature, gated_text, original_text)
  LOOP
    SELECT pg_get_functiondef(change.signature) INTO definition;
    IF strpos(definition, change.gated_text) = 0 THEN
      RAISE EXCEPTION 'compiler gate rollback definition drifted: %',
        change.signature;
    END IF;
    EXECUTE replace(definition, change.gated_text, change.original_text);
  END LOOP;
END
$rollback$;

COMMIT;
