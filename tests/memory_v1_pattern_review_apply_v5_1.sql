\set ON_ERROR_STOP on

DO $catalog$
DECLARE relation_name text; api regprocedure;
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'pattern review/apply V5.1 test requires sage';
  END IF;
  FOREACH relation_name IN ARRAY ARRAY[
    'pattern_review_v5_1','pattern_review_observation_v5_1',
    'pattern_apply_event_v5_1','pattern_operation_request_v5_1'
  ] LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_class AS relation
      JOIN pg_namespace AS namespace ON namespace.oid=relation.relnamespace
      WHERE namespace.nspname='memory' AND relation.relname=relation_name
        AND relation.relrowsecurity AND relation.relforcerowsecurity
    ) THEN
      RAISE EXCEPTION 'memory.% is missing forced RLS',relation_name;
    END IF;
    IF has_table_privilege('brains_app',format('memory.%I',relation_name),'SELECT')
       OR has_table_privilege('brains_app',format('memory.%I',relation_name),'INSERT')
       OR has_table_privilege('brains_app',format('memory.%I',relation_name),'UPDATE')
       OR has_table_privilege('brains_app',format('memory.%I',relation_name),'DELETE') THEN
      RAISE EXCEPTION 'brains_app has direct access to memory.%',relation_name;
    END IF;
  END LOOP;
  FOREACH api IN ARRAY ARRAY[
    'memory.preflight_pattern_review_v5_1(jsonb,memory.pattern_review_decision_v5_1,text,text,jsonb)'::regprocedure,
    'memory.review_pattern_v5_1(uuid,jsonb,memory.pattern_review_decision_v5_1,text,text,jsonb,text)'::regprocedure,
    'memory.preflight_pattern_apply_v5_1(uuid)'::regprocedure,
    'memory.apply_pattern_review_v5_1(uuid,uuid,text)'::regprocedure
  ] LOOP
    IF NOT has_function_privilege('brains_app',api,'EXECUTE')
       OR has_function_privilege('public',api,'EXECUTE') THEN
      RAISE EXCEPTION 'pattern API execute boundary is wrong for %',api;
    END IF;
    IF EXISTS (
      SELECT 1 FROM pg_proc
      WHERE oid=api AND (
        NOT prosecdef
        OR proowner<>'memory_v5_epistemic_writer'::regrole::oid
        OR proconfig<>ARRAY['search_path=""']::text[]
      )
    ) THEN
      RAISE EXCEPTION 'pattern API is not a locked writer definer: %',api;
    END IF;
  END LOOP;
  IF has_function_privilege(
       'brains_app','memory.pattern_proposal_sha256_v5_1(jsonb)','EXECUTE'
     ) OR has_function_privilege(
       'brains_app','memory.pattern_head_state_v5_1(text)','EXECUTE'
     ) OR has_function_privilege(
       'brains_app','memory.guard_pattern_head_update_v5_1()','EXECUTE'
     ) THEN
    RAISE EXCEPTION 'brains_app can execute an internal pattern helper';
  END IF;
END
$catalog$;

BEGIN;
SET LOCAL statement_timeout='30s';

INSERT INTO memory.predicate(predicate,object_kind,cardinality,description)
VALUES ('test.pattern_preference_v5_1','literal','many','Rollback-only pattern fixture');
INSERT INTO memory.predicate_registry_version(
  registry_version,contract_version,status,runtime_active,
  unknown_predicate_action,registry_sha256
) VALUES (
  'memory_predicate_registry_v5','memory_v1_relational_extraction_v5',
  'proposed',false,'defer_unregistered_predicate',repeat('9',64)
) ON CONFLICT (registry_version) DO NOTHING;
INSERT INTO memory.predicate_contract(
  predicate,registry_version,lifecycle,extraction_allowed,object_kind,
  cardinality,successor_predicates,contract,contract_sha256
) VALUES (
  'test.pattern_preference_v5_1','memory_predicate_registry_v5','active',true,'literal',
  'many','{}','{}',repeat('8',64)
);

