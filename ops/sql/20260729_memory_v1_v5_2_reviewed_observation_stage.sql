BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'reviewed-observation stage migration requires sage';
  END IF;
  IF to_regclass('memory.v5_local_packet_stage_admission') IS NULL
     OR to_regclass('memory.v5_2_local_packet_route_event') IS NULL
     OR to_regclass('memory.v5_2_atom_admission_apply') IS NULL
     OR to_regclass('memory.v5_2_atom_admission_proposal') IS NULL
     OR to_regclass('memory.v5_2_atom_admission_review') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regclass('memory.relational_operation_request') IS NULL
     OR to_regclass('memory.observation_entity_binding') IS NULL
     OR to_regprocedure('memory.plan_owner_v5_local_entailment_v1(integer)') IS NULL
     OR to_regprocedure(
       'memory.register_owner_v5_local_entailment_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,memory.observation_entailment_decision_v5,text,jsonb,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'reviewed-observation stage prerequisites are absent';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_v5_2_reviewed_observation_stage_maintainer') IS NULL THEN
    CREATE ROLE memory_v5_2_reviewed_observation_stage_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOBYPASSRLS;
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname='memory_v5_2_reviewed_observation_stage_maintainer'
      AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
           OR rolinherit OR rolbypassrls)
  ) THEN
    RAISE EXCEPTION 'reviewed-observation stage role is overprivileged';
  END IF;
END
$role$;

ALTER TABLE memory.v5_2_local_packet_route_event
  ADD CONSTRAINT v5_2_local_packet_route_event_owner_route_key
  UNIQUE(owner_user_id,route_event_id);

ALTER TABLE memory.v5_local_packet_stage_admission
  ALTER COLUMN artifact_id DROP NOT NULL,
  ADD COLUMN source_route_event_id uuid,
  ADD COLUMN source_atom_apply_id uuid;

ALTER TABLE memory.v5_local_packet_stage_admission
  ADD CONSTRAINT v5_local_packet_stage_source_route_fkey
  FOREIGN KEY(owner_user_id,source_route_event_id)
  REFERENCES memory.v5_2_local_packet_route_event(
    owner_user_id,route_event_id
  ) ON DELETE RESTRICT,
  ADD CONSTRAINT v5_local_packet_stage_source_atom_apply_fkey
  FOREIGN KEY(owner_user_id,source_atom_apply_id)
  REFERENCES memory.v5_2_atom_admission_apply(
    owner_user_id,apply_id
  ) ON DELETE RESTRICT,
  ADD CONSTRAINT v5_local_packet_stage_source_route_key
  UNIQUE(owner_user_id,source_route_event_id),
  ADD CONSTRAINT v5_local_packet_stage_source_atom_apply_key
  UNIQUE(owner_user_id,source_atom_apply_id);

ALTER TABLE memory.v5_local_packet_stage_admission
  DROP CONSTRAINT v5_local_packet_stage_admission_policy_decision_check;
ALTER TABLE memory.v5_local_packet_stage_admission
  ADD CONSTRAINT v5_local_packet_stage_admission_policy_decision_check
  CHECK (
    (
      artifact_id IS NOT NULL
      AND source_route_event_id IS NULL
      AND source_atom_apply_id IS NULL
      AND (
        (decision='auto_stage_eligible'
          AND policy_version='memory_v1_v5_local_auto_stage_policy_v1')
        OR
        (decision='validated_entity_stage'
          AND policy_version='memory_v1_v5_local_entity_validation_policy_v1')
        OR
        (decision='reviewed_entity_stage'
          AND policy_version='memory_v1_v5_reviewed_stage_admission_policy_v1')
      )
    )
    OR
    (
      artifact_id IS NULL
      AND source_route_event_id IS NOT NULL
      AND source_atom_apply_id IS NOT NULL
      AND decision='v5_2_atom_reviewed_stage'
      AND policy_version=
        'memory_v1_v5_2_atom_reviewed_stage_admission_policy_v1'
    )
    OR
    (
      artifact_id IS NULL
      AND source_route_event_id IS NOT NULL
      AND source_atom_apply_id IS NULL
      AND decision='v5_2_reviewed_route_stage'
      AND policy_version=
        'memory_v1_v5_2_reviewed_route_stage_admission_policy_v1'
    )
  );

