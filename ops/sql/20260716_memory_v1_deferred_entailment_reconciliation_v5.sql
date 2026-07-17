BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'deferred-entailment reconciliation migration requires sage';
  END IF;
  IF to_regrole('memory_v5_writer') IS NULL
     OR to_regclass('memory.observation_entailment_v5') IS NULL
     OR to_regclass('memory.claim_observation') IS NULL
     OR to_regclass('memory.claim_assessment_review_v5') IS NULL
     OR to_regclass('memory.claim_assessment_apply_v5') IS NULL
     OR to_regprocedure(
       'memory.preflight_claim_assessment_review_v5(uuid,memory.claim_assessment_action_v5,numeric,numeric,numeric,numeric,jsonb,text,text,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.review_claim_assessment_v5(uuid,uuid,memory.claim_assessment_action_v5,numeric,numeric,numeric,numeric,jsonb,text,text,text,text)'
     ) IS NULL
     OR to_regprocedure(
       'memory.preflight_claim_assessment_apply_v5(uuid,uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.apply_claim_assessment_v5(uuid,uuid,uuid,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'deferred-entailment reconciliation prerequisites are absent';
  END IF;
  IF (SELECT rolbypassrls FROM pg_roles WHERE rolname='memory_v5_writer') THEN
    RAISE EXCEPTION 'memory_v5_writer must not bypass RLS';
  END IF;
END
$preflight$;

DO $decision_owner_key$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='memory.observation_entailment_v5'::regclass
      AND conname='observation_entailment_v5_owner_decision_key'
  ) THEN
    ALTER TABLE memory.observation_entailment_v5
      ADD CONSTRAINT observation_entailment_v5_owner_decision_key
      UNIQUE (owner_user_id,decision_id);
  END IF;
END
$decision_owner_key$;

CREATE TABLE IF NOT EXISTS memory.claim_entailment_reconciliation_v5 (
  reconciliation_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  observation_stance memory.evidence_stance NOT NULL,
  decision_id uuid NOT NULL,
  decision_authorization_manifest_sha256 text NOT NULL,
  support_state jsonb NOT NULL,
  support_state_sha256 text NOT NULL,
  review_request_id uuid NOT NULL,
  review_id uuid NOT NULL,
  review_authorization_manifest_sha256 text NOT NULL,
  apply_request_id uuid NOT NULL,
  apply_event_id uuid NOT NULL,
  assessment_id uuid NOT NULL,
  apply_manifest_sha256 text NOT NULL,
  from_status memory.claim_status NOT NULL,
  to_status memory.claim_status NOT NULL,
  prior_revision_number integer NOT NULL,
  resulting_revision_number integer NOT NULL,
  reconciliation_policy_version text NOT NULL,
  reconciliation_manifest_sha256 text NOT NULL,
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id,reconciliation_id),
  UNIQUE (owner_user_id,claim_id,decision_id),
  UNIQUE (owner_user_id,review_request_id),
  UNIQUE (owner_user_id,apply_request_id),
  FOREIGN KEY (owner_user_id,claim_id)
    REFERENCES memory.claim(owner_user_id,claim_id) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,claim_id,observation_id,observation_stance)
    REFERENCES memory.claim_observation(
      owner_user_id,claim_id,observation_id,stance
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,decision_id)
    REFERENCES memory.observation_entailment_v5(
      owner_user_id,decision_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,claim_id,review_id)
    REFERENCES memory.claim_assessment_review_v5(
      owner_user_id,claim_id,review_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,claim_id,review_id)
    REFERENCES memory.claim_assessment_apply_v5(
      owner_user_id,claim_id,review_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (apply_event_id)
    REFERENCES memory.claim_assessment_apply_v5(event_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,assessment_id)
    REFERENCES memory.claim_assessment(owner_user_id,assessment_id)
    ON DELETE RESTRICT,
  CHECK (observation_stance='supports'),
  CHECK (memory.v5_sha256_valid(
    decision_authorization_manifest_sha256
  )),
  CHECK (jsonb_typeof(support_state)='array'
    AND jsonb_array_length(support_state)>0),
  CHECK (memory.v5_sha256_valid(support_state_sha256)),
  CHECK (
    memory.v5_digest_text(memory.v5_canonical_json_text(support_state))
      =support_state_sha256
  ),
  CHECK (memory.v5_sha256_valid(
    review_authorization_manifest_sha256
  )),
  CHECK (memory.v5_sha256_valid(apply_manifest_sha256)),
  CHECK (from_status IN ('candidate','supported','uncertain','disputed')),
  CHECK (to_status='retracted'),
  CHECK (prior_revision_number>0
    AND resulting_revision_number=prior_revision_number+1),
  CHECK (
    reconciliation_policy_version
      ='memory_v1_deferred_entailment_reconciliation_v5'
  ),
  CHECK (memory.v5_sha256_valid(reconciliation_manifest_sha256))
);