INSERT INTO memory.entity(
  entity_id,owner_user_id,entity_key,entity_type,canonical_name,
  normalized_name,metadata
) VALUES
  ('e6100000-0000-4000-8000-000000000001',
   '11111111-1111-4111-8111-111111111111','self','self','Owner A','owner a',
   '{"identity_state":"trusted_owner_self"}'::jsonb),
  ('e6200000-0000-4000-8000-000000000001',
   '22222222-2222-4222-8222-222222222222','self','self','Owner B','owner b',
   '{"identity_state":"trusted_owner_self"}'::jsonb);

INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,content,
  content_sha256,observed_at,directness,source_reliability,independence_key
) VALUES
  ('a6100000-0000-4000-8000-000000000001',
   '11111111-1111-4111-8111-111111111111','user_statement','test.pattern','a-1',
   'I enjoy classical music.',repeat('1',64),'2026-05-01T12:00:00Z',1,1,'a-1'),
  ('a6100000-0000-4000-8000-000000000002',
   '11111111-1111-4111-8111-111111111111','user_statement','test.pattern','a-2',
   'Classical music is still a favorite.',repeat('2',64),'2026-06-01T12:00:00Z',1,1,'a-2'),
  ('a6100000-0000-4000-8000-000000000003',
   '11111111-1111-4111-8111-111111111111','user_statement','test.pattern','a-3',
   'I listened to classical music again.',repeat('3',64),'2026-07-01T12:00:00Z',1,1,'a-3'),
  ('a6200000-0000-4000-8000-000000000001',
   '22222222-2222-4222-8222-222222222222','user_statement','test.pattern','b-1',
   'I enjoy jazz.',repeat('4',64),'2026-07-01T12:00:00Z',1,1,'b-1');

SELECT set_config('app.user_id','11111111-1111-4111-8111-111111111111',true);
INSERT INTO memory.entity_mention(
  mention_id,owner_user_id,evidence_id,packet_sha256,entity_ref,entity_type,
  mention_kind,source_spans,extraction_confidence,extractor,
  extractor_version,mention_sha256
) VALUES
  ('b6100000-0000-4000-8000-000000000001','11111111-1111-4111-8111-111111111111',
   'a6100000-0000-4000-8000-000000000001',repeat('a',64),'e01','self',
   'self_reference','[{"start":0,"end":1,"span_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]',
   1,'pattern_test','v5.1',repeat('1',64)),
  ('b6100000-0000-4000-8000-000000000002','11111111-1111-4111-8111-111111111111',
   'a6100000-0000-4000-8000-000000000002',repeat('b',64),'e01','self',
   'self_reference','[{"start":0,"end":1,"span_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}]',
   1,'pattern_test','v5.1',repeat('2',64)),
  ('b6100000-0000-4000-8000-000000000003','11111111-1111-4111-8111-111111111111',
   'a6100000-0000-4000-8000-000000000003',repeat('c',64),'e01','self',
   'self_reference','[{"start":0,"end":1,"span_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}]',
   1,'pattern_test','v5.1',repeat('3',64));

SELECT set_config('app.user_id','22222222-2222-4222-8222-222222222222',true);
INSERT INTO memory.entity_mention(
  mention_id,owner_user_id,evidence_id,packet_sha256,entity_ref,entity_type,
  mention_kind,source_spans,extraction_confidence,extractor,
  extractor_version,mention_sha256
) VALUES
  ('b6200000-0000-4000-8000-000000000001','22222222-2222-4222-8222-222222222222',
   'a6200000-0000-4000-8000-000000000001',repeat('d',64),'e01','self',
   'self_reference','[{"start":0,"end":1,"span_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}]',
   1,'pattern_test','v5.1',repeat('4',64));

