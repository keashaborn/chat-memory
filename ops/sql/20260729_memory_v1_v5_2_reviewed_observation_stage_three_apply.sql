\set ON_ERROR_STOP on

BEGIN;
SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

DO $apply$
DECLARE
  result record;
BEGIN
  SELECT * INTO STRICT result
  FROM memory.register_owner_v5_2_reviewed_observation_stage_v1(
    '35febb7e-0993-5ddf-b9b0-71a22d1b8501'::uuid,
    'ef8f0063-bab5-5ffa-afd6-174650f1f260'::uuid,
    '0ee7e138-6fd8-5b4d-80bb-3a627c81301a'::uuid,
    'afc68e35-eac7-59c7-986d-29a65c8388f8'::uuid,
    '1049b51b-a56f-4d0c-a148-9fc5be8773d8'::uuid,
    '54791dbff748d2c0d5318ccac6821459aae98606f3b744747c9bb8c654bd6b0c',
    '2060fa63a268bc128b1deeef041f560ba95fae49d14b71f79668257aeae508cb',
    '79150ef4d3927852ccccafb47a3de6fb1c9b01bfd2647d164e904840443ad201',
    'memory_v1_v5_2_atom_reviewed_stage_admission_policy_v1'
  );
  IF result.outcome <> 'applied' OR result.rows_written <> 2 THEN
    RAISE EXCEPTION 'Dahlia reviewed-observation admission failed';
  END IF;

  SELECT * INTO STRICT result
  FROM memory.register_owner_v5_2_reviewed_observation_stage_v1(
    '28f7e091-252e-5357-b64f-144f6445e2e8'::uuid,
    '486067e8-9720-55cf-b6ae-c49dd8fc6238'::uuid,
    'a8cdb502-f3e9-55dd-a804-a8d3f7096005'::uuid,
    'bf11157f-11f7-59dd-9c4e-c5ce7c4d93af'::uuid,
    '8730dee5-5221-4948-a525-358397dffcb1'::uuid,
    '9b7dbb26affa581731ef80d252ed2c3f1ab67e61a24c22f025892c6631577477',
    '523c57d9421c3e1d4a5dc469d2ba1543441f7aea6fc6b9e7e4bd884dc1d9ee54',
    'f8585573100106ab2bc217db7a5c40b5051e6e6c3d23e0d398326cd1d3bb619f',
    'memory_v1_v5_2_atom_reviewed_stage_admission_policy_v1'
  );
  IF result.outcome <> 'applied' OR result.rows_written <> 2 THEN
    RAISE EXCEPTION 'Helsing reviewed-observation admission failed';
  END IF;

  SELECT * INTO STRICT result
  FROM memory.register_owner_v5_2_reviewed_observation_stage_v1(
    'daa5a659-ba7c-5841-adb2-6aaa7c0481ae'::uuid,
    '8cbe179f-33a0-50e2-bcc3-89cc5154bdb4'::uuid,
    '55b2ab8f-7b3c-5b38-af3c-98a237472082'::uuid,
    NULL::uuid,
    '73a4e07f-f77e-4442-87ed-5a2aa62aa972'::uuid,
    '80f3a70eec0000bb40f57a2ab35c2bf2aa1fbc33ca40bfbfff23143c3bb70f02',
    '240eba070211b0b75df0ae3a972eabc2b38215aebe9ba599dee69ccdc304dff2',
    '0bda5c4695d1eb3bb0c9e9d6ac4ccd00306acd20dc9b3a4491b25509eeb41602',
    'memory_v1_v5_2_reviewed_route_stage_admission_policy_v1'
  );
  IF result.outcome <> 'applied' OR result.rows_written <> 2 THEN
    RAISE EXCEPTION 'Keasha reviewed-observation admission failed';
  END IF;
END
$apply$;

DO $replay$
DECLARE
  result record;
  item record;