CREATE TABLE memory.v5_2_reviewed_observation_stage_admission (
  admission_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  route_event_id uuid NOT NULL,
  atom_apply_id uuid,
  batch_id uuid NOT NULL,
  source_kind text NOT NULL CHECK (
    (source_kind='atom_apply' AND atom_apply_id IS NOT NULL)
    OR
    (source_kind='reviewed_route' AND atom_apply_id IS NULL)
  ),
  stage_manifest_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(stage_manifest_sha256)),
  resolution_state_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(resolution_state_sha256)),
  observation_state_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(observation_state_sha256)),
  resolution_count smallint NOT NULL
    CHECK (resolution_count BETWEEN 1 AND 24),
  approved_review_count smallint NOT NULL
    CHECK (approved_review_count BETWEEN 0 AND 24),
  observation_count smallint NOT NULL
    CHECK (observation_count BETWEEN 1 AND 32),
  binding_count smallint NOT NULL
    CHECK (binding_count BETWEEN 1 AND 32),
  policy_version text NOT NULL CHECK (
    policy_version IN (
      'memory_v1_v5_2_atom_reviewed_stage_admission_policy_v1',
      'memory_v1_v5_2_reviewed_route_stage_admission_policy_v1'
    )
  ),
  decision text NOT NULL CHECK (
    decision IN (
      'v5_2_atom_reviewed_stage',
      'v5_2_reviewed_route_stage'
    )
  ),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,admission_id),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,route_event_id),
  UNIQUE(owner_user_id,atom_apply_id),
  UNIQUE(owner_user_id,batch_id),
  FOREIGN KEY(owner_user_id,admission_id)
    REFERENCES memory.v5_local_packet_stage_admission(
      owner_user_id,admission_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,route_event_id)
    REFERENCES memory.v5_2_local_packet_route_event(
      owner_user_id,route_event_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,atom_apply_id)
    REFERENCES memory.v5_2_atom_admission_apply(
      owner_user_id,apply_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,batch_id)
    REFERENCES memory.relational_stage_batch(owner_user_id,batch_id)
    ON DELETE RESTRICT,
  CHECK (
    (source_kind='atom_apply'
      AND decision='v5_2_atom_reviewed_stage'
      AND policy_version=
        'memory_v1_v5_2_atom_reviewed_stage_admission_policy_v1')
    OR
    (source_kind='reviewed_route'
      AND decision='v5_2_reviewed_route_stage'
      AND policy_version=
        'memory_v1_v5_2_reviewed_route_stage_admission_policy_v1')
  )
);
ALTER TABLE memory.v5_2_reviewed_observation_stage_admission
  OWNER TO memory_v5_2_reviewed_observation_stage_maintainer;
ALTER TABLE memory.v5_2_reviewed_observation_stage_admission
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_2_reviewed_observation_stage_admission
  FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation
  ON memory.v5_2_reviewed_observation_stage_admission
  TO memory_v5_2_reviewed_observation_stage_maintainer
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
CREATE POLICY local_entailment_read
  ON memory.v5_2_reviewed_observation_stage_admission
  FOR SELECT TO memory_v5_local_entailment_maintainer
  USING (owner_user_id=memory.current_actor_user_id());
CREATE TRIGGER v5_2_reviewed_observation_stage_append_only_guard
BEFORE UPDATE OR DELETE
ON memory.v5_2_reviewed_observation_stage_admission
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

CREATE POLICY v5_2_reviewed_observation_stage_access
  ON memory.v5_local_packet_stage_admission
  TO memory_v5_2_reviewed_observation_stage_maintainer
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());

DO $read_policies$
DECLARE
  target regclass;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'memory.v5_2_local_packet_route_event'::regclass,
    'memory.v5_2_atom_admission_apply'::regclass,
    'memory.v5_2_atom_admission_proposal'::regclass,
    'memory.v5_2_atom_admission_review'::regclass,
    'memory.evidence_extraction_packet_v5_local'::regclass,
    'memory.evidence_extraction_job'::regclass,
    'memory.evidence'::regclass,
    'memory.relational_stage_batch'::regclass,
    'memory.relational_operation_request'::regclass,
    'memory.entity_resolution_plan'::regclass,
    'memory.entity_resolution_review'::regclass,
    'memory.entity_resolution_apply'::regclass,
    'memory.observation'::regclass,
    'memory.observation_entity_binding'::regclass
  ] LOOP
    EXECUTE format(
      'CREATE POLICY v5_2_reviewed_observation_stage_read ON %s FOR SELECT TO memory_v5_2_reviewed_observation_stage_maintainer USING (owner_user_id=memory.current_actor_user_id())',
      target
    );
  END LOOP;
