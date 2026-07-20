\set ON_ERROR_STOP on

DO $catalog$
DECLARE
  relation_name text;
  policy_roles oid[];
  api regprocedure;
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'epistemic pattern/salience V5.1 test requires sage';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname='memory_v5_epistemic_writer'
      AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolinherit AND NOT rolbypassrls
  ) OR pg_has_role('brains_app','memory_v5_epistemic_writer','MEMBER') THEN
    RAISE EXCEPTION 'epistemic V5.1 writer role is unsafe';
  END IF;
  FOREACH relation_name IN ARRAY ARRAY[
    'pattern_hypothesis_v5_1',
    'pattern_hypothesis_revision_v5_1',
    'pattern_observation_link_v5_1',
    'epistemic_target_binding_v5_1',
    'epistemic_assessment_snapshot_v5_1',
    'epistemic_assessment_observation_link_v5_1',
    'salience_feature_snapshot_v5_1',
    'retrieval_outcome_signal_v5_1',
    'epistemic_operation_request_v5_1'
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_class AS relation
      JOIN pg_namespace AS namespace ON namespace.oid=relation.relnamespace
      WHERE namespace.nspname='memory' AND relation.relname=relation_name
        AND relation.relrowsecurity AND relation.relforcerowsecurity
    ) THEN
      RAISE EXCEPTION 'memory.% is missing forced RLS',relation_name;
    END IF;
    SELECT policy.polroles INTO policy_roles
    FROM pg_policy AS policy
    WHERE policy.polrelid=format('memory.%I',relation_name)::regclass
      AND policy.polname='owner_isolation';
    IF policy_roles<>ARRAY['memory_v5_epistemic_writer'::regrole::oid] THEN
      RAISE EXCEPTION 'memory.% has an unsafe owner policy',relation_name;
    END IF;
    IF has_table_privilege(
         'brains_app',format('memory.%I',relation_name),'SELECT'
       ) OR has_table_privilege(
         'brains_app',format('memory.%I',relation_name),'INSERT'
       ) OR has_table_privilege(
         'brains_app',format('memory.%I',relation_name),'UPDATE'
       ) OR has_table_privilege(
         'brains_app',format('memory.%I',relation_name),'DELETE'
       ) THEN
      RAISE EXCEPTION 'brains_app has direct access to memory.%',relation_name;
    END IF;
  END LOOP;

  FOREACH api IN ARRAY ARRAY[
    'memory.persist_epistemic_snapshot_packet_v5_1(uuid,jsonb)'::regprocedure,
    'memory.record_retrieval_outcome_signal_v5_1(uuid,uuid,uuid,memory.retrieval_outcome_v5_1,uuid,text)'::regprocedure
  ]
  LOOP
    IF NOT has_function_privilege('brains_app',api,'EXECUTE') THEN
      RAISE EXCEPTION 'brains_app cannot execute required API %',api;
    END IF;
    IF EXISTS (
      SELECT 1 FROM pg_proc AS procedure
      WHERE procedure.oid=api AND (
        NOT procedure.prosecdef
        OR procedure.proowner<>'memory_v5_epistemic_writer'::regrole::oid
        OR procedure.proconfig<>ARRAY['search_path=pg_catalog']::text[]
      )
    ) THEN
      RAISE EXCEPTION 'API % is not a locked epistemic writer definer',api;
    END IF;
    IF has_function_privilege('public',api,'EXECUTE') THEN
      RAISE EXCEPTION 'PUBLIC can execute epistemic API %',api;
    END IF;
  END LOOP;
  IF has_function_privilege(
       'brains_app','memory.require_v5_epistemic_writer_context()','EXECUTE'
     ) OR has_function_privilege(
       'brains_app','memory.epistemic_packet_sha256_v5_1(jsonb)','EXECUTE'
     ) THEN
    RAISE EXCEPTION 'brains_app can execute an internal epistemic helper';
  END IF;
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='memory'
      AND table_name IN (
        'epistemic_assessment_snapshot_v5_1',
        'salience_feature_snapshot_v5_1'
      )
      AND column_name IN (
        'truth','truth_score','confidence','salience','final_score',
        'overall_score','net_support_score'
      )
  ) THEN
    RAISE EXCEPTION 'canonical truth/scalar salience column was installed';
  END IF;
