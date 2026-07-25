BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'V5.2 entity apply planner install requires sage';
  END IF;
  IF to_regrole('memory_v5_writer') IS NULL
     OR to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure(
       'memory.preflight_entity_resolution_apply_v5_2(uuid,uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.apply_entity_resolution_v5_2(uuid,uuid,uuid,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'V5.2 entity apply prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_2_entity_apply_review_v1(
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
      'mention_kind',mention.mention_kind::text,
      'name_text',mention.name_text,
      'mention_sha256',mention.mention_sha256,
      'action',plan.action::text,
      'decision_state',plan.decision_state::text,
      'selected_entity_id',plan.selected_entity_id::text,
      'proposed_entity',plan.proposed_entity,
      'candidate_set_sha256',plan.candidate_set_sha256,
      'decision_sha256',plan.decision_sha256,
      'latest_review_id',review.review_id::text,
      'latest_review_decision',review.decision::text,
      'existing_apply_count',(
        SELECT count(*) FROM memory.entity_resolution_apply AS applied
        WHERE applied.owner_user_id=actor
          AND applied.resolution_id=plan.resolution_id
      ),
      'observation_count',(
        SELECT count(*) FROM memory.observation AS observation
        WHERE observation.owner_user_id=actor
          AND (
            observation.subject_mention_id=mention.mention_id
            OR observation.object_mention_id=mention.mention_id
          )
      )
    )
    ORDER BY mention.entity_ref
  ) INTO items
  FROM memory.entity_resolution_plan AS plan
  JOIN memory.entity_mention AS mention
    ON mention.owner_user_id=plan.owner_user_id
   AND mention.mention_id=plan.mention_id
  LEFT JOIN LATERAL (
    SELECT candidate.*
    FROM memory.entity_resolution_review AS candidate
    WHERE candidate.owner_user_id=plan.owner_user_id
      AND candidate.resolution_id=plan.resolution_id
    ORDER BY candidate.created_at DESC,candidate.review_id DESC
    LIMIT 1
  ) AS review ON true
  WHERE plan.owner_user_id=actor
    AND plan.evidence_id=p_evidence_id
    AND plan.predicate_registry_version='memory_predicate_registry_v5_2';

  IF items IS NULL THEN
    RAISE EXCEPTION 'owner-scoped V5.2 entity apply plan is unavailable'
      USING ERRCODE='P0002';
  END IF;
  RETURN jsonb_build_object(
    'contract_version','memory_v1_v5_2_entity_apply_review_plan_v1',
    'policy_version','memory_v1_v5_2_entity_apply_review_policy_v1',
    'owner_user_id',actor::text,
    'evidence_id',p_evidence_id::text,
    'items',items
  );
END
$function$;

ALTER FUNCTION memory.plan_owner_v5_2_entity_apply_review_v1(uuid)
  OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_entity_apply_review_v1(uuid)
  FROM PUBLIC,memory_v5_2_local_router_maintainer;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_2_entity_apply_review_v1(uuid)
  TO brains_app;

COMMENT ON FUNCTION memory.plan_owner_v5_2_entity_apply_review_v1(uuid) IS
  'Returns current owner-scoped V5.2 resolution, review, and apply status for controlled entity application. It performs no writes.';

COMMIT;
