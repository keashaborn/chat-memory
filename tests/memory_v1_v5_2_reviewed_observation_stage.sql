\set ON_ERROR_STOP on

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',false
);

DO $source_contract$
DECLARE
  row_value record;
BEGIN
  SELECT * INTO STRICT row_value
  FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1(20)
  WHERE route_event_id='0ee7e138-6fd8-5b4d-80bb-3a627c81301a'
    AND batch_id='1049b51b-a56f-4d0c-a148-9fc5be8773d8';
  IF row_value.source_kind<>'atom_apply'
     OR row_value.atom_apply_id<>
       'afc68e35-eac7-59c7-986d-29a65c8388f8'::uuid
     OR row_value.observation_count<>1 THEN
    RAISE EXCEPTION 'Dahlia source contract failed';
  END IF;

  SELECT * INTO STRICT row_value
  FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1(20)
  WHERE route_event_id='a8cdb502-f3e9-55dd-a804-a8d3f7096005'
    AND batch_id='8730dee5-5221-4948-a525-358397dffcb1';
  IF row_value.source_kind<>'atom_apply'
     OR row_value.atom_apply_id<>
       'bf11157f-11f7-59dd-9c4e-c5ce7c4d93af'::uuid
     OR row_value.observation_count<>1 THEN
    RAISE EXCEPTION 'Helsing source contract failed';
  END IF;

  SELECT * INTO STRICT row_value
  FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1(20)
  WHERE route_event_id='55b2ab8f-7b3c-5b38-af3c-98a237472082'
    AND batch_id='73a4e07f-f77e-4442-87ed-5a2aa62aa972';
  IF row_value.source_kind<>'reviewed_route'
     OR row_value.atom_apply_id IS NOT NULL
     OR row_value.observation_count<>1 THEN
    RAISE EXCEPTION 'Keasha source contract failed';
  END IF;
END
$source_contract$;

WITH source AS (
  SELECT *
  FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1(20)
  WHERE route_event_id='0ee7e138-6fd8-5b4d-80bb-3a627c81301a'
    AND batch_id='1049b51b-a56f-4d0c-a148-9fc5be8773d8'
)
SELECT *
FROM source
CROSS JOIN LATERAL
  memory.register_owner_v5_2_reviewed_observation_stage_v1(
    '10000000-0000-4000-8000-000000000001',
    '11000000-0000-4000-8000-000000000001',
    source.route_event_id,source.atom_apply_id,source.batch_id,
    source.stage_manifest_sha256,source.resolution_state_sha256,
    source.observation_state_sha256,
    'memory_v1_v5_2_atom_reviewed_stage_admission_policy_v1'
  );

WITH source AS (
  SELECT *
  FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1(20)
  WHERE route_event_id='a8cdb502-f3e9-55dd-a804-a8d3f7096005'
    AND batch_id='8730dee5-5221-4948-a525-358397dffcb1'
)
SELECT *
FROM source
CROSS JOIN LATERAL
  memory.register_owner_v5_2_reviewed_observation_stage_v1(
    '20000000-0000-4000-8000-000000000002',
    '22000000-0000-4000-8000-000000000002',
    source.route_event_id,source.atom_apply_id,source.batch_id,
    source.stage_manifest_sha256,source.resolution_state_sha256,
    source.observation_state_sha256,
    'memory_v1_v5_2_atom_reviewed_stage_admission_policy_v1'
  );

WITH source AS (
  SELECT *
  FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1(20)
  WHERE route_event_id='55b2ab8f-7b3c-5b38-af3c-98a237472082'
    AND batch_id='73a4e07f-f77e-4442-87ed-5a2aa62aa972'
)
SELECT *
FROM source
CROSS JOIN LATERAL
  memory.register_owner_v5_2_reviewed_observation_stage_v1(
    '30000000-0000-4000-8000-000000000003',
    '33000000-0000-4000-8000-000000000003',
    source.route_event_id,source.atom_apply_id,source.batch_id,
    source.stage_manifest_sha256,source.resolution_state_sha256,
    source.observation_state_sha256,
    'memory_v1_v5_2_reviewed_route_stage_admission_policy_v1'
  );