END
$catalog$;

BEGIN;
SET LOCAL statement_timeout='30s';

INSERT INTO memory.predicate(predicate,object_kind,cardinality,description)
VALUES ('test.epistemic_v5_1','literal','one','rollback-only V5.1 test')
ON CONFLICT(predicate) DO NOTHING;

INSERT INTO memory.entity(
  entity_id,owner_user_id,entity_key,entity_type,canonical_name,normalized_name
) VALUES
  ('e5100000-0000-4000-8000-000000000001',
   '11111111-1111-4111-8111-111111111111',
   'self','self','Owner A','owner a'),
  ('e5200000-0000-4000-8000-000000000001',
   '22222222-2222-4222-8222-222222222222',
   'self','self','Owner B','owner b');

INSERT INTO memory.claim(
  claim_id,owner_user_id,subject_entity_id,predicate,object_literal,
  canonical_text,canonical_key,status
) VALUES
  ('c5100000-0000-4000-8000-000000000001',
   '11111111-1111-4111-8111-111111111111',
   'e5100000-0000-4000-8000-000000000001',
   'test.epistemic_v5_1','{"value":"alpha"}'::jsonb,
   'Rollback-only owner A claim','test:epistemic:v5_1:owner_a','candidate'),
  ('c5200000-0000-4000-8000-000000000001',
   '22222222-2222-4222-8222-222222222222',
   'e5200000-0000-4000-8000-000000000001',
   'test.epistemic_v5_1','{"value":"beta"}'::jsonb,
   'Rollback-only owner B claim','test:epistemic:v5_1:owner_b','candidate');

INSERT INTO memory.claim_revision(
  revision_id,owner_user_id,claim_id,revision_number,snapshot,reason,actor_type
) VALUES
  ('d5100000-0000-4000-8000-000000000001',
   '11111111-1111-4111-8111-111111111111',
   'c5100000-0000-4000-8000-000000000001',1,
   '{"revision":1}'::jsonb,'rollback-only test','system'),
  ('d5200000-0000-4000-8000-000000000001',
   '22222222-2222-4222-8222-222222222222',
   'c5200000-0000-4000-8000-000000000001',1,
   '{"revision":1}'::jsonb,'rollback-only test','system');

INSERT INTO memory.retrieval_trace(
  trace_id,owner_user_id,query_hash,intent,domain,token_budget,selected_count
) VALUES
  ('75100000-0000-4000-8000-000000000001',
   '11111111-1111-4111-8111-111111111111',repeat('1',64),
   'SPECIFIC_RECALL','personal',128,1),
  ('75200000-0000-4000-8000-000000000001',
   '22222222-2222-4222-8222-222222222222',repeat('2',64),
   'SPECIFIC_RECALL','personal',128,1);

DO $packets$
DECLARE
  packet jsonb;
  conflicting jsonb;