BEGIN
  FOR item IN
    SELECT *
    FROM (VALUES
      (
        '35febb7e-0993-5ddf-b9b0-71a22d1b8501'::uuid,
        'ef8f0063-bab5-5ffa-afd6-174650f1f260'::uuid,
        '0ee7e138-6fd8-5b4d-80bb-3a627c81301a'::uuid,
        'afc68e35-eac7-59c7-986d-29a65c8388f8'::uuid,
        '1049b51b-a56f-4d0c-a148-9fc5be8773d8'::uuid,
        '54791dbff748d2c0d5318ccac6821459aae98606f3b744747c9bb8c654bd6b0c',
        '2060fa63a268bc128b1deeef041f560ba95fae49d14b71f79668257aeae508cb',
        '79150ef4d3927852ccccafb47a3de6fb1c9b01bfd2647d164e904840443ad201',
        'memory_v1_v5_2_atom_reviewed_stage_admission_policy_v1'
      ),
      (
        '28f7e091-252e-5357-b64f-144f6445e2e8'::uuid,
        '486067e8-9720-55cf-b6ae-c49dd8fc6238'::uuid,
        'a8cdb502-f3e9-55dd-a804-a8d3f7096005'::uuid,
        'bf11157f-11f7-59dd-9c4e-c5ce7c4d93af'::uuid,
        '8730dee5-5221-4948-a525-358397dffcb1'::uuid,
        '9b7dbb26affa581731ef80d252ed2c3f1ab67e61a24c22f025892c6631577477',
        '523c57d9421c3e1d4a5dc469d2ba1543441f7aea6fc6b9e7e4bd884dc1d9ee54',
        'f8585573100106ab2bc217db7a5c40b5051e6e6c3d23e0d398326cd1d3bb619f',
        'memory_v1_v5_2_atom_reviewed_stage_admission_policy_v1'
      ),
      (
        'daa5a659-ba7c-5841-adb2-6aaa7c0481ae'::uuid,
        '8cbe179f-33a0-50e2-bcc3-89cc5154bdb4'::uuid,
        '55b2ab8f-7b3c-5b38-af3c-98a237472082'::uuid,
        NULL::uuid,
        '73a4e07f-f77e-4442-87ed-5a2aa62aa972'::uuid,
        '80f3a70eec0000bb40f57a2ab35c2bf2aa1fbc33ca40bfbfff23143c3bb70f02',
        '240eba070211b0b75df0ae3a972eabc2b38215aebe9ba599dee69ccdc304dff2',
        '0bda5c4695d1eb3bb0c9e9d6ac4ccd00306acd20dc9b3a4491b25509eeb41602',
        'memory_v1_v5_2_reviewed_route_stage_admission_policy_v1'
      )
    ) AS value(
      admission_id,
      operation_id,
      route_event_id,
      atom_apply_id,
      batch_id,
      stage_manifest_sha256,
      resolution_state_sha256,
      observation_state_sha256,
      policy_version
    )
  LOOP
    SELECT * INTO STRICT result
    FROM memory.register_owner_v5_2_reviewed_observation_stage_v1(
      item.admission_id,
      item.operation_id,
      item.route_event_id,
      item.atom_apply_id,
      item.batch_id,
      item.stage_manifest_sha256,
      item.resolution_state_sha256,
      item.observation_state_sha256,
      item.policy_version
    );
    IF result.outcome <> 'replayed' OR result.rows_written <> 0 THEN
      RAISE EXCEPTION 'reviewed-observation replay invariant failed';
    END IF;
  END LOOP;
END
$replay$;

DO $verify$
BEGIN
  IF (
    SELECT count(*)
    FROM (VALUES
      (
        '35febb7e-0993-5ddf-b9b0-71a22d1b8501'::uuid,
        '1049b51b-a56f-4d0c-a148-9fc5be8773d8'::uuid
      ),
      (
        '28f7e091-252e-5357-b64f-144f6445e2e8'::uuid,
        '8730dee5-5221-4948-a525-358397dffcb1'::uuid
      ),
      (
        'daa5a659-ba7c-5841-adb2-6aaa7c0481ae'::uuid,
        '73a4e07f-f77e-4442-87ed-5a2aa62aa972'::uuid
      )
    ) AS expected(admission_id,batch_id)
    WHERE EXISTS (
      SELECT 1
      FROM memory.v5_local_stage_admission_batch_v2(
        expected.admission_id
      ) AS actual
      WHERE actual.batch_id=expected.batch_id
    )
  ) <> 3 THEN
    RAISE EXCEPTION 'exact reviewed-observation batch mappings missing';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_2_reviewed_observation_stage_v1(20)
    WHERE route_event_id=ANY(ARRAY[
      '0ee7e138-6fd8-5b4d-80bb-3a627c81301a'::uuid,
      'a8cdb502-f3e9-55dd-a804-a8d3f7096005'::uuid,
      '55b2ab8f-7b3c-5b38-af3c-98a237472082'::uuid
    ])
  ) THEN
    RAISE EXCEPTION 'applied sources remain eligible';
  END IF;
END
$verify$;

COMMIT;
