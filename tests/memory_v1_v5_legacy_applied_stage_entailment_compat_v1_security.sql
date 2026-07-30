\pset pager off

DO $clone_only$
BEGIN
  IF current_database() !~ '^memory_legacy_stage_compat_' THEN
    RAISE EXCEPTION
      'security test requires a disposable legacy-stage clone';
  END IF;
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'security test requires brains_app session';
  END IF;
END
$clone_only$;

BEGIN;
SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

DO $cross_owner_rejected$
DECLARE
  rejected boolean := false;
BEGIN
  PERFORM set_config(
    'app.user_id',
    '673d64a3-c4ba-4d1c-89e3-e0c579022fad',
    true
  );
  BEGIN
    PERFORM *
    FROM memory.register_owner_v5_legacy_stage_compat_v1(
      '69bda9ea-0379-4e9b-afb4-72d8d946bcad'::uuid,
      'b4ec20be-52ed-49b5-918f-0f04f97cff67'::uuid,
      'c5b20c4f-ddd2-4684-bb5a-9ad8b93d7083'::uuid,
      '7877ebf3-1c5a-4bd2-8b43-9514a88a0e12'::uuid,
      '0f47e176-afb3-5a5e-8466-356ce208ab39'::uuid,
      '8f4a048f402899b1d8dfc9b3f05a6aa1673b14a9ffd6c8971f83806b1d59e2b1',
      '198188733b84d3d5eb8f4a30b931fed66f8048a7cfb4ec99f0ebcd08ec4a76df',
      '2c19e94e8d5463c9675aab518b561967b09836d7f5ffd5ca78e038539dafc490',
      '8c2ecaa78031df10bc2ca10701b38a85bb913c42b474f66d5377facebeb44773',
      '770e2c0f513183f462e9fae89bf28b945ae9a2632da9314b2a204eee1ab677d2',
      9,
      'f4a2a795a559794eec68239415936de75a8650af8bef75ac2529ba242bdb548c',
      'ee5259f15fc774674840fd4c3e5861b0f8fe7062',
      'memory_v1_v5_legacy_applied_stage_entailment_policy_v1'
    );
  EXCEPTION
    WHEN no_data_found THEN
      rejected := true;
  END;
  IF NOT rejected THEN
    RAISE EXCEPTION 'cross-owner registration was not rejected';
  END IF;
  IF (
    SELECT count(*)
    FROM memory.plan_owner_v5_local_entailment_v1(20)
  ) <> 0 THEN
    RAISE EXCEPTION 'cross-owner planner exposed target observations';
  END IF;
  PERFORM set_config(
    'app.user_id',
    '1240822d-ac9a-4096-95aa-e2b24d36ef50',
    true
  );
END
$cross_owner_rejected$;

DO $hash_drift_rejected$
DECLARE
  rejected boolean := false;
BEGIN
  BEGIN
    PERFORM *
    FROM memory.register_owner_v5_legacy_stage_compat_v1(
      '27c57d3a-9798-402d-9852-c545bb5eb643'::uuid,
      '7513af47-489a-4132-b301-ed134881ff14'::uuid,
      'c5b20c4f-ddd2-4684-bb5a-9ad8b93d7083'::uuid,
      '7877ebf3-1c5a-4bd2-8b43-9514a88a0e12'::uuid,
      '0f47e176-afb3-5a5e-8466-356ce208ab39'::uuid,
      '8f4a048f402899b1d8dfc9b3f05a6aa1673b14a9ffd6c8971f83806b1d59e2b1',
      '198188733b84d3d5eb8f4a30b931fed66f8048a7cfb4ec99f0ebcd08ec4a76df',
      '2c19e94e8d5463c9675aab518b561967b09836d7f5ffd5ca78e038539dafc490',
      '8c2ecaa78031df10bc2ca10701b38a85bb913c42b474f66d5377facebeb44773',
      '870e2c0f513183f462e9fae89bf28b945ae9a2632da9314b2a204eee1ab677d2',
      9,
      'f4a2a795a559794eec68239415936de75a8650af8bef75ac2529ba242bdb548c',
      'ee5259f15fc774674840fd4c3e5861b0f8fe7062',
      'memory_v1_v5_legacy_applied_stage_entailment_policy_v1'
    );
  EXCEPTION
    WHEN check_violation THEN
      rejected := true;
  END;
  IF NOT rejected THEN
    RAISE EXCEPTION 'observation-set hash drift was not rejected';
  END IF;
