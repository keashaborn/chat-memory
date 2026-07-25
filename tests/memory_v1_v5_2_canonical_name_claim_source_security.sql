\set ON_ERROR_STOP on
BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage'
     OR to_regprocedure(
       'memory.preflight_projection_source_v5_2(uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.preflight_projection_entailment_source_v5_2(uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.preflight_reusable_observation_entailment_v5_2(uuid,jsonb)'
     ) IS NULL
     OR to_regprocedure(
       'memory.preflight_projection_temporal_state_v5_2(uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.render_projection_claim_text_temporal_v5_2(uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.guard_projection_item_complete_v5()'
     ) IS NULL THEN
    RAISE EXCEPTION 'canonical-name claim-source functions are absent';
  END IF;
END
$block$;

DO $block$
BEGIN
  IF strpos(
       pg_get_functiondef(
         'memory.guard_projection_item_complete_v5()'::regprocedure
       ),
       'canonical_name_claim_source_v5_2_compat'
     ) = 0
     OR strpos(
       pg_get_functiondef(
         'memory.guard_projection_item_complete_v5()'::regprocedure
       ),
       'stance_reconciliation_v1'
     ) = 0 THEN
    RAISE EXCEPTION
      'projection integrity compatibility is absent or regressed';
  END IF;
END
$block$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

DO $block$
DECLARE
  source record;
  entailment record;
BEGIN
  SELECT * INTO STRICT source
  FROM memory.preflight_projection_source_v5_2(
    '93024235-89a8-49d5-88fa-7e4a143b68f3'::uuid
  );
  IF source.predicate_registry_version<>
       'memory_predicate_registry_v5_2'
     OR source.predicate<>'identity.name_canonical'
     OR source.projection_class<>'direct_claim'
     OR source.modality<>'corrective'
     OR source.subject_entity_id<>
       '09308a2b-3019-4f59-8fc3-bb1fe1408a0d'::uuid
     OR source.subject_entity_type<>'animal'
     OR source.subject_canonical_name<>'Neko'
     OR source.object_kind<>'literal'
     OR source.object_literal->>'value'<>'Neko'
     OR source.surface_policy<>'direct_or_relevant' THEN
    RAISE EXCEPTION 'canonical-name compatibility source drifted';
  END IF;

  SELECT * INTO STRICT entailment
  FROM memory.preflight_projection_entailment_source_v5_2(
    '93024235-89a8-49d5-88fa-7e4a143b68f3'::uuid
  );
  IF entailment.observation_id<>source.observation_id
     OR entailment.observation_sha256<>source.observation_sha256
     OR entailment.evidence_id<>source.evidence_id
     OR entailment.evidence_content_sha256<>
       source.evidence_content_sha256 THEN
    RAISE EXCEPTION 'canonical-name entailment source drifted';
  END IF;
  PERFORM *
  FROM memory.preflight_reusable_observation_entailment_v5_2(
    source.observation_id,
    entailment.source_spans
  );
  IF memory.preflight_projection_temporal_state_v5_2(
       source.observation_id
     ) <> 'not_applicable' THEN
    RAISE EXCEPTION 'canonical-name temporal source drifted';
  END IF;
  IF memory.render_projection_claim_text_temporal_v5_2(
       source.observation_id
     ) <> 'Neko''s canonical name is Neko.' THEN
    RAISE EXCEPTION 'canonical-name rendered text drifted';
  END IF;
END
$block$;

SELECT set_config(
  'app.user_id',
  '557ea042-cb82-48f8-9429-472e96c957ef',
  true
);

DO $block$
BEGIN
  BEGIN
    PERFORM *
    FROM memory.preflight_projection_source_v5_2(
      '93024235-89a8-49d5-88fa-7e4a143b68f3'::uuid
    );
    RAISE EXCEPTION 'cross-owner projection source was visible';
  EXCEPTION
    WHEN no_data_found THEN NULL;
  END;
  BEGIN
    PERFORM memory.preflight_projection_temporal_state_v5_2(
      '93024235-89a8-49d5-88fa-7e4a143b68f3'::uuid
    );
    RAISE EXCEPTION 'cross-owner temporal source was visible';
  EXCEPTION
    WHEN no_data_found THEN NULL;
  END;
  BEGIN
    PERFORM *
    FROM memory.preflight_projection_entailment_source_v5_2(
      '93024235-89a8-49d5-88fa-7e4a143b68f3'::uuid
    );
    RAISE EXCEPTION 'cross-owner entailment source was visible';
  EXCEPTION
    WHEN no_data_found THEN NULL;
  END;
  BEGIN
    PERFORM *
    FROM memory.preflight_reusable_observation_entailment_v5_2(
      '93024235-89a8-49d5-88fa-7e4a143b68f3'::uuid,
      '[
        {
          "start": 66,
          "end": 70,
          "span_sha256":
            "016526330aaf250542e5acc9103d9f663a8a5bb00d1b8607a1b170b6d93d6401"
        },
        {
          "start": 146,
          "end": 159,
          "span_sha256":
            "059fef53c30e4fa6b50dd4cd3a086f67259fae08a268d9cf26dfe081782babbc"
        }
      ]'::jsonb
    );
    RAISE EXCEPTION 'cross-owner reusable entailment was visible';
  EXCEPTION
    WHEN no_data_found THEN NULL;
  END;
END
$block$;

RESET SESSION AUTHORIZATION;

DO $block$
BEGIN
  IF EXISTS (
       SELECT 1
       FROM pg_proc AS procedure
       CROSS JOIN LATERAL aclexplode(
         COALESCE(
           procedure.proacl,
           acldefault('f', procedure.proowner)
         )
       ) AS privilege
       WHERE procedure.oid IN (
         'memory.preflight_projection_source_v5_2(uuid)'::regprocedure,
         'memory.preflight_projection_entailment_source_v5_2(uuid)'::regprocedure,
         'memory.preflight_reusable_observation_entailment_v5_2(uuid,jsonb)'::regprocedure,
         'memory.preflight_projection_temporal_state_v5_2(uuid)'::regprocedure,
         'memory.render_projection_claim_text_temporal_v5_2(uuid)'::regprocedure
       )
         AND privilege.grantee=0
         AND privilege.privilege_type='EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.preflight_projection_source_v5_2(uuid)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.preflight_projection_entailment_source_v5_2(uuid)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.preflight_reusable_observation_entailment_v5_2(uuid,jsonb)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.preflight_projection_temporal_state_v5_2(uuid)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.render_projection_claim_text_temporal_v5_2(uuid)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'canonical-name claim-source ACL drifted';
  END IF;
END
$block$;

ROLLBACK;
SELECT 'memory_v1_v5_2_canonical_name_claim_source_security: PASS'
  AS result;