CREATE INDEX IF NOT EXISTS claim_entailment_reconciliation_owner_claim_idx
  ON memory.claim_entailment_reconciliation_v5(
    owner_user_id,claim_id,created_at DESC
  );
CREATE INDEX IF NOT EXISTS claim_entailment_reconciliation_owner_decision_idx
  ON memory.claim_entailment_reconciliation_v5(
    owner_user_id,decision_id
  );
CREATE INDEX IF NOT EXISTS claim_entailment_reconciliation_event_idx
  ON memory.claim_entailment_reconciliation_v5(apply_event_id);
CREATE INDEX IF NOT EXISTS claim_entailment_reconciliation_assessment_idx
  ON memory.claim_entailment_reconciliation_v5(
    owner_user_id,assessment_id
  );

CREATE OR REPLACE FUNCTION
memory.preflight_deferred_entailment_reconciliation_v5(
  p_claim_id uuid,
  p_decision_id uuid
)
RETURNS TABLE(
  claim_id uuid,
  observation_id uuid,
  decision_id uuid,
  from_status text,
  target_status text,
  current_revision_number integer,
  claim_state_sha256 text,
  evidence_manifest_sha256 text,
  decision_authorization_manifest_sha256 text,
  support_state jsonb,
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
  selected_decision memory.observation_entailment_v5%ROWTYPE;
  state jsonb;
  support_value jsonb;
  support_hash text;
  review_preflight record;
  reason_codes constant jsonb :=
    '["all_supporting_observations_deferred","deferred_entailment","source_contradicts_predicate"]'::jsonb;
  rationale constant text :=
    'All active supporting observations are governed as deferred because their source contradicts the projected predicate. Retract the claim.';
  manifest text;
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_claim_id IS NULL OR p_decision_id IS NULL THEN
    RAISE EXCEPTION 'reconciliation identifiers are required'
      USING ERRCODE='22023';
  END IF;

  SELECT decision.* INTO selected_decision
  FROM memory.observation_entailment_v5 AS decision
  JOIN memory.observation AS observation
    ON observation.owner_user_id=decision.owner_user_id
   AND observation.observation_id=decision.observation_id
   AND observation.observation_sha256=decision.observation_sha256
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id=decision.owner_user_id
   AND evidence.evidence_id=decision.evidence_id
   AND evidence.content_sha256=decision.evidence_content_sha256
   AND evidence.status='active'
  JOIN memory.claim_observation AS link
    ON link.owner_user_id=decision.owner_user_id
   AND link.observation_id=decision.observation_id
   AND link.claim_id=p_claim_id
   AND link.stance='supports'
  WHERE decision.owner_user_id=actor
    AND decision.decision_id=p_decision_id
    AND decision.policy_version='memory_v1_predicate_entailment_v5_1'
    AND decision.decision='deferred'
    AND decision.reason_code='source_contradicts_predicate';
  IF NOT FOUND THEN
    RAISE EXCEPTION
      'owner-scoped contradictory deferred support decision not found'
      USING ERRCODE='P0002';
  END IF;

  state := memory.claim_assessment_state_v5(p_claim_id);

  SELECT COALESCE(jsonb_agg(
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
  ),'[]'::jsonb)
  INTO support_value
  FROM memory.claim_observation AS link
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
  WHERE link.owner_user_id=actor
    AND link.claim_id=p_claim_id
    AND link.stance='supports';

  IF jsonb_array_length(support_value)<1
     OR EXISTS (
       SELECT 1
       FROM jsonb_array_elements(support_value) AS rows(item)
       WHERE rows.item->>'decision' IS DISTINCT FROM 'deferred'
          OR rows.item->>'reason_code'
               IS DISTINCT FROM 'source_contradicts_predicate'
          OR rows.item->>'decision_id' IS NULL
     )
     OR NOT EXISTS (
       SELECT 1
       FROM jsonb_array_elements(support_value) AS rows(item)
       WHERE rows.item->>'decision_id'=p_decision_id::text
     ) THEN
    RAISE EXCEPTION
      'claim has accepted, unassessed, or non-contradictory active support'
      USING ERRCODE='23514';
  END IF;

  support_hash := memory.v5_digest_text(
    memory.v5_canonical_json_text(support_value)
  );
  SELECT * INTO review_preflight
  FROM memory.preflight_claim_assessment_review_v5(
    p_claim_id,
    'retract'::memory.claim_assessment_action_v5,
    0,1,0,1,reason_codes,rationale,
    'system','memory_v1_deferred_entailment_reconciliation_v5'
  );
  manifest := memory.v5_digest_text(concat_ws('|',
    'memory_v1_deferred_entailment_reconciliation_v5',
    actor::text,p_claim_id::text,
    selected_decision.observation_id::text,p_decision_id::text,
    selected_decision.authorization_manifest_sha256,
    review_preflight.from_status,review_preflight.target_status,
    review_preflight.current_revision_number::text,
    review_preflight.claim_state_sha256,
    review_preflight.evidence_manifest_sha256,
    support_hash,
    review_preflight.authorization_manifest_sha256
  ));
  RETURN QUERY SELECT
    p_claim_id,selected_decision.observation_id,p_decision_id,
    review_preflight.from_status,review_preflight.target_status,
    review_preflight.current_revision_number,
    review_preflight.claim_state_sha256,
    review_preflight.evidence_manifest_sha256,
    selected_decision.authorization_manifest_sha256,
    support_value,support_hash,
    review_preflight.authorization_manifest_sha256,
    manifest;
END
$function$;

CREATE OR REPLACE FUNCTION memory.reconcile_deferred_entailment_claim_v5(
  p_reconciliation_id uuid,
  p_review_request_id uuid,
  p_apply_request_id uuid,
  p_claim_id uuid,
  p_decision_id uuid,
  p_reconciliation_manifest_sha256 text
)
RETURNS TABLE(
  reconciliation_id uuid,
  outcome text,
  review_id uuid,
  apply_event_id uuid,
  assessment_id uuid,
  resulting_revision_number integer,
  rows_written integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  existing memory.claim_entailment_reconciliation_v5%ROWTYPE;
  preflight record;
  review_result record;
  apply_preflight record;
  apply_result record;
  reason_codes constant jsonb :=
    '["all_supporting_observations_deferred","deferred_entailment","source_contradicts_predicate"]'::jsonb;
  rationale constant text :=
    'All active supporting observations are governed as deferred because their source contradicts the projected predicate. Retract the claim.';
BEGIN
  actor := memory.require_v5_writer_context();
  IF p_reconciliation_id IS NULL
     OR p_review_request_id IS NULL
     OR p_apply_request_id IS NULL
     OR p_claim_id IS NULL
     OR p_decision_id IS NULL
     OR p_review_request_id=p_apply_request_id
     OR NOT memory.v5_sha256_valid(
       p_reconciliation_manifest_sha256
     ) THEN
    RAISE EXCEPTION 'reconciliation inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|deferred_entailment_reconciliation|'
      ||p_claim_id::text,
    0
  ));

  SELECT stored.* INTO existing
  FROM memory.claim_entailment_reconciliation_v5 AS stored
  WHERE stored.owner_user_id=actor
    AND (
      stored.reconciliation_id=p_reconciliation_id
      OR (
        stored.claim_id=p_claim_id
        AND stored.decision_id=p_decision_id
      )
    );
  IF FOUND THEN
    IF existing.reconciliation_id<>p_reconciliation_id
       OR existing.review_request_id<>p_review_request_id
       OR existing.apply_request_id<>p_apply_request_id
       OR existing.claim_id<>p_claim_id
       OR existing.decision_id<>p_decision_id
       OR existing.reconciliation_manifest_sha256
            <>p_reconciliation_manifest_sha256 THEN
      RAISE EXCEPTION 'reconciliation replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      existing.reconciliation_id,'replayed',existing.review_id,
      existing.apply_event_id,existing.assessment_id,
      existing.resulting_revision_number,0;
    RETURN;
  END IF;

  SELECT * INTO preflight
  FROM memory.preflight_deferred_entailment_reconciliation_v5(
    p_claim_id,p_decision_id
  );
  IF preflight.reconciliation_manifest_sha256
       <>p_reconciliation_manifest_sha256 THEN
    RAISE EXCEPTION 'reconciliation manifest mismatch'
      USING ERRCODE='23514';
  END IF;

  SELECT * INTO review_result
  FROM memory.review_claim_assessment_v5(
    p_review_request_id,p_claim_id,
    'retract'::memory.claim_assessment_action_v5,
    0,1,0,1,reason_codes,rationale,
    'system','memory_v1_deferred_entailment_reconciliation_v5',
    preflight.review_authorization_manifest_sha256
  );
  IF review_result.outcome<>'applied' THEN
    RAISE EXCEPTION 'reconciliation review was not newly applied'
      USING ERRCODE='23514';
  END IF;

  SELECT * INTO apply_preflight
  FROM memory.preflight_claim_assessment_apply_v5(
    p_claim_id,review_result.review_id
  );
  SELECT * INTO apply_result
  FROM memory.apply_claim_assessment_v5(
    p_apply_request_id,p_claim_id,review_result.review_id,
    apply_preflight.apply_manifest_sha256
  );
  IF apply_result.outcome<>'applied'
     OR apply_result.rows_written<>5 THEN
    RAISE EXCEPTION 'reconciliation assessment was not newly applied'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.claim_entailment_reconciliation_v5(
    reconciliation_id,owner_user_id,claim_id,
    observation_id,observation_stance,decision_id,
    decision_authorization_manifest_sha256,
    support_state,support_state_sha256,
    review_request_id,review_id,
    review_authorization_manifest_sha256,
    apply_request_id,apply_event_id,assessment_id,
    apply_manifest_sha256,
    from_status,to_status,prior_revision_number,
    resulting_revision_number,reconciliation_policy_version,
    reconciliation_manifest_sha256,invoked_by_session
  ) VALUES (
    p_reconciliation_id,actor,p_claim_id,
    preflight.observation_id,'supports',p_decision_id,
    preflight.decision_authorization_manifest_sha256,
    preflight.support_state,preflight.support_state_sha256,
    p_review_request_id,review_result.review_id,
    preflight.review_authorization_manifest_sha256,
    p_apply_request_id,apply_result.event_id,apply_result.assessment_id,
    apply_preflight.apply_manifest_sha256,
    preflight.from_status::memory.claim_status,
    preflight.target_status::memory.claim_status,
    preflight.current_revision_number,
    apply_result.resulting_revision_number,
    'memory_v1_deferred_entailment_reconciliation_v5',
    p_reconciliation_manifest_sha256,session_user
  );
  RETURN QUERY SELECT
    p_reconciliation_id,'applied',review_result.review_id,
    apply_result.event_id,apply_result.assessment_id,
    apply_result.resulting_revision_number,8;