SELECT set_config('app.user_id','11111111-1111-4111-8111-111111111111',true);
INSERT INTO memory.entity_resolution_plan(
  resolution_id,owner_user_id,evidence_id,mention_id,predicate_registry_version,
  entity_normalization_version,resolver,resolver_version,action,decision_state,
  selected_entity_id,candidate_set_sha256,decision_sha256
) VALUES
  ('c6100000-0000-4000-8000-000000000001','11111111-1111-4111-8111-111111111111',
   'a6100000-0000-4000-8000-000000000001','b6100000-0000-4000-8000-000000000001',
   'memory_predicate_registry_v5','memory_entity_normalization_v5','pattern_test','v5.1',
   'link_existing','auto_link_eligible','e6100000-0000-4000-8000-000000000001',repeat('1',64),repeat('a',64)),
  ('c6100000-0000-4000-8000-000000000002','11111111-1111-4111-8111-111111111111',
   'a6100000-0000-4000-8000-000000000002','b6100000-0000-4000-8000-000000000002',
   'memory_predicate_registry_v5','memory_entity_normalization_v5','pattern_test','v5.1',
   'link_existing','auto_link_eligible','e6100000-0000-4000-8000-000000000001',repeat('2',64),repeat('b',64)),
  ('c6100000-0000-4000-8000-000000000003','11111111-1111-4111-8111-111111111111',
   'a6100000-0000-4000-8000-000000000003','b6100000-0000-4000-8000-000000000003',
   'memory_predicate_registry_v5','memory_entity_normalization_v5','pattern_test','v5.1',
   'link_existing','auto_link_eligible','e6100000-0000-4000-8000-000000000001',repeat('3',64),repeat('c',64));

SELECT set_config('app.user_id','22222222-2222-4222-8222-222222222222',true);
INSERT INTO memory.entity_resolution_plan(
  resolution_id,owner_user_id,evidence_id,mention_id,predicate_registry_version,
  entity_normalization_version,resolver,resolver_version,action,decision_state,
  selected_entity_id,candidate_set_sha256,decision_sha256
) VALUES
  ('c6200000-0000-4000-8000-000000000001','22222222-2222-4222-8222-222222222222',
   'a6200000-0000-4000-8000-000000000001','b6200000-0000-4000-8000-000000000001',
   'memory_predicate_registry_v5','memory_entity_normalization_v5','pattern_test','v5.1',
   'link_existing','auto_link_eligible','e6200000-0000-4000-8000-000000000001',repeat('4',64),repeat('d',64));

SELECT set_config('app.user_id','11111111-1111-4111-8111-111111111111',true);
INSERT INTO memory.entity_resolution_candidate(
  owner_user_id,resolution_id,candidate_entity_id,ordinal,entity_type,
  features,exclusion_reasons
) VALUES
  ('11111111-1111-4111-8111-111111111111','c6100000-0000-4000-8000-000000000001','e6100000-0000-4000-8000-000000000001',1,'self','{"active_status":true,"entity_type_match":true,"exact_canonical_name":false,"exact_alias":false,"relationship_role_supported":false,"source_local_coreference":true,"graph_neighbor_supported":true,"conflicting_attribute_count":0,"same_name_candidate_count":1}','[]'),
  ('11111111-1111-4111-8111-111111111111','c6100000-0000-4000-8000-000000000002','e6100000-0000-4000-8000-000000000001',1,'self','{"active_status":true,"entity_type_match":true,"exact_canonical_name":false,"exact_alias":false,"relationship_role_supported":false,"source_local_coreference":true,"graph_neighbor_supported":true,"conflicting_attribute_count":0,"same_name_candidate_count":1}','[]'),
  ('11111111-1111-4111-8111-111111111111','c6100000-0000-4000-8000-000000000003','e6100000-0000-4000-8000-000000000001',1,'self','{"active_status":true,"entity_type_match":true,"exact_canonical_name":false,"exact_alias":false,"relationship_role_supported":false,"source_local_coreference":true,"graph_neighbor_supported":true,"conflicting_attribute_count":0,"same_name_candidate_count":1}','[]');

