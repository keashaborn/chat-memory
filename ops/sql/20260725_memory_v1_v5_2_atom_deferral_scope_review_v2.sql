\set ON_ERROR_STOP on

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

DO $preflight$
BEGIN
  IF to_regrole('memory_v5_2_atom_admission_maintainer') IS NULL
     OR to_regclass('memory.v5_2_atom_admission_proposal') IS NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_2_atom_admission_v1(uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.record_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,text,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.preflight_owner_v5_2_atom_review_v1(uuid,memory.v5_2_atom_review_decision,text,text,jsonb)'
     ) IS NULL
     OR to_regprocedure(
       'memory.review_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.preflight_owner_v5_2_atom_apply_v1(uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.apply_owner_v5_2_atom_review_v1(uuid,uuid,uuid,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.2 atom-admission V1 foundation is absent';
  END IF;
END
$preflight$;

ALTER TABLE memory.v5_2_atom_admission_proposal
  DROP CONSTRAINT IF EXISTS
    v5_2_atom_admission_proposal_policy_version_check;
ALTER TABLE memory.v5_2_atom_admission_proposal
  ADD CONSTRAINT v5_2_atom_admission_proposal_policy_version_check
  CHECK (policy_version IN (
    'memory_v1_v5_2_atom_admission_policy_v1',
    'memory_v1_v5_2_atom_admission_policy_v2'
  ));

DO $planner$
DECLARE
  definition text;
  old_observations text := $old$
  observations AS (
    SELECT item.value AS atom,item.ordinality::integer AS ordinal,
      NOT EXISTS (
        SELECT 1 FROM deferral_spans AS blocked
        WHERE memory.v5_2_source_spans_overlap_v1(
          item.value->'source_spans',jsonb_build_array(blocked.value)
        )
      ) AS span_safe
    FROM jsonb_array_elements(packet.normalized_packet->'observations')
      WITH ORDINALITY AS item(value,ordinality)
  ),$old$;
  new_observations text := $new$
  observations AS (
    SELECT item.value AS atom,item.ordinality::integer AS ordinal,
      NOT EXISTS (
        SELECT 1 FROM deferral_spans AS blocked
        WHERE memory.v5_2_source_spans_overlap_v1(
          item.value->'source_spans',jsonb_build_array(blocked.value)
        )
        AND NOT (
          (
            blocked.deferral->>'reason_code'
              IN ('structured_domain','question_only')
            AND blocked.deferral->>'memory_shape'='none'
            AND (blocked.deferral->>'review_required')::boolean=false
          )
          OR (
            blocked.deferral->>'reason_code'='compound_requires_split'
            AND blocked.deferral->>'memory_shape'
                  <>item.value->>'projection_class'
          )
          OR (
            blocked.deferral->>'reason_code'='sensitive_manual_review'
            AND blocked.deferral->>'memory_shape'
                  =item.value->>'projection_class'
            AND (blocked.deferral->>'review_required')::boolean=true
          )
        )
      ) AS span_safe,
      EXISTS (
        SELECT 1 FROM deferral_spans AS blocked
        WHERE blocked.deferral->>'reason_code'='sensitive_manual_review'
          AND blocked.deferral->>'memory_shape'
                =item.value->>'projection_class'
          AND (blocked.deferral->>'review_required')::boolean=true
          AND memory.v5_2_source_spans_overlap_v1(
            item.value->'source_spans',jsonb_build_array(blocked.value)
          )
      ) AS sensitive_review,
      EXISTS (
        SELECT 1 FROM deferral_spans AS blocked
        WHERE (
          (
            blocked.deferral->>'reason_code'
              IN ('structured_domain','question_only')
            AND blocked.deferral->>'memory_shape'='none'
            AND (blocked.deferral->>'review_required')::boolean=false
          )
          OR (
            blocked.deferral->>'reason_code'='compound_requires_split'
            AND blocked.deferral->>'memory_shape'
                  <>item.value->>'projection_class'
          )
        )
        AND memory.v5_2_source_spans_overlap_v1(
          item.value->'source_spans',jsonb_build_array(blocked.value)
        )
      ) AS scoped_deferral
    FROM jsonb_array_elements(packet.normalized_packet->'observations')
      WITH ORDINALITY AS item(value,ordinality)
  ),$new$;
  old_safe_mentions text := $old$
  safe_mentions AS (
    SELECT mention.entity_ref
    FROM mentions AS mention
    JOIN candidate_refs AS needed USING(entity_ref)
    WHERE mention.span_safe OR mention.trusted_self
  ),$old$;
  new_safe_mentions text := $new$
  safe_mentions AS (
    SELECT mention.entity_ref
    FROM mentions AS mention
    JOIN candidate_refs AS needed USING(entity_ref)
  ),$new$;
  old_observation_reasons text := $old$
      CASE
        WHEN admitted
          THEN '["deferral_span_disjoint","entity_dependencies_admissible"]'::jsonb
        WHEN NOT span_safe
          THEN '["overlaps_source_deferral"]'::jsonb
        ELSE '["entity_dependency_deferred"]'::jsonb
      END AS decision_reasons$old$;
  new_observation_reasons text := $new$
      CASE
        WHEN admitted AND sensitive_review AND scoped_deferral
          THEN '["entity_dependencies_admissible","sensitive_manual_review_required","source_deferral_scope_disjoint"]'::jsonb
        WHEN admitted AND sensitive_review
          THEN '["entity_dependencies_admissible","sensitive_manual_review_required"]'::jsonb
        WHEN admitted AND scoped_deferral
          THEN '["entity_dependencies_admissible","source_deferral_scope_disjoint"]'::jsonb
        WHEN admitted
          THEN '["deferral_span_disjoint","entity_dependencies_admissible"]'::jsonb
        WHEN NOT span_safe
          THEN '["overlaps_hard_source_deferral"]'::jsonb
        ELSE '["entity_dependency_deferred"]'::jsonb
      END AS decision_reasons$new$;
BEGIN
  SELECT pg_get_functiondef(
    'memory.plan_owner_v5_2_atom_admission_v1(uuid)'::regprocedure
  ) INTO definition;
  IF position(old_observations IN definition)=0
     OR position(old_safe_mentions IN definition)=0
     OR position(old_observation_reasons IN definition)=0
     OR position('SELECT span.value' IN definition)=0 THEN
    RAISE EXCEPTION 'V1 atom planner source differs from the V2 patch contract';
  END IF;
  definition := replace(
    definition,
    'memory.plan_owner_v5_2_atom_admission_v1',
    'memory.plan_owner_v5_2_atom_admission_v2'
  );
  definition := replace(
    definition,
    '''memory_v1_v5_2_atom_admission_policy_v1''',
    '''memory_v1_v5_2_atom_admission_policy_v2'''
  );
  definition := replace(
    definition,
    '''memory_v1_v5_2_atom_admission_proposal_v1''',
    '''memory_v1_v5_2_atom_admission_proposal_v2'''
  );
  definition := replace(
    definition,
    'SELECT span.value',
    'SELECT span.value,deferral.value AS deferral'
  );
  definition := replace(definition,old_observations,new_observations);
  definition := replace(definition,old_safe_mentions,new_safe_mentions);
  definition := replace(
    definition,
    '''["required_by_admitted_observation","deferral_span_disjoint"]''::jsonb',
    '''["required_by_admitted_observation","required_by_reviewable_observation"]''::jsonb'
  );
  definition := replace(
    definition,old_observation_reasons,new_observation_reasons
  );
  EXECUTE definition;
END
$planner$;

DO $lifecycle$
DECLARE
  definition text;
BEGIN
  SELECT pg_get_functiondef(
    'memory.record_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,text,text)'::regprocedure
  ) INTO definition;
  definition := replace(
    definition,
    'memory.record_owner_v5_2_atom_proposal_v1',
    'memory.record_owner_v5_2_atom_proposal_v2'
  );
  definition := replace(
    definition,
    'memory.plan_owner_v5_2_atom_admission_v1',
    'memory.plan_owner_v5_2_atom_admission_v2'
  );
  definition := replace(
    definition,
    '''memory_v1_v5_2_atom_admission_policy_v1''',
    '''memory_v1_v5_2_atom_admission_policy_v2'''
  );
  definition := replace(
    definition,
    '''memory_v1_v5_2_atom_proposal_record_v1''',
    '''memory_v1_v5_2_atom_proposal_record_v2'''
  );
  EXECUTE definition;

  SELECT pg_get_functiondef(
    'memory.preflight_owner_v5_2_atom_review_v1(uuid,memory.v5_2_atom_review_decision,text,text,jsonb)'::regprocedure
  ) INTO definition;
  definition := replace(
    definition,
    'memory.preflight_owner_v5_2_atom_review_v1',
    'memory.preflight_owner_v5_2_atom_review_v2'
  );
  definition := replace(
    definition,
    'memory.plan_owner_v5_2_atom_admission_v1',
    'memory.plan_owner_v5_2_atom_admission_v2'
  );
  definition := replace(
    definition,
    '''memory_v1_v5_2_atom_admission_policy_v1''',
    '''memory_v1_v5_2_atom_admission_policy_v2'''
  );
  definition := replace(
    definition,
    '''memory_v1_v5_2_atom_review_authorization_v1''',
    '''memory_v1_v5_2_atom_review_authorization_v2'''
  );
  EXECUTE definition;

  SELECT pg_get_functiondef(
    'memory.review_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text)'::regprocedure
  ) INTO definition;
  definition := replace(
    definition,
    'memory.review_owner_v5_2_atom_proposal_v1',
    'memory.review_owner_v5_2_atom_proposal_v2'
  );
  definition := replace(
    definition,
    'memory.preflight_owner_v5_2_atom_review_v1',
    'memory.preflight_owner_v5_2_atom_review_v2'
  );
  definition := replace(
    definition,
    '''memory_v1_v5_2_atom_review_record_v1''',
    '''memory_v1_v5_2_atom_review_record_v2'''
  );
  EXECUTE definition;

  SELECT pg_get_functiondef(
    'memory.preflight_owner_v5_2_atom_apply_v1(uuid)'::regprocedure
  ) INTO definition;
  definition := replace(
    definition,
    'memory.preflight_owner_v5_2_atom_apply_v1',
    'memory.preflight_owner_v5_2_atom_apply_v2'
  );
  definition := replace(
    definition,
    'memory.plan_owner_v5_2_atom_admission_v1',
    'memory.plan_owner_v5_2_atom_admission_v2'
  );
  definition := replace(
    definition,
    '''memory_v1_v5_2_atom_apply_v1''',
    '''memory_v1_v5_2_atom_apply_v2'''
  );
  EXECUTE definition;

  SELECT pg_get_functiondef(
    'memory.apply_owner_v5_2_atom_review_v1(uuid,uuid,uuid,text)'::regprocedure
  ) INTO definition;
  definition := replace(
    definition,
    'memory.apply_owner_v5_2_atom_review_v1',
    'memory.apply_owner_v5_2_atom_review_v2'
  );
  definition := replace(
    definition,
    'memory.preflight_owner_v5_2_atom_apply_v1',
    'memory.preflight_owner_v5_2_atom_apply_v2'
  );
  definition := replace(
    definition,
    '''memory_v1_v5_2_atom_apply_record_v1''',
    '''memory_v1_v5_2_atom_apply_record_v2'''
  );
  EXECUTE definition;
END
$lifecycle$;

ALTER FUNCTION memory.plan_owner_v5_2_atom_admission_v2(uuid)
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.record_owner_v5_2_atom_proposal_v2(
  uuid,uuid,uuid,text,text
) OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.preflight_owner_v5_2_atom_review_v2(
  uuid,memory.v5_2_atom_review_decision,text,text,jsonb
) OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.review_owner_v5_2_atom_proposal_v2(
  uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text
) OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.preflight_owner_v5_2_atom_apply_v2(uuid)
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.apply_owner_v5_2_atom_review_v2(
  uuid,uuid,uuid,text
) OWNER TO memory_v5_2_atom_admission_maintainer;

REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_2_atom_admission_v2(uuid),
  memory.record_owner_v5_2_atom_proposal_v2(uuid,uuid,uuid,text,text),
  memory.preflight_owner_v5_2_atom_review_v2(
    uuid,memory.v5_2_atom_review_decision,text,text,jsonb
  ),
  memory.review_owner_v5_2_atom_proposal_v2(
    uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text
  ),
  memory.preflight_owner_v5_2_atom_apply_v2(uuid),
  memory.apply_owner_v5_2_atom_review_v2(uuid,uuid,uuid,text)
  FROM PUBLIC,brains_app;

GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_2_atom_admission_v2(uuid),
  memory.record_owner_v5_2_atom_proposal_v2(uuid,uuid,uuid,text,text),
  memory.preflight_owner_v5_2_atom_review_v2(
    uuid,memory.v5_2_atom_review_decision,text,text,jsonb
  ),
  memory.review_owner_v5_2_atom_proposal_v2(
    uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text
  ),
  memory.preflight_owner_v5_2_atom_apply_v2(uuid),
  memory.apply_owner_v5_2_atom_review_v2(uuid,uuid,uuid,text)
  TO brains_app;

COMMENT ON FUNCTION memory.plan_owner_v5_2_atom_admission_v2(uuid) IS
  'Builds an owner-scoped atom plan that distinguishes source-only structured material, split supportive context, sensitive reviewed claims, and hard deferrals. Every admitted projection still requires append-only owner review and apply before staging.';

COMMIT;