BEGIN
  packet:=jsonb_build_object(
    'contract_version','memory_v1_epistemic_pattern_salience_v5_1',
    'policy_version','memory_v1_epistemic_pattern_salience_policy_v5_1',
    'source_registry_version','memory_predicate_registry_v5_1',
    'target',jsonb_build_object(
      'target_kind','claim',
      'target_id','c5100000-0000-4000-8000-000000000001',
      'target_revision_number',1,
      'semantic_key_sha256',memory.v5_digest_text('test:epistemic:v5_1:owner_a')
    ),
    'evidence_assessment',jsonb_build_object(
      'assessment_state','insufficient',
      'supporting_observation_count',0,
      'opposing_observation_count',0,
      'qualifying_observation_count',0,
      'corrective_observation_count',0,
      'independent_support_cluster_count',0,
      'independent_opposition_cluster_count',0,
      'source_diversity_count',0,
      'supporting_observation_ids','[]'::jsonb,
      'opposing_observation_ids','[]'::jsonb,
      'qualifying_observation_ids','[]'::jsonb,
      'corrective_observation_ids','[]'::jsonb,
      'dimensions',jsonb_build_object(
        'directness',0,'source_reliability',0,'independence',0,
        'relevance',0,'temporal_fit',0,'specificity',0,
        'extraction_quality',1,'support_strength',0,'opposition_strength',0
      ),
      'method','deterministic_policy',
      'method_version','memory_v1_epistemic_assessment_v5_1',
      'inputs_sha256',repeat('a',64)
    ),
    'pattern_assessment',NULL,
    'salience_features',jsonb_build_object(
      'importance',0.2,'frequency',0,'recency',0.8,
      'emotional_significance',0,'goal_relevance',0,
      'future_utility',0,'retrieval_utility',0,
      'contradiction_pressure',0,'as_of_date','2026-07-20',
      'method_version','memory_v1_salience_features_v5_1',
      'signal_manifest_sha256',repeat('b',64)
    ),
    'retrieval_history',jsonb_build_object(
      'eligible_count',0,'selected_count',0,'injected_count',0,
      'explicitly_helpful_count',0,'explicitly_confirmed_count',0,
      'corrected_count',0,'not_relevant_count',0,'caused_confusion_count',0,
      'last_retrieval_trace_id',NULL
    ),
    'reason_codes',jsonb_build_array('insufficient_evidence'),
    'input_manifest_sha256',repeat('a',64),
    'packet_sha256',repeat('0',64)
  );
  packet:=jsonb_set(
    packet,'{packet_sha256}',
    to_jsonb(memory.epistemic_packet_sha256_v5_1(packet))
  );
  conflicting:=jsonb_set(packet,'{salience_features,recency}','0.7'::jsonb);
  conflicting:=jsonb_set(
    conflicting,'{packet_sha256}',
    to_jsonb(memory.epistemic_packet_sha256_v5_1(conflicting))
  );
  PERFORM set_config('test.epistemic_packet',packet::text,true);
  PERFORM set_config('test.epistemic_conflicting_packet',conflicting::text,true);
END
$packets$;

CREATE FUNCTION pg_temp.assert_direct_write_denied()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE denied boolean:=false;
BEGIN
  BEGIN
    INSERT INTO memory.epistemic_operation_request_v5_1(
      owner_user_id,request_id,operation,manifest_sha256,result,invoked_by_session
    ) VALUES (
      '11111111-1111-4111-8111-111111111111',gen_random_uuid(),
      'persist_snapshot_packet',repeat('f',64),'{}'::jsonb,session_user
    );
  EXCEPTION WHEN insufficient_privilege THEN denied:=true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'brains_app directly wrote epistemic audit state';
  END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','11111111-1111-4111-8111-111111111111',true);
SELECT pg_temp.assert_direct_write_denied();

DO $apply_replay$
DECLARE
  first_result record;
  replay_result record;
  conflict_denied boolean:=false;
BEGIN
  SELECT * INTO first_result
  FROM memory.persist_epistemic_snapshot_packet_v5_1(
    'a5100000-0000-4000-8000-000000000001',
    current_setting('test.epistemic_packet')::jsonb
  );
  IF first_result.apply_outcome<>'applied' THEN
    RAISE EXCEPTION 'epistemic snapshot was not applied';
  END IF;
  PERFORM set_config(
    'test.target_binding_id',first_result.target_binding_id::text,true
  );
  SELECT * INTO replay_result
  FROM memory.persist_epistemic_snapshot_packet_v5_1(
    'a5100000-0000-4000-8000-000000000001',
    current_setting('test.epistemic_packet')::jsonb
  );
  IF replay_result.apply_outcome<>'replayed'
     OR replay_result.target_binding_id<>first_result.target_binding_id
     OR replay_result.assessment_snapshot_id<>first_result.assessment_snapshot_id
     OR replay_result.feature_snapshot_id<>first_result.feature_snapshot_id THEN
    RAISE EXCEPTION 'epistemic snapshot replay was not zero-write';
  END IF;
  BEGIN
    PERFORM * FROM memory.persist_epistemic_snapshot_packet_v5_1(
      'a5100000-0000-4000-8000-000000000001',
      current_setting('test.epistemic_conflicting_packet')::jsonb
    );
  EXCEPTION WHEN check_violation THEN conflict_denied:=true;
  END;
  IF NOT conflict_denied THEN
    RAISE EXCEPTION 'conflicting epistemic request replay was accepted';
  END IF;
