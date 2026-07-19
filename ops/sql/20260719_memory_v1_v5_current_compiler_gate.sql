BEGIN;

DO $migration$
DECLARE
  expected_hash constant text :=
    '5cb83e837e38174af4cfda016c20009168ff62974e7d9137b30228d9973256a2';
  change record;
  definition text;
  updated text;
BEGIN
  IF session_user <> 'sage' THEN
    RAISE EXCEPTION 'current-compiler gate migration requires sage'
      USING ERRCODE = '42501';
  END IF;

  FOR change IN
    SELECT * FROM (VALUES
      (
        'memory.plan_owner_v5_local_entity_validation_v1(integer)'::regprocedure,
        E'    AND packet.manual_review_required\n',
        E'    AND packet.manual_review_required\n'
          || '    AND packet.policy_compiler_sha256=''' || expected_hash
          || E'''\n'
      ),
      (
        'memory.plan_owner_v5_local_auto_stage_v1(integer)'::regprocedure,
        E'    AND packet.provider_id=''local_llama_cpp''\n',
        E'    AND packet.provider_id=''local_llama_cpp''\n'
          || '    AND packet.policy_compiler_sha256=''' || expected_hash
          || E'''\n'
      ),
      (
        'memory.register_owner_v5_local_entity_validation_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,jsonb,text)'::regprocedure,
        E'    AND value.packet_storage_sha256=p_packet_storage_sha256\n'
          || E'    AND value.manual_review_required;\n',
        E'    AND value.packet_storage_sha256=p_packet_storage_sha256\n'
          || '    AND value.policy_compiler_sha256=''' || expected_hash
          || E'''\n'
          || E'    AND value.manual_review_required;\n'
      ),
      (
        'memory.register_owner_v5_local_auto_stage_v1(uuid,uuid,uuid,text,text,text,text)'::regprocedure,
        E'     OR source.provider_id<>''local_llama_cpp''\n',
        E'     OR source.provider_id<>''local_llama_cpp''\n'
          || E'     OR NOT EXISTS (\n'
          || E'       SELECT 1\n'
          || E'       FROM memory.evidence_extraction_packet_v5_local AS governed_packet\n'
          || E'       WHERE governed_packet.owner_user_id=actor\n'
          || E'         AND governed_packet.packet_id=source.packet_id\n'
          || '         AND governed_packet.policy_compiler_sha256='''
          || expected_hash || E'''\n'
          || E'     )\n'
      )
    ) AS changes(signature, anchor_text, replacement_text)
  LOOP
    SELECT pg_get_functiondef(change.signature) INTO definition;
    IF strpos(definition, change.replacement_text) > 0 THEN
      CONTINUE;
    END IF;
    IF strpos(definition, change.anchor_text) = 0
       OR strpos(
         substr(definition, strpos(definition, change.anchor_text)
           + length(change.anchor_text)),
         change.anchor_text
       ) > 0 THEN
      RAISE EXCEPTION 'compiler gate function definition drifted: %',
        change.signature;
    END IF;
    updated := replace(
      definition, change.anchor_text, change.replacement_text
    );
    EXECUTE updated;
  END LOOP;

  IF EXISTS (
    SELECT 1
    FROM pg_proc AS function
    JOIN pg_roles AS owner ON owner.oid = function.proowner
    WHERE function.oid IN (
      'memory.plan_owner_v5_local_entity_validation_v1(integer)'::regprocedure,
      'memory.register_owner_v5_local_entity_validation_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,jsonb,text)'::regprocedure
    )
      AND owner.rolname <> 'memory_v5_local_entity_validation_maintainer'
  ) OR EXISTS (
    SELECT 1
    FROM pg_proc AS function
    JOIN pg_roles AS owner ON owner.oid = function.proowner
    WHERE function.oid IN (
      'memory.plan_owner_v5_local_auto_stage_v1(integer)'::regprocedure,
      'memory.register_owner_v5_local_auto_stage_v1(uuid,uuid,uuid,text,text,text,text)'::regprocedure
    )
      AND owner.rolname <> 'memory_v5_local_disposition_maintainer'
  ) THEN
    RAISE EXCEPTION 'compiler gate changed function ownership';
  END IF;
END
$migration$;

COMMIT;
