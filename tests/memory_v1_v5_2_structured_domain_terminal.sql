\set ON_ERROR_STOP on

BEGIN;

SELECT set_config('test.target_owner', :'target_owner', true);
SELECT set_config('test.other_owner', :'other_owner', true);
SELECT set_config('test.packet_id', :'packet_id', true);
SELECT set_config('test.packet_storage_sha256', :'packet_storage_sha256', true);
SELECT set_config('test.observation_packet_id', :'observation_packet_id', true);
SELECT set_config(
  'test.observation_packet_storage_sha256',
  :'observation_packet_storage_sha256',
  true
);
SELECT set_config('app.user_id', :'target_owner', true);

DO $test$
DECLARE
  target record;
  applied record;
  replayed record;
  denied boolean:=false;
BEGIN
  SELECT * INTO target
  FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
  WHERE packet_id=current_setting('test.packet_id')::uuid;
  IF NOT FOUND
     OR target.disposition_route<>'terminal_deferral'
     OR target.reason_code<>'deferral_only_no_stage'
     OR target.entity_mention_count<>0
     OR target.observation_count<>0
     OR target.comparison_hint_count<>0
     OR target.deferral_count<1 THEN
    RAISE EXCEPTION
      'V5.2 structured-domain terminal planner did not select the target';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
    WHERE packet_id=current_setting('test.observation_packet_id')::uuid
  ) THEN
    RAISE EXCEPTION
      'V5.2 relational-content packet entered the terminal planner';
  END IF;

  SELECT * INTO applied
  FROM memory.finalize_owner_v5_local_deferral_v1(
    '88a84cc4-b4e9-5e03-8678-c84263b7a68f'::uuid,
    '45a66877-2ac3-57e9-93e8-d878f1bfa627'::uuid,
    current_setting('test.packet_id')::uuid,
    current_setting('test.packet_storage_sha256'),
    'deferral_only_no_stage'
  );
  SELECT * INTO replayed
  FROM memory.finalize_owner_v5_local_deferral_v1(
    '88a84cc4-b4e9-5e03-8678-c84263b7a68f'::uuid,
    '45a66877-2ac3-57e9-93e8-d878f1bfa627'::uuid,
    current_setting('test.packet_id')::uuid,
    current_setting('test.packet_storage_sha256'),
    'deferral_only_no_stage'
  );
  IF applied.disposition<>'terminal_no_stage'
     OR applied.reason_code<>'deferral_only_no_stage'
     OR applied.apply_outcome<>'applied'
     OR replayed.apply_outcome<>'replayed' THEN
    RAISE EXCEPTION
      'V5.2 structured-domain terminal apply or replay failed';
  END IF;

  BEGIN
    PERFORM *
    FROM memory.finalize_owner_v5_local_deferral_v1(
      '9cef2dad-e7f2-58da-a1bf-da0b78deaa4d'::uuid,
      '16c7462b-eaf4-54a4-9ff4-ef4fe2b251ce'::uuid,
      current_setting('test.observation_packet_id')::uuid,
      current_setting('test.observation_packet_storage_sha256'),
      'deferral_only_no_stage'
    );
  EXCEPTION WHEN check_violation THEN
    denied:=true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION
      'V5.2 relational-content packet was not rejected';
  END IF;

  PERFORM set_config(
    'app.user_id',current_setting('test.other_owner'),true
  );
  IF EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
    WHERE packet_id=current_setting('test.packet_id')::uuid
  ) THEN
    RAISE EXCEPTION 'cross-owner planner exposed the target packet';
  END IF;
  denied:=false;
  BEGIN
    PERFORM *
    FROM memory.finalize_owner_v5_local_deferral_v1(
      '88a84cc4-b4e9-5e03-8678-c84263b7a68f'::uuid,
      '45a66877-2ac3-57e9-93e8-d878f1bfa627'::uuid,
      current_setting('test.packet_id')::uuid,
      current_setting('test.packet_storage_sha256'),
      'deferral_only_no_stage'
    );
  EXCEPTION WHEN check_violation THEN
    denied:=true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'cross-owner terminal replay was not rejected';
  END IF;
END
$test$;

ROLLBACK;

SELECT 'memory_v1_v5_2_structured_domain_terminal: PASS' AS result;