END
$read_policies$;

GRANT USAGE ON SCHEMA memory
TO memory_v5_2_reviewed_observation_stage_maintainer;
GRANT SELECT ON
  memory.v5_2_local_packet_route_event,
  memory.v5_2_atom_admission_apply,
  memory.v5_2_atom_admission_proposal,
  memory.v5_2_atom_admission_review,
  memory.evidence_extraction_packet_v5_local,
  memory.evidence_extraction_job,
  memory.evidence,
  memory.relational_stage_batch,
  memory.relational_operation_request,
  memory.entity_resolution_plan,
  memory.entity_resolution_review,
  memory.entity_resolution_apply,
  memory.observation,
  memory.observation_entity_binding
TO memory_v5_2_reviewed_observation_stage_maintainer;
GRANT SELECT,INSERT ON
  memory.v5_local_packet_stage_admission,
  memory.v5_2_reviewed_observation_stage_admission
TO memory_v5_2_reviewed_observation_stage_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id(),
  memory.v5_digest_text(text),
  memory.v5_sha256_valid(text)
TO memory_v5_2_reviewed_observation_stage_maintainer;

CREATE OR REPLACE FUNCTION
memory.v5_2_reviewed_observation_stage_source_v1(
  p_route_event_id uuid,
  p_batch_id uuid
)
RETURNS TABLE(
  source_kind text,
  route_event_id uuid,
  atom_apply_id uuid,
  packet_id uuid,
  job_id uuid,
  evidence_id uuid,
  batch_id uuid,
  stage_request_id uuid,
  review_report_sha256 text,
  stage_bundle_sha256 text,
  packet_storage_sha256 text,
  repository_commit text,
  stage_manifest_sha256 text,
  resolution_state_sha256 text,
  observation_state_sha256 text,
  entity_mention_count integer,
  observation_count integer,
  resolution_count integer,
  approved_review_count integer,
  binding_count integer,
  source_created_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'reviewed-observation source requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;

  RETURN QUERY
  WITH base AS (
    SELECT
      CASE
        WHEN atom.apply_id IS NOT NULL
          AND atom.stage_projection_sha256=batch.extraction_packet_sha256
          AND batch.extractor IN (
            'memory_v1_v5_2_atom_stage_projection',
            'memory_v1_v5_2_atom_stage_projection_v2'
          )
          THEN 'atom_apply'::text
        WHEN atom.apply_id IS NULL
          AND route.blocking_code_count=0
          AND route.request_id=request.request_id
          AND route.validator_packet_sha256=batch.extraction_packet_sha256
          AND batch.extractor='memory_v1_v5_2_local_packet_review'
          THEN 'reviewed_route'::text
        ELSE NULL::text
      END AS source_kind,
      route.route_event_id,atom.apply_id AS atom_apply_id,
      route.packet_id,route.job_id,route.evidence_id,batch.batch_id,
      request.request_id AS stage_request_id,
      route.review_report_sha256,route.stage_bundle_sha256,
      route.packet_storage_sha256,route.repository_commit,
      batch.stage_manifest_sha256,batch.mention_count,
      batch.observation_count,batch.resolution_count,batch.result,
      greatest(route.created_at,batch.created_at) AS source_created_at
    FROM memory.v5_2_local_packet_route_event AS route
    JOIN memory.evidence_extraction_packet_v5_local AS packet
      ON packet.owner_user_id=route.owner_user_id
     AND packet.packet_id=route.packet_id
     AND packet.job_id=route.job_id
     AND packet.evidence_id=route.evidence_id
    JOIN memory.evidence_extraction_job AS job
      ON job.owner_user_id=route.owner_user_id
     AND job.job_id=route.job_id
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=route.owner_user_id
     AND evidence.evidence_id=route.evidence_id
    JOIN memory.relational_stage_batch AS batch
      ON batch.owner_user_id=route.owner_user_id
     AND batch.batch_id=p_batch_id
     AND batch.evidence_id=route.evidence_id
    JOIN memory.relational_operation_request AS request
      ON request.owner_user_id=batch.owner_user_id
     AND request.operation='stage_packet'
     AND request.outcome='applied'
     AND request.result->>'batch_id'=batch.batch_id::text
     AND request.manifest_sha256=batch.stage_manifest_sha256
    LEFT JOIN memory.v5_2_atom_admission_apply AS atom
      ON atom.owner_user_id=route.owner_user_id
     AND atom.packet_id=route.packet_id
    LEFT JOIN memory.v5_2_atom_admission_proposal AS proposal
      ON proposal.owner_user_id=atom.owner_user_id
     AND proposal.proposal_id=atom.proposal_id
     AND proposal.packet_id=route.packet_id
     AND proposal.evidence_id=route.evidence_id
     AND proposal.stage_projection_sha256=atom.stage_projection_sha256
    LEFT JOIN memory.v5_2_atom_admission_review AS atom_review
      ON atom_review.owner_user_id=atom.owner_user_id
     AND atom_review.review_id=atom.review_id
     AND atom_review.proposal_id=atom.proposal_id
     AND atom_review.decision='authorized'
    WHERE route.owner_user_id=actor
      AND route.route_event_id=p_route_event_id
      AND route.route='manual_review_artifact_ready'
      AND route.reason_code='reviewable_relational_packet_v5_2'
      AND route.review_contract='memory_v1_v5_2_local_packet_review_v1'
      AND route.bundle_contract='memory_v1_v5_2_stage_preflight_v1'
      AND packet.packet_storage_sha256=route.packet_storage_sha256
      AND packet.evidence_content_sha256=evidence.content_sha256
      AND packet.external_model_calls=0
      AND job.status='review_required'
      AND job.lease_token IS NULL
      AND job.lease_expires_at IS NULL
      AND job.last_error IS NULL
      AND evidence.status='active'
      AND evidence.content IS NOT NULL
      AND memory.v5_digest_text(evidence.content)=evidence.content_sha256
      AND jsonb_typeof(batch.result->'resolution_ids')='object'
      AND jsonb_typeof(batch.result->'observation_ids')='object'
      AND (SELECT count(*)
        FROM jsonb_object_keys(batch.result->'resolution_ids'))
          =batch.resolution_count
      AND (SELECT count(*)
        FROM jsonb_object_keys(batch.result->'observation_ids'))
          =batch.observation_count
      AND (
        atom.apply_id IS NULL
        OR (
          proposal.proposal_id IS NOT NULL
          AND atom_review.review_id IS NOT NULL
          AND atom.proposal_sha256=proposal.proposal_sha256
          AND atom.review_authorization_manifest_sha256=
            atom_review.authorization_manifest_sha256
        )
      )
  ), resolution_state AS (
    SELECT
      base.route_event_id,
      count(*)::integer AS exact_resolution_count,
      count(applied.resolution_id)::integer AS applied_count,
      count(*) FILTER (
        WHERE plan.decision_state='manual_review_required'
      )::integer AS manual_count,
      count(*) FILTER (
        WHERE plan.decision_state='manual_review_required'
          AND review.review_id IS NOT NULL
          AND review.decision='approved'
      )::integer AS approved_review_count,
      count(*) FILTER (
        WHERE plan.action NOT IN ('link_existing','create_new')
           OR plan.decision_state NOT IN (
             'auto_link_eligible','manual_review_required'
           )
      )::integer AS invalid_resolution_count,
      memory.v5_digest_text(coalesce(string_agg(concat_ws('|',
        plan.resolution_id::text,plan.action::text,
        plan.decision_state::text,
        coalesce(applied.applied_entity_id::text,''),
        coalesce(applied.apply_manifest_sha256,''),
        coalesce(review.review_id::text,''),
        coalesce(review.authorization_manifest_sha256,'')
      ),E'\n' ORDER BY plan.resolution_id),'')) AS state_sha256
    FROM base
    JOIN LATERAL jsonb_each_text(
      base.result->'resolution_ids'
    ) AS ids ON true
    JOIN memory.entity_resolution_plan AS plan
      ON plan.owner_user_id=actor
     AND plan.resolution_id=ids.value::uuid
     AND plan.evidence_id=base.evidence_id
    LEFT JOIN memory.entity_resolution_apply AS applied
      ON applied.owner_user_id=plan.owner_user_id
     AND applied.resolution_id=plan.resolution_id
    LEFT JOIN memory.entity_resolution_review AS review
      ON review.owner_user_id=applied.owner_user_id
     AND review.resolution_id=applied.resolution_id
     AND review.review_id=applied.review_id
    GROUP BY base.route_event_id
  ), observation_state AS (
    SELECT
      base.route_event_id,
      count(*)::integer AS exact_observation_count,
      count(binding.observation_id)::integer AS binding_count,
      memory.v5_digest_text(coalesce(string_agg(concat_ws('|',
        observation.observation_id::text,
        observation.observation_sha256,
        coalesce(binding.binding_manifest_sha256,'')
      ),E'\n' ORDER BY observation.observation_id),'')) AS state_sha256
    FROM base
    JOIN LATERAL jsonb_each_text(
      base.result->'observation_ids'
    ) AS ids ON true
    JOIN memory.observation AS observation
      ON observation.owner_user_id=actor
     AND observation.observation_id=ids.value::uuid
     AND observation.evidence_id=base.evidence_id
     AND observation.observation_ref=ids.key
     AND observation.packet_sha256=(
       SELECT exact_batch.extraction_packet_sha256
       FROM memory.relational_stage_batch AS exact_batch
       WHERE exact_batch.owner_user_id=actor
         AND exact_batch.batch_id=base.batch_id
     )
     AND observation.projection_class<>'never_surface'
    LEFT JOIN memory.observation_entity_binding AS binding
      ON binding.owner_user_id=observation.owner_user_id
     AND binding.observation_id=observation.observation_id
    GROUP BY base.route_event_id
  )
  SELECT base.source_kind,base.route_event_id,base.atom_apply_id,
    base.packet_id,base.job_id,base.evidence_id,base.batch_id,
    base.stage_request_id,base.review_report_sha256,
    base.stage_bundle_sha256,base.packet_storage_sha256,
    base.repository_commit,base.stage_manifest_sha256,
    resolution_state.state_sha256,observation_state.state_sha256,
    base.mention_count::integer,base.observation_count::integer,
    base.resolution_count::integer,
    resolution_state.approved_review_count,
    observation_state.binding_count,base.source_created_at
  FROM base
  JOIN resolution_state USING(route_event_id)
  JOIN observation_state USING(route_event_id)
  WHERE base.source_kind IS NOT NULL
    AND resolution_state.exact_resolution_count=base.resolution_count
    AND resolution_state.applied_count=base.resolution_count
    AND resolution_state.manual_count=
      resolution_state.approved_review_count
    AND resolution_state.invalid_resolution_count=0
    AND observation_state.exact_observation_count=base.observation_count
    AND observation_state.binding_count=base.observation_count;
END
$function$;

CREATE OR REPLACE FUNCTION
memory.plan_owner_v5_2_reviewed_observation_stage_v1(
  p_limit integer DEFAULT 1
)
RETURNS TABLE(
  source_kind text,
  route_event_id uuid,
  atom_apply_id uuid,
  batch_id uuid,
  stage_manifest_sha256 text,
  resolution_state_sha256 text,
  observation_state_sha256 text,
  observation_count integer,
  source_created_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'reviewed-observation plan requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION 'reviewed-observation plan limit is invalid'
      USING ERRCODE='22023';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT route.route_event_id,batch.batch_id
    FROM memory.v5_2_local_packet_route_event AS route
    JOIN memory.v5_2_atom_admission_apply AS atom
      ON atom.owner_user_id=route.owner_user_id
     AND atom.packet_id=route.packet_id
    JOIN memory.relational_stage_batch AS batch
      ON batch.owner_user_id=route.owner_user_id
     AND batch.evidence_id=route.evidence_id
     AND batch.extraction_packet_sha256=atom.stage_projection_sha256
     AND batch.extractor IN (
       'memory_v1_v5_2_atom_stage_projection',
       'memory_v1_v5_2_atom_stage_projection_v2'
     )
    WHERE route.owner_user_id=actor
    UNION
    SELECT route.route_event_id,batch.batch_id
    FROM memory.v5_2_local_packet_route_event AS route
    JOIN memory.relational_operation_request AS request
      ON request.owner_user_id=route.owner_user_id
     AND request.request_id=route.request_id
     AND request.operation='stage_packet'
     AND request.outcome='applied'
    JOIN memory.relational_stage_batch AS batch
      ON batch.owner_user_id=request.owner_user_id
     AND batch.batch_id=(request.result->>'batch_id')::uuid
     AND batch.evidence_id=route.evidence_id
     AND batch.extraction_packet_sha256=route.validator_packet_sha256
     AND batch.extractor='memory_v1_v5_2_local_packet_review'
    WHERE route.owner_user_id=actor
      AND route.blocking_code_count=0
  )
  SELECT source.source_kind,source.route_event_id,
    source.atom_apply_id,source.batch_id,source.stage_manifest_sha256,
    source.resolution_state_sha256,source.observation_state_sha256,
    source.observation_count,source.source_created_at
  FROM candidate
  CROSS JOIN LATERAL
    memory.v5_2_reviewed_observation_stage_source_v1(
      candidate.route_event_id,candidate.batch_id
    ) AS source
  WHERE NOT EXISTS (
    SELECT 1
    FROM memory.v5_local_packet_stage_admission AS prior
    WHERE prior.owner_user_id=actor
      AND (
        prior.source_route_event_id=source.route_event_id
        OR (
          source.atom_apply_id IS NOT NULL
          AND prior.source_atom_apply_id=source.atom_apply_id
        )
      )
  )
  ORDER BY source.source_created_at,source.route_event_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION
memory.register_owner_v5_2_reviewed_observation_stage_v1(
  p_admission_id uuid,
  p_operation_id uuid,
  p_route_event_id uuid,
  p_expected_atom_apply_id uuid,
  p_expected_batch_id uuid,
  p_expected_stage_manifest_sha256 text,
  p_expected_resolution_state_sha256 text,
  p_expected_observation_state_sha256 text,
  p_policy_version text
)
RETURNS TABLE(
  admission_id uuid,
  route_event_id uuid,
  batch_id uuid,
  decision text,
  outcome text,
  rows_written integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  source record;
  replayed memory.v5_2_reviewed_observation_stage_admission%ROWTYPE;
  expected_decision text;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'reviewed-observation register requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_admission_id IS NULL OR p_operation_id IS NULL
     OR p_route_event_id IS NULL OR p_expected_batch_id IS NULL
     OR NOT memory.v5_sha256_valid(p_expected_stage_manifest_sha256)
     OR NOT memory.v5_sha256_valid(p_expected_resolution_state_sha256)
     OR NOT memory.v5_sha256_valid(p_expected_observation_state_sha256)
     OR p_policy_version NOT IN (
       'memory_v1_v5_2_atom_reviewed_stage_admission_policy_v1',
       'memory_v1_v5_2_reviewed_route_stage_admission_policy_v1'
     ) THEN
    RAISE EXCEPTION 'reviewed-observation register inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_2_reviewed_observation_stage',actor::text,
    p_route_event_id::text
  ),0));

  SELECT * INTO replayed
  FROM memory.v5_2_reviewed_observation_stage_admission AS value
  WHERE value.owner_user_id=actor
    AND (
      value.admission_id=p_admission_id
      OR value.operation_id=p_operation_id
      OR value.route_event_id=p_route_event_id
      OR (
        p_expected_atom_apply_id IS NOT NULL
        AND value.atom_apply_id=p_expected_atom_apply_id
      )
      OR value.batch_id=p_expected_batch_id
    );
  IF FOUND THEN
    IF replayed.admission_id<>p_admission_id
       OR replayed.operation_id<>p_operation_id
       OR replayed.route_event_id<>p_route_event_id
       OR replayed.atom_apply_id IS DISTINCT FROM p_expected_atom_apply_id
       OR replayed.batch_id<>p_expected_batch_id
       OR replayed.stage_manifest_sha256
            <>p_expected_stage_manifest_sha256
       OR replayed.resolution_state_sha256
            <>p_expected_resolution_state_sha256
       OR replayed.observation_state_sha256
            <>p_expected_observation_state_sha256
       OR replayed.policy_version<>p_policy_version THEN
      RAISE EXCEPTION 'reviewed-observation replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.admission_id,replayed.route_event_id,
      replayed.batch_id,replayed.decision,'replayed'::text,0;
    RETURN;
  END IF;

  SELECT * INTO source
  FROM memory.v5_2_reviewed_observation_stage_source_v1(
    p_route_event_id,p_expected_batch_id
  );
  IF NOT FOUND THEN
    RAISE EXCEPTION 'eligible reviewed-observation source not found'
      USING ERRCODE='P0002';
  END IF;
  expected_decision:=CASE source.source_kind
    WHEN 'atom_apply' THEN 'v5_2_atom_reviewed_stage'
    WHEN 'reviewed_route' THEN 'v5_2_reviewed_route_stage'
    ELSE NULL
  END;
  IF source.atom_apply_id IS DISTINCT FROM p_expected_atom_apply_id
     OR source.stage_manifest_sha256<>p_expected_stage_manifest_sha256
     OR source.resolution_state_sha256
          <>p_expected_resolution_state_sha256
     OR source.observation_state_sha256
          <>p_expected_observation_state_sha256
     OR (
       source.source_kind='atom_apply'
       AND p_policy_version<>
         'memory_v1_v5_2_atom_reviewed_stage_admission_policy_v1'
     )
     OR (
       source.source_kind='reviewed_route'
       AND p_policy_version<>
         'memory_v1_v5_2_reviewed_route_stage_admission_policy_v1'
     ) THEN
    RAISE EXCEPTION 'reviewed-observation source drifted'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_local_packet_stage_admission(
    admission_id,owner_user_id,operation_id,artifact_id,
    source_route_event_id,source_atom_apply_id,packet_id,job_id,
    evidence_id,stage_request_id,review_report_sha256,
    stage_bundle_sha256,packet_storage_sha256,repository_commit,
    policy_version,decision,entity_mention_count,observation_count
  ) VALUES (
    p_admission_id,actor,p_operation_id,NULL,
    source.route_event_id,source.atom_apply_id,source.packet_id,
    source.job_id,source.evidence_id,source.stage_request_id,
    source.review_report_sha256,source.stage_bundle_sha256,
    source.packet_storage_sha256,source.repository_commit,
    p_policy_version,expected_decision,source.entity_mention_count,
    source.observation_count
  );
  INSERT INTO memory.v5_2_reviewed_observation_stage_admission(
    admission_id,owner_user_id,operation_id,route_event_id,
    atom_apply_id,batch_id,source_kind,stage_manifest_sha256,
    resolution_state_sha256,observation_state_sha256,
    resolution_count,approved_review_count,observation_count,
    binding_count,policy_version,decision
  ) VALUES (
    p_admission_id,actor,p_operation_id,source.route_event_id,
    source.atom_apply_id,source.batch_id,source.source_kind,
    source.stage_manifest_sha256,source.resolution_state_sha256,
    source.observation_state_sha256,source.resolution_count,
    source.approved_review_count,source.observation_count,
    source.binding_count,p_policy_version,expected_decision
  );
  RETURN QUERY SELECT p_admission_id,source.route_event_id,
    source.batch_id,expected_decision,'applied'::text,2;