END
$function$;

ALTER TABLE memory.claim_entailment_reconciliation_v5
  OWNER TO memory_v5_writer;
ALTER FUNCTION
memory.preflight_deferred_entailment_reconciliation_v5(uuid,uuid)
  OWNER TO memory_v5_writer;
ALTER FUNCTION memory.reconcile_deferred_entailment_claim_v5(
  uuid,uuid,uuid,uuid,uuid,text
) OWNER TO memory_v5_writer;

ALTER TABLE memory.claim_entailment_reconciliation_v5
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.claim_entailment_reconciliation_v5
  FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.claim_entailment_reconciliation_v5;
CREATE POLICY owner_isolation
  ON memory.claim_entailment_reconciliation_v5
  TO memory_v5_writer
  USING (
    owner_user_id=(SELECT memory.current_actor_user_id())
  )
  WITH CHECK (
    owner_user_id=(SELECT memory.current_actor_user_id())
  );

DROP TRIGGER IF EXISTS claim_entailment_reconciliation_v5_append_only_guard
  ON memory.claim_entailment_reconciliation_v5;
CREATE TRIGGER claim_entailment_reconciliation_v5_append_only_guard
BEFORE UPDATE OR DELETE ON memory.claim_entailment_reconciliation_v5
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only();

GRANT SELECT,INSERT ON memory.claim_entailment_reconciliation_v5
  TO memory_v5_writer;
GRANT SELECT ON
  memory.observation_entailment_v5,
  memory.claim_observation,
  memory.observation,
  memory.evidence
TO memory_v5_writer;

REVOKE ALL ON memory.claim_entailment_reconciliation_v5
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION
memory.preflight_deferred_entailment_reconciliation_v5(uuid,uuid)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.reconcile_deferred_entailment_claim_v5(
  uuid,uuid,uuid,uuid,uuid,text
) FROM PUBLIC,brains_app;

GRANT EXECUTE ON FUNCTION
memory.preflight_deferred_entailment_reconciliation_v5(uuid,uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.reconcile_deferred_entailment_claim_v5(
  uuid,uuid,uuid,uuid,uuid,text
) TO brains_app;

COMMIT;
