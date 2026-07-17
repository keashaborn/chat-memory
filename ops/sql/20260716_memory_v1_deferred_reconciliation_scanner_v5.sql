BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'deferred reconciliation scanner migration requires sage';
  END IF;
  IF to_regrole('memory_v5_writer') IS NULL
     OR to_regclass('memory.claim_entailment_reconciliation_v5') IS NULL
     OR to_regprocedure(
       'memory.preflight_deferred_entailment_reconciliation_v5(uuid,uuid)'
     ) IS NULL THEN
    RAISE EXCEPTION 'deferred reconciliation scanner prerequisites are absent';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION
memory.v5_deferred_support_state_eligible(
  support_state jsonb
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path=''
AS $function$
  SELECT
    jsonb_typeof(support_state)='array'
    AND jsonb_array_length(support_state)>0
    AND NOT EXISTS (
      SELECT 1
      FROM jsonb_array_elements(support_state) AS rows(item)
      WHERE rows.item->>'observation_id' IS NULL
         OR rows.item->>'observation_sha256' IS NULL
         OR rows.item->>'evidence_id' IS NULL
         OR rows.item->>'evidence_content_sha256' IS NULL
         OR rows.item->>'stance' IS DISTINCT FROM 'supports'
         OR rows.item->>'decision_id' IS NULL
         OR rows.item->>'decision' IS DISTINCT FROM 'deferred'
         OR rows.item->>'reason_code'
              IS DISTINCT FROM 'source_contradicts_predicate'
         OR rows.item->>'decision_authorization_manifest_sha256' IS NULL
    );
$function$;

CREATE OR REPLACE FUNCTION
memory.scan_deferred_entailment_reconciliation_v5(
  p_limit integer DEFAULT 25
)
RETURNS TABLE(
  claim_id uuid,
  observation_id uuid,
  decision_id uuid,
  from_status text,
  target_status text,
  current_revision_number integer,
  support_count integer,
  support_state_sha256 text,
  review_authorization_manifest_sha256 text,
  reconciliation_manifest_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_limit IS NULL OR p_limit<1 OR p_limit>100 THEN
    RAISE EXCEPTION 'scanner limit must be between 1 and 100'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  WITH support_by_claim AS (
    SELECT
      claim.claim_id,
      jsonb_agg(
        jsonb_build_object(
          'observation_id',observation.observation_id::text,
          'observation_sha256',observation.observation_sha256,
          'evidence_id',evidence.evidence_id::text,
          'evidence_content_sha256',evidence.content_sha256,
          'stance',link.stance::text,
          'relevance',link.relevance::text,
          'decision_id',decision.decision_id::text,
          'decision',decision.decision::text,
          'reason_code',decision.reason_code,
          'decision_authorization_manifest_sha256',
            decision.authorization_manifest_sha256
        )
        ORDER BY observation.observation_id
      ) AS support_state
    FROM memory.claim AS claim
    JOIN memory.claim_observation AS link
      ON link.owner_user_id=claim.owner_user_id
     AND link.claim_id=claim.claim_id
     AND link.stance='supports'
    JOIN memory.observation AS observation
      ON observation.owner_user_id=link.owner_user_id
     AND observation.observation_id=link.observation_id
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=observation.owner_user_id
     AND evidence.evidence_id=observation.evidence_id
     AND evidence.status='active'
    LEFT JOIN memory.observation_entailment_v5 AS decision
      ON decision.owner_user_id=observation.owner_user_id
     AND decision.observation_id=observation.observation_id
     AND decision.observation_sha256=observation.observation_sha256
     AND decision.evidence_id=evidence.evidence_id
     AND decision.evidence_content_sha256=evidence.content_sha256
     AND decision.policy_version='memory_v1_predicate_entailment_v5_1'
    WHERE claim.owner_user_id=actor
      AND claim.status IN ('candidate','supported','uncertain','disputed')
      AND claim.canonical_key LIKE 'v5:%'
      AND claim.metadata->>'memory_contract'='memory_projection_v5'
    GROUP BY claim.claim_id
  ),
  eligible AS (
    SELECT
      grouped.claim_id,
      grouped.support_state,
      (grouped.support_state->0->>'decision_id')::uuid
        AS selected_decision_id
    FROM support_by_claim AS grouped
    WHERE memory.v5_deferred_support_state_eligible(
      grouped.support_state
    )
  )
  SELECT
    preflight.claim_id,
    preflight.observation_id,
    preflight.decision_id,
    preflight.from_status,
    preflight.target_status,
    preflight.current_revision_number,
    jsonb_array_length(eligible.support_state)::integer,
    preflight.support_state_sha256,
    preflight.review_authorization_manifest_sha256,
    preflight.reconciliation_manifest_sha256
  FROM eligible
  CROSS JOIN LATERAL
    memory.preflight_deferred_entailment_reconciliation_v5(
      eligible.claim_id,eligible.selected_decision_id
    ) AS preflight
  WHERE NOT EXISTS (
    SELECT 1
    FROM memory.claim_entailment_reconciliation_v5 AS reconciled
    WHERE reconciled.owner_user_id=actor
      AND reconciled.claim_id=eligible.claim_id
      AND reconciled.decision_id=eligible.selected_decision_id
  )
  ORDER BY preflight.claim_id
  LIMIT p_limit;
END
$function$;

ALTER FUNCTION memory.v5_deferred_support_state_eligible(jsonb)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.scan_deferred_entailment_reconciliation_v5(integer)
  OWNER TO memory_v5_writer;

REVOKE ALL ON FUNCTION
  memory.v5_deferred_support_state_eligible(jsonb)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION
  memory.scan_deferred_entailment_reconciliation_v5(integer)
  FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION
  memory.scan_deferred_entailment_reconciliation_v5(integer)
  TO brains_app;

COMMIT;