DO $applied_contract$
BEGIN
  IF (
    SELECT count(*)
    FROM memory.v5_local_stage_admission_batch_v2(
      '10000000-0000-4000-8000-000000000001'
    )
    WHERE batch_id='1049b51b-a56f-4d0c-a148-9fc5be8773d8'
  )<>1 OR (
    SELECT count(*)
    FROM memory.v5_local_stage_admission_batch_v2(
      '20000000-0000-4000-8000-000000000002'
    )
    WHERE batch_id='8730dee5-5221-4948-a525-358397dffcb1'
  )<>1 OR (
    SELECT count(*)
    FROM memory.v5_local_stage_admission_batch_v2(
      '30000000-0000-4000-8000-000000000003'
    )
    WHERE batch_id='73a4e07f-f77e-4442-87ed-5a2aa62aa972'
  )<>1 THEN
    RAISE EXCEPTION 'exact stage batch resolution failed';
  END IF;
END
$applied_contract$;

SELECT *
FROM memory.register_owner_v5_2_reviewed_observation_stage_v1(
  '10000000-0000-4000-8000-000000000001',
  '11000000-0000-4000-8000-000000000001',
  '0ee7e138-6fd8-5b4d-80bb-3a627c81301a',
  'afc68e35-eac7-59c7-986d-29a65c8388f8',
  '1049b51b-a56f-4d0c-a148-9fc5be8773d8',
  '54791dbff748d2c0d5318ccac6821459aae98606f3b744747c9bb8c654bd6b0c',
  '2060fa63a268bc128b1deeef041f560ba95fae49d14b71f79668257aeae508cb',
  '79150ef4d3927852ccccafb47a3de6fb1c9b01bfd2647d164e904840443ad201',
  'memory_v1_v5_2_atom_reviewed_stage_admission_policy_v1'
)
WHERE outcome='replayed' AND rows_written=0;

DO $entailment_planner_contract$
BEGIN
  IF (
    SELECT count(*)
    FROM memory.plan_owner_v5_local_entailment_v1(20)
    WHERE observation_id=ANY(ARRAY[
      '917ab793-6f03-4af4-847b-c87f5632fa91'::uuid,
      'c0194481-bed5-438f-9407-07e398f14e50'::uuid,
      '14e21c6b-1728-439b-9613-7d9b933d33b8'::uuid
    ])
  )<>3 THEN
    RAISE EXCEPTION 'three reviewed observations are not entailment eligible';
  END IF;
END
$entailment_planner_contract$;

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',false
);

DO $foreign_owner_contract$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1(20)
    WHERE route_event_id='0ee7e138-6fd8-5b4d-80bb-3a627c81301a'
  ) OR EXISTS (
    SELECT 1
    FROM memory.v5_local_stage_admission_batch_v2(
      '10000000-0000-4000-8000-000000000001'
    )
  ) THEN
    RAISE EXCEPTION 'foreign owner observed target state';
  END IF;
  BEGIN
    PERFORM *
    FROM memory.register_owner_v5_2_reviewed_observation_stage_v1(
      '40000000-0000-4000-8000-000000000004',
      '44000000-0000-4000-8000-000000000004',
      '0ee7e138-6fd8-5b4d-80bb-3a627c81301a',NULL,
      '1049b51b-a56f-4d0c-a148-9fc5be8773d8',
      repeat('0',64),repeat('0',64),repeat('0',64),
      'memory_v1_v5_2_reviewed_route_stage_admission_policy_v1'
    );
    RAISE EXCEPTION 'foreign owner registration unexpectedly succeeded';
  EXCEPTION
    WHEN no_data_found THEN NULL;
  END;
END
$foreign_owner_contract$;

RESET SESSION AUTHORIZATION;