SELECT set_config('app.user_id','22222222-2222-4222-8222-222222222222',true);
INSERT INTO memory.entity_resolution_candidate(
  owner_user_id,resolution_id,candidate_entity_id,ordinal,entity_type,
  features,exclusion_reasons
) VALUES
  ('22222222-2222-4222-8222-222222222222','c6200000-0000-4000-8000-000000000001','e6200000-0000-4000-8000-000000000001',1,'self','{"active_status":true,"entity_type_match":true,"exact_canonical_name":false,"exact_alias":false,"relationship_role_supported":false,"source_local_coreference":true,"graph_neighbor_supported":true,"conflicting_attribute_count":0,"same_name_candidate_count":1}','[]');

SELECT set_config('app.user_id','11111111-1111-4111-8111-111111111111',true);
INSERT INTO memory.entity_resolution_apply(
  owner_user_id,resolution_id,applied_entity_id,apply_manifest_sha256
) VALUES
  ('11111111-1111-4111-8111-111111111111','c6100000-0000-4000-8000-000000000001','e6100000-0000-4000-8000-000000000001',repeat('5',64)),
  ('11111111-1111-4111-8111-111111111111','c6100000-0000-4000-8000-000000000002','e6100000-0000-4000-8000-000000000001',repeat('6',64)),
  ('11111111-1111-4111-8111-111111111111','c6100000-0000-4000-8000-000000000003','e6100000-0000-4000-8000-000000000001',repeat('7',64));

SELECT set_config('app.user_id','22222222-2222-4222-8222-222222222222',true);
INSERT INTO memory.entity_resolution_apply(
  owner_user_id,resolution_id,applied_entity_id,apply_manifest_sha256
) VALUES
  ('22222222-2222-4222-8222-222222222222','c6200000-0000-4000-8000-000000000001','e6200000-0000-4000-8000-000000000001',repeat('8',64));

SELECT set_config('app.user_id','11111111-1111-4111-8111-111111111111',true);
INSERT INTO memory.observation(
  observation_id,owner_user_id,evidence_id,observation_ref,
  subject_mention_id,predicate,predicate_registry_version,object_literal,
  polarity,modality,projection_class,surface_policy,project_scope,sensitivity,
  extraction_confidence,source_spans,extractor,extractor_version,
  packet_sha256,observation_sha256
) VALUES
  ('d6100000-0000-4000-8000-000000000001','11111111-1111-4111-8111-111111111111','a6100000-0000-4000-8000-000000000001','o01','b6100000-0000-4000-8000-000000000001','test.pattern_preference_v5_1','memory_predicate_registry_v5','{"kind":"literal","datatype":"text","value":"classical music","unit":null,"approximate":false}','affirmed','asserted','direct_claim','direct_or_relevant','{"state":"not_applicable","project_key":null,"binding_source":"not_applicable"}','medium',1,'[{"start":0,"end":10,"span_sha256":"1111111111111111111111111111111111111111111111111111111111111111"}]','pattern_test','v5.1',repeat('a',64),repeat('1',64)),
  ('d6100000-0000-4000-8000-000000000002','11111111-1111-4111-8111-111111111111','a6100000-0000-4000-8000-000000000002','o01','b6100000-0000-4000-8000-000000000002','test.pattern_preference_v5_1','memory_predicate_registry_v5','{"kind":"literal","datatype":"text","value":"classical music","unit":null,"approximate":false}','affirmed','asserted','direct_claim','direct_or_relevant','{"state":"not_applicable","project_key":null,"binding_source":"not_applicable"}','medium',1,'[{"start":0,"end":10,"span_sha256":"2222222222222222222222222222222222222222222222222222222222222222"}]','pattern_test','v5.1',repeat('b',64),repeat('2',64)),
  ('d6100000-0000-4000-8000-000000000003','11111111-1111-4111-8111-111111111111','a6100000-0000-4000-8000-000000000003','o01','b6100000-0000-4000-8000-000000000003','test.pattern_preference_v5_1','memory_predicate_registry_v5','{"kind":"literal","datatype":"text","value":"classical music","unit":null,"approximate":false}','affirmed','asserted','direct_claim','direct_or_relevant','{"state":"not_applicable","project_key":null,"binding_source":"not_applicable"}','medium',1,'[{"start":0,"end":10,"span_sha256":"3333333333333333333333333333333333333333333333333333333333333333"}]','pattern_test','v5.1',repeat('c',64),repeat('3',64));