END
$apply_replay$;

RESET SESSION AUTHORIZATION;
DO $counts$
BEGIN
  IF (SELECT count(*) FROM memory.epistemic_target_binding_v5_1)<>1
     OR (SELECT count(*) FROM memory.epistemic_assessment_snapshot_v5_1)<>1
     OR (SELECT count(*) FROM memory.salience_feature_snapshot_v5_1)<>1
     OR (SELECT count(*) FROM memory.epistemic_operation_request_v5_1)<>1
     OR (SELECT count(*) FROM memory.epistemic_assessment_observation_link_v5_1)<>0 THEN
    RAISE EXCEPTION 'epistemic snapshot bounded row counts changed';
  END IF;
END
$counts$;

DO $signal_hash$
DECLARE calculated text;
BEGIN
  calculated:=memory.v5_digest_text(memory.v5_canonical_json_text(
    jsonb_build_object(
      'contract_version','memory_v1_retrieval_outcome_signal_v5_1',
      'owner_user_id','11111111-1111-4111-8111-111111111111',
      'trace_id','75100000-0000-4000-8000-000000000001',
      'target_binding_id',current_setting('test.target_binding_id'),
      'outcome','selected','source_evidence_id',NULL
    )
  ));
  PERFORM set_config('test.signal_sha256',calculated,true);
END
$signal_hash$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','11111111-1111-4111-8111-111111111111',true);
DO $signal_replay$
DECLARE first_result record; replay_result record;
BEGIN
  SELECT * INTO first_result
  FROM memory.record_retrieval_outcome_signal_v5_1(
    'a5100000-0000-4000-8000-000000000002',
    '75100000-0000-4000-8000-000000000001',
    current_setting('test.target_binding_id')::uuid,
    'selected',NULL,current_setting('test.signal_sha256')
  );
  SELECT * INTO replay_result
  FROM memory.record_retrieval_outcome_signal_v5_1(
    'a5100000-0000-4000-8000-000000000002',
    '75100000-0000-4000-8000-000000000001',
    current_setting('test.target_binding_id')::uuid,
    'selected',NULL,current_setting('test.signal_sha256')
  );
  IF first_result.apply_outcome<>'applied'
     OR replay_result.apply_outcome<>'replayed'
     OR first_result.signal_id<>replay_result.signal_id THEN
    RAISE EXCEPTION 'retrieval outcome replay was not zero-write';
  END IF;
END
$signal_replay$;

RESET SESSION AUTHORIZATION;
DO $signal_counts$
BEGIN
  IF (SELECT count(*) FROM memory.retrieval_outcome_signal_v5_1)<>1
     OR (SELECT count(*) FROM memory.epistemic_operation_request_v5_1)<>2 THEN
    RAISE EXCEPTION 'retrieval outcome bounded row counts changed';
  END IF;
END
$signal_counts$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','22222222-2222-4222-8222-222222222222',true);
DO $cross_owner$
DECLARE denied boolean:=false;
BEGIN
  BEGIN
    PERFORM * FROM memory.persist_epistemic_snapshot_packet_v5_1(
      'a5200000-0000-4000-8000-000000000001',
      current_setting('test.epistemic_packet')::jsonb
    );
  EXCEPTION WHEN check_violation THEN denied:=true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'cross-owner snapshot target was accepted';
  END IF;
END
$cross_owner$;

SELECT set_config('app.user_id','',true);
DO $missing_actor$
DECLARE denied boolean:=false;
BEGIN
  BEGIN
    PERFORM * FROM memory.persist_epistemic_snapshot_packet_v5_1(
      'a5300000-0000-4000-8000-000000000001',
      current_setting('test.epistemic_packet')::jsonb
    );
  EXCEPTION WHEN insufficient_privilege THEN denied:=true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'epistemic snapshot accepted missing actor context';
  END IF;
END
$missing_actor$;

RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_epistemic_pattern_salience_v5_1: PASS' AS result;