END
$hash_drift_rejected$;

CREATE TEMP TABLE first_apply AS
SELECT *
FROM memory.register_owner_v5_legacy_stage_compat_v1(
  '71957a30-3d39-46cd-9198-e4714b3326b9'::uuid,
  '8c751d72-073c-4b33-9431-72838f374003'::uuid,
  'c5b20c4f-ddd2-4684-bb5a-9ad8b93d7083'::uuid,
  '7877ebf3-1c5a-4bd2-8b43-9514a88a0e12'::uuid,
  '0f47e176-afb3-5a5e-8466-356ce208ab39'::uuid,
  '8f4a048f402899b1d8dfc9b3f05a6aa1673b14a9ffd6c8971f83806b1d59e2b1',
  '198188733b84d3d5eb8f4a30b931fed66f8048a7cfb4ec99f0ebcd08ec4a76df',
  '2c19e94e8d5463c9675aab518b561967b09836d7f5ffd5ca78e038539dafc490',
  '8c2ecaa78031df10bc2ca10701b38a85bb913c42b474f66d5377facebeb44773',
  '770e2c0f513183f462e9fae89bf28b945ae9a2632da9314b2a204eee1ab677d2',
  9,
  'f4a2a795a559794eec68239415936de75a8650af8bef75ac2529ba242bdb548c',
  :'repository_commit',
  'memory_v1_v5_legacy_applied_stage_entailment_policy_v1'
);

CREATE TEMP TABLE second_apply AS
SELECT *
FROM memory.register_owner_v5_legacy_stage_compat_v1(
  '67c735b7-c6ca-4edc-b888-43164fd6a578'::uuid,
  'fd48e387-406e-47a8-b87a-b15320d63487'::uuid,
  'e8d7b533-e3f4-4074-9af5-d13a8e7fca5f'::uuid,
  'aaaa0d55-0563-4197-8c8e-a657240e6cc1'::uuid,
  '5bb2e79d-d05f-5e3d-950a-b5235bbe3fb4'::uuid,
  'a3e955221754569704db72d498a8e6f1ef038a85789d9d858ddf671af30ca6ac',
  'fda3771a093ac07a0e69528b9f6807b304c193603c17bfc19e28e6bcea3e01c6',
  '2379bbd6e608361b3a01350420b690592b3e37ab5c474f7a3ef86f7ba848fab5',
  'af432eb456651421c0efc7b3a66e04c5d2b92b77b3cc7e1012295d2a3b76c931',
  'f387b937b577f3cef97d77b00cc58a71608a753bd453634ac809a0ff4c300424',
  11,
  'f4a2a795a559794eec68239415936de75a8650af8bef75ac2529ba242bdb548c',
  :'repository_commit',
  'memory_v1_v5_legacy_applied_stage_entailment_policy_v1'
);

DO $apply_counts$
BEGIN
  IF NOT EXISTS (
      SELECT 1
      FROM first_apply
      WHERE outcome = 'applied'
        AND allowed_observation_count = 9
        AND rows_written = 10
    )
    OR NOT EXISTS (
      SELECT 1
      FROM second_apply
      WHERE outcome = 'applied'
        AND allowed_observation_count = 11
        AND rows_written = 12
    )
  THEN
    RAISE EXCEPTION 'compatibility apply counts drifted';
  END IF;
END
$apply_counts$;