SELECT set_config('app.user_id','22222222-2222-4222-8222-222222222222',true);
INSERT INTO memory.observation(
  observation_id,owner_user_id,evidence_id,observation_ref,
  subject_mention_id,predicate,predicate_registry_version,object_literal,
  polarity,modality,projection_class,surface_policy,project_scope,sensitivity,
  extraction_confidence,source_spans,extractor,extractor_version,
  packet_sha256,observation_sha256
) VALUES
  ('d6200000-0000-4000-8000-000000000001','22222222-2222-4222-8222-222222222222','a6200000-0000-4000-8000-000000000001','o01','b6200000-0000-4000-8000-000000000001','test.pattern_preference_v5_1','memory_predicate_registry_v5','{"kind":"literal","datatype":"text","value":"jazz","unit":null,"approximate":false}','affirmed','asserted','direct_claim','direct_or_relevant','{"state":"not_applicable","project_key":null,"binding_source":"not_applicable"}','medium',1,'[{"start":0,"end":10,"span_sha256":"4444444444444444444444444444444444444444444444444444444444444444"}]','pattern_test','v5.1',repeat('d',64),repeat('4',64));

SELECT set_config('app.user_id','11111111-1111-4111-8111-111111111111',true);
INSERT INTO memory.observation_entity_binding(
  owner_user_id,observation_id,subject_resolution_id,subject_entity_id,
  binding_manifest_sha256
) VALUES
  ('11111111-1111-4111-8111-111111111111','d6100000-0000-4000-8000-000000000001','c6100000-0000-4000-8000-000000000001','e6100000-0000-4000-8000-000000000001',repeat('1',64)),
  ('11111111-1111-4111-8111-111111111111','d6100000-0000-4000-8000-000000000002','c6100000-0000-4000-8000-000000000002','e6100000-0000-4000-8000-000000000001',repeat('2',64)),
  ('11111111-1111-4111-8111-111111111111','d6100000-0000-4000-8000-000000000003','c6100000-0000-4000-8000-000000000003','e6100000-0000-4000-8000-000000000001',repeat('3',64));

SELECT set_config('app.user_id','22222222-2222-4222-8222-222222222222',true);
INSERT INTO memory.observation_entity_binding(
  owner_user_id,observation_id,subject_resolution_id,subject_entity_id,
  binding_manifest_sha256
) VALUES
  ('22222222-2222-4222-8222-222222222222','d6200000-0000-4000-8000-000000000001','c6200000-0000-4000-8000-000000000001','e6200000-0000-4000-8000-000000000001',repeat('4',64));

SELECT set_config('app.user_id','11111111-1111-4111-8111-111111111111',true);

