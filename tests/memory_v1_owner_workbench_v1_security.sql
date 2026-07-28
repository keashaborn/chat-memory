\set ON_ERROR_STOP on
BEGIN;
SET LOCAL statement_timeout='120s';

DO $preflight$
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'owner memory workbench security test requires brains_app';
  END IF;
  IF to_regclass('memory.owner_packet_feedback_v1') IS NULL
     OR to_regprocedure(
       'memory.owner_memory_workbench_summary_v1()'
     ) IS NULL
     OR to_regprocedure(
       'memory.list_owner_memory_workbench_v1(text,integer,timestamptz,uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.record_owner_memory_workbench_feedback_v1(uuid,uuid,text,text,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'owner memory workbench objects are missing';
  END IF;
  IF NOT (
    SELECT relrowsecurity AND relforcerowsecurity
    FROM pg_class
    WHERE oid='memory.owner_packet_feedback_v1'::regclass
  ) THEN
    RAISE EXCEPTION 'owner memory workbench RLS is not forced';
  END IF;
  IF has_table_privilege(
    'brains_app','memory.owner_packet_feedback_v1','SELECT'
  ) OR has_table_privilege(
    'brains_app','memory.owner_packet_feedback_v1','INSERT'
  ) OR has_table_privilege(
    'brains_app','memory.owner_packet_feedback_v1','UPDATE'
  ) OR has_table_privilege(
    'brains_app','memory.owner_packet_feedback_v1','DELETE'
  ) THEN
    RAISE EXCEPTION 'brains_app has direct workbench table privileges';
  END IF;
  IF has_function_privilege(
    'brains_app',
    'memory.guard_owner_packet_feedback_promotion_v1()',
    'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'brains_app can execute the internal promotion guard';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgrelid='memory.owner_packet_feedback_v1'::regclass
      AND tgname='owner_packet_feedback_v1_append_only_guard'
      AND NOT tgisinternal
  ) OR NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgrelid='memory.v5_local_packet_stage_admission'::regclass
      AND tgname='owner_packet_feedback_stage_guard'
      AND NOT tgisinternal
  ) OR NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgrelid='memory.v5_2_atom_admission_proposal'::regclass
      AND tgname='owner_packet_feedback_atom_guard'
      AND NOT tgisinternal
  ) THEN
    RAISE EXCEPTION 'owner memory workbench guards are missing';
  END IF;
END
$preflight$;

CREATE TEMP TABLE workbench_fixture(
  owner_user_id uuid NOT NULL,
  packet_id uuid NOT NULL,
  packet_storage_sha256 text NOT NULL
);
INSERT INTO workbench_fixture VALUES (
  :'target_owner'::uuid,
  :'target_packet'::uuid,
  :'target_packet_sha256'
);

DO $fixture$
BEGIN
  IF (SELECT count(*) FROM workbench_fixture)<>1 THEN
    RAISE EXCEPTION 'owner memory workbench fixture is unavailable';
  END IF;
END
$fixture$;

SELECT set_config(
  'app.user_id',(SELECT owner_user_id::text FROM workbench_fixture),true
);

DO $owner_read$
BEGIN
  IF (
    SELECT count(*)
    FROM memory.list_owner_memory_workbench_v1('all',25,NULL,NULL)
    WHERE packet_id=(SELECT packet_id FROM workbench_fixture)
  )<>1 THEN
    RAISE EXCEPTION 'owner cannot read own workbench packet';
  END IF;
END
$owner_read$;

CREATE TEMP TABLE first_feedback AS
SELECT *
FROM memory.record_owner_memory_workbench_feedback_v1(
  '11111111-1111-4111-8111-111111111111'::uuid,
  (SELECT packet_id FROM workbench_fixture),
  (SELECT packet_storage_sha256 FROM workbench_fixture),
  'correct',
  NULL
);

DO $first_apply$
BEGIN
  IF (SELECT apply_outcome FROM first_feedback)<>'applied'
     OR (
       SELECT correct_count
       FROM memory.owner_memory_workbench_summary_v1()
     )<>1 THEN
    RAISE EXCEPTION 'first owner feedback apply failed';
  END IF;
END
$first_apply$;

CREATE TEMP TABLE replay_feedback AS
SELECT *
FROM memory.record_owner_memory_workbench_feedback_v1(
  '11111111-1111-4111-8111-111111111111'::uuid,
  (SELECT packet_id FROM workbench_fixture),
  (SELECT packet_storage_sha256 FROM workbench_fixture),
  'correct',
  NULL
);

DO $replay$
BEGIN
  IF (SELECT apply_outcome FROM replay_feedback)<>'replayed'
     OR (
       SELECT correct_count
       FROM memory.owner_memory_workbench_summary_v1()
     )<>1 THEN
    RAISE EXCEPTION 'owner feedback replay is not zero-write';
  END IF;
END
$replay$;

CREATE TEMP TABLE changed_feedback AS
SELECT *
FROM memory.record_owner_memory_workbench_feedback_v1(
  '22222222-2222-4222-8222-222222222222'::uuid,
  (SELECT packet_id FROM workbench_fixture),
  (SELECT packet_storage_sha256 FROM workbench_fixture),
  'not_correct',
  'The interpretation changes the meaning.'
);

DO $changed$
BEGIN
  IF (SELECT apply_outcome FROM changed_feedback)<>'applied'
     OR (
       SELECT not_correct_count
       FROM memory.owner_memory_workbench_summary_v1()
     )<>1
     OR (
       SELECT feedback_decision
       FROM memory.list_owner_memory_workbench_v1('reviewed',25,NULL,NULL)
       WHERE packet_id=(SELECT packet_id FROM workbench_fixture)
     ) IS DISTINCT FROM 'not_correct' THEN
    RAISE EXCEPTION 'append-only owner feedback supersession failed';
  END IF;
END
$changed$;

DO $direct_denial$
BEGIN
  BEGIN
    UPDATE memory.owner_packet_feedback_v1 SET decision='correct';
    RAISE EXCEPTION 'direct feedback update unexpectedly succeeded';
  EXCEPTION
    WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    DELETE FROM memory.owner_packet_feedback_v1;
    RAISE EXCEPTION 'direct feedback delete unexpectedly succeeded';
  EXCEPTION
    WHEN insufficient_privilege THEN NULL;
  END;
END
$direct_denial$;

SELECT set_config(
  'app.user_id','00000000-0000-4000-8000-000000000001',true
);
DO $cross_owner$
BEGIN
  IF (
    SELECT count(*)
    FROM memory.list_owner_memory_workbench_v1('all',25,NULL,NULL)
    WHERE packet_id=(SELECT packet_id FROM workbench_fixture)
  )<>0 THEN
    RAISE EXCEPTION 'cross-owner workbench read succeeded';
  END IF;
  BEGIN
    PERFORM *
    FROM memory.record_owner_memory_workbench_feedback_v1(
      '33333333-3333-4333-8333-333333333333'::uuid,
      (SELECT packet_id FROM workbench_fixture),
      (SELECT packet_storage_sha256 FROM workbench_fixture),
      'correct',
      NULL
    );
    RAISE EXCEPTION 'cross-owner feedback unexpectedly succeeded';
  EXCEPTION
    WHEN invalid_parameter_value THEN NULL;
  END;
END
$cross_owner$;

ROLLBACK;