END
$function$;

ALTER FUNCTION memory.v5_2_reviewed_observation_stage_source_v1(uuid,uuid)
  OWNER TO memory_v5_2_reviewed_observation_stage_maintainer;
ALTER FUNCTION memory.plan_owner_v5_2_reviewed_observation_stage_v1(integer)
  OWNER TO memory_v5_2_reviewed_observation_stage_maintainer;
ALTER FUNCTION memory.register_owner_v5_2_reviewed_observation_stage_v1(
  uuid,uuid,uuid,uuid,uuid,text,text,text,text
) OWNER TO memory_v5_2_reviewed_observation_stage_maintainer;
REVOKE ALL ON FUNCTION
  memory.v5_2_reviewed_observation_stage_source_v1(uuid,uuid)
FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_2_reviewed_observation_stage_v1(integer)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
  memory.register_owner_v5_2_reviewed_observation_stage_v1(
    uuid,uuid,uuid,uuid,uuid,text,text,text,text
  )
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_2_reviewed_observation_stage_v1(integer)
TO brains_app;
GRANT EXECUTE ON FUNCTION
  memory.register_owner_v5_2_reviewed_observation_stage_v1(
    uuid,uuid,uuid,uuid,uuid,text,text,text,text
  )
TO brains_app;

CREATE OR REPLACE FUNCTION memory.v5_local_stage_admission_batch_v2(
  p_stage_admission_id uuid
)
RETURNS TABLE(batch_id uuid)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  stage memory.v5_local_packet_stage_admission%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'stage batch resolution requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  SELECT * INTO stage
  FROM memory.v5_local_packet_stage_admission AS value
  WHERE value.owner_user_id=actor
    AND value.admission_id=p_stage_admission_id;
  IF NOT FOUND THEN
    RETURN;
  END IF;

  IF stage.decision='auto_stage_eligible' THEN
    RETURN QUERY
    SELECT batch.batch_id
    FROM memory.relational_operation_request AS request
    JOIN memory.relational_stage_batch AS batch
      ON batch.owner_user_id=request.owner_user_id
     AND batch.batch_id=(request.result->>'batch_id')::uuid
     AND batch.stage_manifest_sha256=request.manifest_sha256
    WHERE request.owner_user_id=actor
      AND request.request_id=stage.stage_request_id
      AND request.operation='stage_packet'
      AND request.outcome='applied';
  ELSIF stage.decision='validated_entity_stage' THEN
    RETURN QUERY
    SELECT value.batch_id
    FROM memory.v5_local_validated_stage_admission AS value
    WHERE value.owner_user_id=actor
      AND value.admission_id=stage.admission_id;
  ELSIF stage.decision='reviewed_entity_stage' THEN
    RETURN QUERY
    SELECT value.batch_id
    FROM memory.v5_local_reviewed_stage_admission AS value
    WHERE value.owner_user_id=actor
      AND value.admission_id=stage.admission_id;
  ELSIF stage.decision IN (
    'v5_2_atom_reviewed_stage','v5_2_reviewed_route_stage'
  ) THEN
    RETURN QUERY
    SELECT value.batch_id
    FROM memory.v5_2_reviewed_observation_stage_admission AS value
    WHERE value.owner_user_id=actor
      AND value.admission_id=stage.admission_id;
  END IF;