CREATE TEMP TABLE first_replay AS
SELECT *
FROM memory.register_owner_v5_legacy_stage_compat_v1(
  '71957a30-3d39-46cd-9198-e4714b3326b9'::uuid,
  '8c751d72-073c-4b33-9431-72838f374003'::uuid,
  'c5b20c4f-ddd2-4684-bb5a-9ad8b93d7083'::uuid,
  '7877ebf3-1c5a-4bd2-8b43-9514a88a0e12'::uuid,
  '0f47e176-afb3-5a5e-8466-356ce208ab39'::uuid,
  '8f4a048f402899b1d8dfc9b3f05a6aa1673b14a9ffd6c8971f83806b1d59e2b1',
  '198188733b84d3d5eb8f4a30b931fed66f8048a7cfb4ec99f0ebcd08ec4a76df',
  '2c19e94e8d5463c9675aab518b561967b09836d7f5ffd5ca78e038539dafc490',
  '8c2ecaa78031df10bc2ca10701b38a85bb913c42b474f66d5377facebeb44773',
  '770e2c0f513183f462e9fae89bf28b945ae9a2632da9314b2a204eee1ab677d2',
  9,
  'f4a2a795a559794eec68239415936de75a8650af8bef75ac2529ba242bdb548c',
  :'repository_commit',
  'memory_v1_v5_legacy_applied_stage_entailment_policy_v1'
);

CREATE TEMP TABLE second_replay AS
SELECT *
FROM memory.register_owner_v5_legacy_stage_compat_v1(
  '67c735b7-c6ca-4edc-b888-43164fd6a578'::uuid,
  'fd48e387-406e-47a8-b87a-b15320d63487'::uuid,
  'e8d7b533-e3f4-4074-9af5-d13a8e7fca5f'::uuid,
  'aaaa0d55-0563-4197-8c8e-a657240e6cc1'::uuid,
  '5bb2e79d-d05f-5e3d-950a-b5235bbe3fb4'::uuid,
  'a3e955221754569704db72d498a8e6f1ef038a85789d9d858ddf671af30ca6ac',
  'fda3771a093ac07a0e69528b9f6807b304c193603c17bfc19e28e6bcea3e01c6',
  '2379bbd6e608361b3a01350420b690592b3e37ab5c474f7a3ef86f7ba848fab5',
  'af432eb456651421c0efc7b3a66e04c5d2b92b77b3cc7e1012295d2a3b76c931',
  'f387b937b577f3cef97d77b00cc58a71608a753bd453634ac809a0ff4c300424',
  11,
  'f4a2a795a559794eec68239415936de75a8650af8bef75ac2529ba242bdb548c',
  :'repository_commit',
  'memory_v1_v5_legacy_applied_stage_entailment_policy_v1'
);

DO $replay_counts$
BEGIN
  IF EXISTS (
      SELECT 1 FROM first_replay
      WHERE outcome <> 'replayed' OR rows_written <> 0
    )
    OR EXISTS (
      SELECT 1 FROM second_replay
      WHERE outcome <> 'replayed' OR rows_written <> 0
    )
  THEN
    RAISE EXCEPTION 'compatibility replay wrote rows';
  END IF;
END
$replay_counts$;

CREATE TEMP TABLE planned AS
SELECT *
FROM memory.plan_owner_v5_local_entailment_v1(20);

DO $planner_scope$
BEGIN
  IF (SELECT count(*) FROM planned) <> 20
     OR (
       SELECT count(DISTINCT observation_id)
       FROM planned
     ) <> 20
     OR EXISTS (
       SELECT 1
       FROM planned AS plan
       WHERE plan.stage_admission_id NOT IN (
         '8c751d72-073c-4b33-9431-72838f374003'::uuid,
         'fd48e387-406e-47a8-b87a-b15320d63487'::uuid
       )
     )
     OR EXISTS (
       SELECT 1
       FROM planned AS plan
       WHERE plan.observation_id =
         'bc8866ad-95e8-4413-832e-813f601eece6'::uuid
     )
  THEN
    RAISE EXCEPTION 'compatibility planner scope drifted';
  END IF;
END
$planner_scope$;

ROLLBACK;