CREATE FUNCTION pg_temp.finalize_pattern_proposal(value jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE manifest text;
BEGIN
  SELECT memory.v5_digest_text(string_agg(concat_ws('|',
    item->>'observation_id',item->>'evidence_id',item->>'observation_role',
    item->>'episode_key_sha256',item->>'independence_key_sha256',
    item->>'temporal_bucket_sha256',item->>'observation_sha256',
    item->>'evidence_content_sha256'
  ),E'\n' ORDER BY item->>'observation_id')) INTO manifest
  FROM jsonb_array_elements(value->'observation_items') AS item;
  value:=jsonb_set(value,'{input_manifest_sha256}',to_jsonb(manifest));
  value:=jsonb_set(value,'{proposal_sha256}',
    to_jsonb(memory.pattern_proposal_sha256_v5_1(value)));
  RETURN value;
END
$function$;

DO $proposals$
DECLARE proposal jsonb; inflated jsonb; cross_owner jsonb; association jsonb;
BEGIN
  proposal:=jsonb_build_object(
    'contract_version','memory_v1_pattern_review_v5_1',
    'policy_version','memory_v1_epistemic_pattern_salience_policy_v5_1',
    'source_registry_version','memory_predicate_registry_v5_1',
    'pattern_key_sha256',memory.v5_digest_text(concat_ws('|',
      'memory_v1_pattern_key_v5_1','recurrence',
      'e6100000-0000-4000-8000-000000000001','','test.pattern_preference_v5_1')),
    'pattern_kind','recurrence',
    'subject_entity_id','e6100000-0000-4000-8000-000000000001',
    'secondary_entity_id',NULL,'predicate_family','test.pattern_preference_v5_1',
    'sensitivity','medium','pattern_state','emerging',
    'occurrence_count',3,'counterexample_count',0,
    'independent_episode_count',3,'distinct_temporal_bucket_count',3,
    'valid_from','2026-05-01','valid_to',NULL,
    'automatic_review_eligible',true,
    'identity_inference_forbidden',true,'causal_inference_forbidden',true,
    'reason_codes',jsonb_build_array('inferred_recurrence_threshold_met'),
    'method_version','memory_v1_pattern_policy_v5_1',
    'input_manifest_sha256',repeat('0',64),
    'observation_items',jsonb_build_array(
      jsonb_build_object('observation_id','d6100000-0000-4000-8000-000000000001','evidence_id','a6100000-0000-4000-8000-000000000001','observation_role','occurrence','episode_key_sha256',repeat('a',64),'independence_key_sha256',repeat('1',64),'temporal_bucket_sha256',repeat('4',64),'observation_sha256',repeat('1',64),'evidence_content_sha256',repeat('1',64)),
      jsonb_build_object('observation_id','d6100000-0000-4000-8000-000000000002','evidence_id','a6100000-0000-4000-8000-000000000002','observation_role','occurrence','episode_key_sha256',repeat('b',64),'independence_key_sha256',repeat('2',64),'temporal_bucket_sha256',repeat('5',64),'observation_sha256',repeat('2',64),'evidence_content_sha256',repeat('2',64)),
      jsonb_build_object('observation_id','d6100000-0000-4000-8000-000000000003','evidence_id','a6100000-0000-4000-8000-000000000003','observation_role','occurrence','episode_key_sha256',repeat('c',64),'independence_key_sha256',repeat('3',64),'temporal_bucket_sha256',repeat('6',64),'observation_sha256',repeat('3',64),'evidence_content_sha256',repeat('3',64))
    ),'proposal_sha256',repeat('0',64)
  );
  proposal:=pg_temp.finalize_pattern_proposal(proposal);
  inflated:=jsonb_set(proposal,'{observation_items,1,episode_key_sha256}',to_jsonb(repeat('a',64)));
  inflated:=pg_temp.finalize_pattern_proposal(inflated);
  cross_owner:=jsonb_set(proposal,'{observation_items,2}',jsonb_build_object(
    'observation_id','d6200000-0000-4000-8000-000000000001',
    'evidence_id','a6200000-0000-4000-8000-000000000001',
    'observation_role','occurrence','episode_key_sha256',repeat('d',64),
    'independence_key_sha256',repeat('4',64),
    'temporal_bucket_sha256',repeat('7',64),
    'observation_sha256',repeat('4',64),
    'evidence_content_sha256',repeat('4',64)
  ));
  cross_owner:=pg_temp.finalize_pattern_proposal(cross_owner);
  association:=jsonb_set(proposal,'{pattern_kind}',to_jsonb('co_occurrence'::text));
  association:=jsonb_set(association,'{automatic_review_eligible}','false'::jsonb);
  association:=jsonb_set(association,'{pattern_key_sha256}',to_jsonb(
    memory.v5_digest_text(concat_ws('|','memory_v1_pattern_key_v5_1',
      'co_occurrence','e6100000-0000-4000-8000-000000000001','',
      'test.pattern_preference_v5_1'))));
  association:=pg_temp.finalize_pattern_proposal(association);
  PERFORM set_config('test.pattern_proposal',proposal::text,true);
  PERFORM set_config('test.pattern_inflated',inflated::text,true);
  PERFORM set_config('test.pattern_cross_owner',cross_owner::text,true);
  PERFORM set_config('test.pattern_association',association::text,true);
END
$proposals$;

CREATE FUNCTION pg_temp.assert_preflight_denied(value jsonb, reviewer text)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=''
AS $function$
DECLARE denied boolean:=false;
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_pattern_review_v5_1(
      value,'authorized',reviewer,
      CASE WHEN reviewer IN ('user','admin')
        THEN '11111111-1111-4111-8111-111111111111'
        ELSE 'pattern_policy_v5_1' END,
      jsonb_build_array('rollback_test')
    );
  EXCEPTION WHEN SQLSTATE '23514' OR SQLSTATE '42501' THEN denied:=true;
  END;
  IF NOT denied THEN RAISE EXCEPTION 'unsafe pattern preflight was accepted'; END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_missing_actor_denied()
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=''
AS $function$
DECLARE denied boolean:=false;
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_pattern_review_v5_1(
      current_setting('test.pattern_proposal')::jsonb,'authorized','system',
      'pattern_policy_v5_1',jsonb_build_array('rollback_test')
    );
  EXCEPTION WHEN SQLSTATE '42501' THEN denied:=true;
  END;
  IF NOT denied THEN RAISE EXCEPTION 'missing actor was accepted'; END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_direct_write_denied()
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=''
AS $function$
DECLARE denied boolean:=false;
BEGIN
  BEGIN
    INSERT INTO memory.pattern_operation_request_v5_1(
      owner_user_id,request_id,operation,manifest_sha256,result,invoked_by_session
    ) VALUES ('11111111-1111-4111-8111-111111111111',gen_random_uuid(),
      'review_pattern',repeat('f',64),'{}',session_user);
  EXCEPTION WHEN insufficient_privilege THEN denied:=true;
  END;
  IF NOT denied THEN RAISE EXCEPTION 'brains_app directly wrote pattern state'; END IF;
END
$function$;

CREATE FUNCTION pg_temp.assert_apply_denied(review_value uuid)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=''
AS $function$
DECLARE denied boolean:=false;
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_pattern_apply_v5_1(review_value);
  EXCEPTION WHEN SQLSTATE '23514' THEN denied:=true;
  END;
  IF NOT denied THEN RAISE EXCEPTION 'non-authorized review was applyable'; END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','',true);
SELECT pg_temp.assert_missing_actor_denied();
SELECT set_config('app.user_id','11111111-1111-4111-8111-111111111111',true);
SELECT pg_temp.assert_direct_write_denied();
SELECT pg_temp.assert_preflight_denied(current_setting('test.pattern_inflated')::jsonb,'system');
SELECT pg_temp.assert_preflight_denied(current_setting('test.pattern_cross_owner')::jsonb,'system');
SELECT pg_temp.assert_preflight_denied(current_setting('test.pattern_association')::jsonb,'system');

SELECT * FROM memory.preflight_pattern_review_v5_1(
  current_setting('test.pattern_proposal')::jsonb,'authorized','system',
  'pattern_policy_v5_1',jsonb_build_array('auto_review_threshold_met')
) \gset review_preflight_
SELECT * FROM memory.review_pattern_v5_1(
  'f6100000-0000-4000-8000-000000000001',
  current_setting('test.pattern_proposal')::jsonb,'authorized','system',
  'pattern_policy_v5_1',jsonb_build_array('auto_review_threshold_met'),
  :'review_preflight_authorization_manifest_sha256'
) \gset review_
SELECT 1 / ((:'review_outcome'='applied')::integer);
SELECT 1 / ((:'review_observations_recorded'::integer=3)::integer);

SELECT * FROM memory.review_pattern_v5_1(
  'f6100000-0000-4000-8000-000000000001',
  current_setting('test.pattern_proposal')::jsonb,'authorized','system',
  'pattern_policy_v5_1',jsonb_build_array('auto_review_threshold_met'),
  :'review_preflight_authorization_manifest_sha256'
) \gset review_replay_
SELECT 1 / ((:'review_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'review_replay_observations_recorded'::integer=0)::integer);

SELECT * FROM memory.preflight_pattern_apply_v5_1(:'review_review_id')
\gset apply_preflight_
SELECT * FROM memory.apply_pattern_review_v5_1(
  'f6100000-0000-4000-8000-000000000002',:'review_review_id',
  :'apply_preflight_apply_manifest_sha256'
) \gset apply_
SELECT 1 / ((:'apply_outcome'='applied')::integer);
SELECT 1 / ((:'apply_resulting_revision_number'::integer=1)::integer);
SELECT 1 / ((:'apply_rows_written'::integer=7)::integer);

SELECT * FROM memory.apply_pattern_review_v5_1(
  'f6100000-0000-4000-8000-000000000002',:'review_review_id',
  :'apply_preflight_apply_manifest_sha256'
) \gset apply_replay_
SELECT 1 / ((:'apply_replay_outcome'='replayed')::integer);
SELECT 1 / ((:'apply_replay_rows_written'::integer=0)::integer);

SELECT * FROM memory.preflight_pattern_review_v5_1(
  current_setting('test.pattern_proposal')::jsonb,'rejected','user',
  '11111111-1111-4111-8111-111111111111',
  jsonb_build_array('owner_rejected_pattern')
) \gset rejected_preflight_
SELECT * FROM memory.review_pattern_v5_1(
  'f6100000-0000-4000-8000-000000000003',
  current_setting('test.pattern_proposal')::jsonb,'rejected','user',
  '11111111-1111-4111-8111-111111111111',
  jsonb_build_array('owner_rejected_pattern'),
  :'rejected_preflight_authorization_manifest_sha256'
) \gset rejected_
SELECT pg_temp.assert_apply_denied(:'rejected_review_id');

RESET SESSION AUTHORIZATION;
DO $counts$
BEGIN
  IF (SELECT count(*) FROM memory.pattern_hypothesis_v5_1)<>1
     OR (SELECT count(*) FROM memory.pattern_hypothesis_revision_v5_1)<>1
     OR (SELECT count(*) FROM memory.pattern_observation_link_v5_1)<>3
     OR (SELECT count(*) FROM memory.pattern_apply_event_v5_1)<>1
     OR (SELECT count(*) FROM memory.pattern_review_v5_1)<>2
     OR (SELECT count(*) FROM memory.pattern_review_observation_v5_1)<>6
     OR (SELECT count(*) FROM memory.pattern_operation_request_v5_1)<>3 THEN
    RAISE EXCEPTION 'pattern review/apply bounded row counts changed';
  END IF;
END
$counts$;

DO $head_guard$
DECLARE denied boolean:=false;
BEGIN
  BEGIN
    UPDATE memory.pattern_hypothesis_v5_1 SET predicate_family='tampered';
  EXCEPTION WHEN SQLSTATE '42501' THEN denied:=true;
  END;
  IF NOT denied THEN RAISE EXCEPTION 'pattern stable head fields were mutable'; END IF;
END
$head_guard$;

SET SESSION AUTHORIZATION memory_v5_epistemic_writer;
SELECT set_config('app.user_id','11111111-1111-4111-8111-111111111111',true);
SELECT 1 / (((SELECT count(*) FROM memory.pattern_hypothesis_v5_1)=1)::integer);
SELECT set_config('app.user_id','22222222-2222-4222-8222-222222222222',true);
SELECT 1 / (((SELECT count(*) FROM memory.pattern_hypothesis_v5_1)=0)::integer);
RESET SESSION AUTHORIZATION;

ROLLBACK;
SELECT 'memory_v1_pattern_review_apply_v5_1: PASS' AS result;
