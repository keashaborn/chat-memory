BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $install$
DECLARE
  definition text;
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'V5.2 atom stage planner V2 install requires sage';
  END IF;
  IF to_regrole('memory_v5_2_atom_admission_maintainer') IS NULL
     OR to_regprocedure('memory.plan_owner_v5_2_atom_stage_v1(uuid)') IS NULL THEN
    RAISE EXCEPTION 'V5.2 atom stage planner V1 prerequisite is absent';
  END IF;

  SELECT pg_get_functiondef(
    'memory.plan_owner_v5_2_atom_stage_v1(uuid)'::regprocedure
  ) INTO definition;
  definition:=replace(
    definition,
    'plan_owner_v5_2_atom_stage_v1',
    'plan_owner_v5_2_atom_stage_v2'
  );
  definition:=replace(
    definition,
    'memory_v1_v5_2_atom_admission_proposal_v1',
    'memory_v1_v5_2_atom_admission_proposal_v2'
  );
  definition:=replace(
    definition,
    'memory_v1_v5_2_atom_stage_plan_v1',
    'memory_v1_v5_2_atom_stage_plan_v2'
  );
  definition:=replace(
    definition,
    'memory_v1_v5_2_atom_stage_policy_v1',
    'memory_v1_v5_2_atom_stage_policy_v2'
  );
  IF definition NOT LIKE '%plan_owner_v5_2_atom_stage_v2%'
     OR definition NOT LIKE '%memory_v1_v5_2_atom_admission_proposal_v2%'
     OR definition NOT LIKE '%memory_v1_v5_2_atom_stage_plan_v2%'
     OR definition NOT LIKE '%memory_v1_v5_2_atom_stage_policy_v2%'
     OR definition LIKE '%memory_v1_v5_2_atom_admission_proposal_v1%' THEN
    RAISE EXCEPTION 'V5.2 atom stage planner V2 transformation is incomplete';
  END IF;
  EXECUTE definition;
END
$install$;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_2_entity_resolution_review_v1(
  p_evidence_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  items jsonb;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_evidence_id IS NULL THEN
    RAISE EXCEPTION 'evidence_id is required' USING ERRCODE='22004';
  END IF;
  SELECT jsonb_agg(
    jsonb_build_object(
      'resolution_id',plan.resolution_id::text,
      'mention_id',mention.mention_id::text,
      'entity_ref',mention.entity_ref,
      'entity_type',mention.entity_type,
      'name_text',mention.name_text,
      'mention_sha256',mention.mention_sha256,
      'action',plan.action::text,
      'decision_state',plan.decision_state::text,
      'selected_entity_id',plan.selected_entity_id::text,
      'proposed_entity',plan.proposed_entity,
      'candidate_set_sha256',plan.candidate_set_sha256,
      'decision_sha256',plan.decision_sha256,
      'review_reason_codes',plan.review_reason_codes,
      'existing_review_count',(
        SELECT count(*) FROM memory.entity_resolution_review AS review
        WHERE review.owner_user_id=actor
          AND review.resolution_id=plan.resolution_id
      ),
      'existing_apply_count',(
        SELECT count(*) FROM memory.entity_resolution_apply AS applied
        WHERE applied.owner_user_id=actor
          AND applied.resolution_id=plan.resolution_id
      )
    )
    ORDER BY mention.entity_ref
  ) INTO items
  FROM memory.entity_resolution_plan AS plan
  JOIN memory.entity_mention AS mention
    ON mention.owner_user_id=plan.owner_user_id
   AND mention.mention_id=plan.mention_id
  WHERE plan.owner_user_id=actor
    AND plan.evidence_id=p_evidence_id
    AND plan.predicate_registry_version='memory_predicate_registry_v5_2'
    AND plan.decision_state='manual_review_required';

  IF items IS NULL THEN
    RAISE EXCEPTION 'owner-scoped V5.2 entity review plan is unavailable'
      USING ERRCODE='P0002';
  END IF;
  RETURN jsonb_build_object(
    'contract_version','memory_v1_v5_2_entity_resolution_review_plan_v1',
    'policy_version','memory_v1_v5_2_entity_resolution_review_policy_v1',
    'owner_user_id',actor::text,
    'evidence_id',p_evidence_id::text,
    'items',items
  );
END
$function$;

ALTER FUNCTION memory.plan_owner_v5_2_atom_stage_v2(uuid)
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.plan_owner_v5_2_entity_resolution_review_v1(uuid)
  OWNER TO memory_v5_writer;

REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_atom_stage_v2(uuid)
  FROM PUBLIC,memory_v5_writer,memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_entity_resolution_review_v1(uuid)
  FROM PUBLIC,memory_v5_2_local_router_maintainer;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_2_atom_stage_v2(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_2_entity_resolution_review_v1(uuid)
  TO brains_app;

COMMENT ON FUNCTION memory.plan_owner_v5_2_atom_stage_v2(uuid) IS
  'Returns one exact current owner-scoped V5.2 admission-proposal V2 stage projection through a typed read-only boundary. It does not stage or apply rows.';
COMMENT ON FUNCTION memory.plan_owner_v5_2_entity_resolution_review_v1(uuid) IS
  'Returns current owner-scoped manual V5.2 entity-resolution rows for controlled review. It does not review or apply an entity.';

COMMIT;