END
$function$;

ALTER FUNCTION memory.v5_local_stage_admission_batch_v2(uuid)
  OWNER TO memory_v5_local_entailment_maintainer;
REVOKE ALL ON FUNCTION memory.v5_local_stage_admission_batch_v2(uuid)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.v5_local_stage_admission_batch_v2(uuid)
TO brains_app;

DO $entailment_reads$
DECLARE
  target regclass;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'memory.relational_operation_request'::regclass,
    'memory.v5_local_validated_stage_admission'::regclass,
    'memory.v5_local_reviewed_stage_admission'::regclass
  ] LOOP
    EXECUTE format(
      'CREATE POLICY local_entailment_batch_read ON %s FOR SELECT TO memory_v5_local_entailment_maintainer USING (owner_user_id=memory.current_actor_user_id())',
      target
    );
  END LOOP;
END
$entailment_reads$;
GRANT SELECT ON
  memory.relational_operation_request,
  memory.v5_local_validated_stage_admission,
  memory.v5_local_reviewed_stage_admission,
  memory.v5_2_reviewed_observation_stage_admission
TO memory_v5_local_entailment_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
TO memory_v5_local_entailment_maintainer;

DO $entailment_compatibility$
DECLARE
  plan_def text;
  register_def text;
  old_join constant text :=
'JOIN memory.relational_stage_batch AS batch
    ON batch.owner_user_id=stage.owner_user_id
   AND batch.evidence_id=stage.evidence_id';
  new_join constant text :=
'JOIN LATERAL memory.v5_local_stage_admission_batch_v2(
    stage.admission_id
  ) AS stage_batch ON true
  JOIN memory.relational_stage_batch AS batch
    ON batch.owner_user_id=stage.owner_user_id
   AND batch.batch_id=stage_batch.batch_id';
  old_decisions constant text :=
'''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage''';
  new_decisions constant text :=
'''auto_stage_eligible'',''validated_entity_stage'',''reviewed_entity_stage'',''v5_2_atom_reviewed_stage'',''v5_2_reviewed_route_stage''';
BEGIN
  plan_def:=pg_get_functiondef(
    'memory.plan_owner_v5_local_entailment_v1(integer)'::regprocedure
  );
  IF position(new_join IN plan_def)=0 THEN
    IF position(old_join IN plan_def)=0 THEN
      RAISE EXCEPTION 'local entailment planner join drifted';
    END IF;
    plan_def:=replace(plan_def,old_join,new_join);
  END IF;
  IF position(new_decisions IN plan_def)=0 THEN
    IF position(old_decisions IN plan_def)=0 THEN
      RAISE EXCEPTION 'local entailment planner decisions drifted';
    END IF;
    plan_def:=replace(plan_def,old_decisions,new_decisions);
  END IF;
  EXECUTE plan_def;

  register_def:=pg_get_functiondef(
    'memory.register_owner_v5_local_entailment_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,text,memory.observation_entailment_decision_v5,text,jsonb,text)'::regprocedure
  );
  IF position(new_join IN register_def)=0 THEN
    IF position(old_join IN register_def)=0 THEN
      RAISE EXCEPTION 'local entailment register join drifted';
    END IF;
    register_def:=replace(register_def,old_join,new_join);
  END IF;
  IF position(new_decisions IN register_def)=0 THEN
    IF position(old_decisions IN register_def)=0 THEN
      RAISE EXCEPTION 'local entailment register decisions drifted';
    END IF;
    register_def:=replace(register_def,old_decisions,new_decisions);
  END IF;
  EXECUTE register_def;
END
$entailment_compatibility$;

COMMIT;
