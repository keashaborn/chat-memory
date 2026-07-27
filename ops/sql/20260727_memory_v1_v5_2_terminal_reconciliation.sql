\set ON_ERROR_STOP on
BEGIN;

DO $baseline$
DECLARE
  current_sha text;
BEGIN
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.owner_v5_local_inference_circuit_state_v2(text,text,text,text,text,text,integer,integer)'::regprocedure
  ), 'UTF8'), 'sha256'), 'hex')
  INTO current_sha;
  IF current_sha <> 'e207e9376d0b0df19059645be66777aae18e8abb20b565313601b685ff4de899' THEN
    RAISE EXCEPTION 'terminal reconciliation circuit baseline changed: %',
      current_sha USING ERRCODE = '23514';
  END IF;
END
$baseline$;

CREATE TABLE memory.v5_local_inference_outcome_supersession_v1 (
  supersession_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  job_id uuid NOT NULL,
  original_event_id uuid NOT NULL,
  superseding_event_id uuid NOT NULL,
  original_rejection_code text NOT NULL,
  superseding_rejection_code text NOT NULL,
  original_event_sha256 text NOT NULL,
  superseding_event_sha256 text NOT NULL,
  supersession_basis_sha256 text NOT NULL,
  policy_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, operation_id),
  UNIQUE (owner_user_id, original_event_id),
  FOREIGN KEY (owner_user_id, job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id, job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, original_event_id)
    REFERENCES memory.v5_local_inference_event(owner_user_id, event_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, superseding_event_id)
    REFERENCES memory.v5_local_inference_event(owner_user_id, event_id)
    ON DELETE RESTRICT,
  CHECK (original_event_id <> superseding_event_id),
  CHECK (original_rejection_code = 'uncatalogued_validator_rejection'),
  CHECK (superseding_rejection_code = 'context_missing'),
  CHECK (original_event_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (superseding_event_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (supersession_basis_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (
    policy_version =
      'memory_v1_v5_2_terminal_reconciliation_policy_v1'
  )
);

CREATE INDEX v5_local_outcome_supersession_owner_time_idx
  ON memory.v5_local_inference_outcome_supersession_v1(
    owner_user_id, created_at DESC, supersession_id
  );

ALTER TABLE memory.v5_local_inference_outcome_supersession_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_inference_outcome_supersession_v1
  FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_isolation
  ON memory.v5_local_inference_outcome_supersession_v1
  TO memory_v5_local_inference_maintainer
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());

CREATE TRIGGER v5_local_outcome_supersession_append_only_guard
BEFORE UPDATE OR DELETE
ON memory.v5_local_inference_outcome_supersession_v1
FOR EACH ROW
EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

CREATE TABLE memory.v5_local_terminal_reconciliation_v1 (
  reconciliation_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  packet_id uuid NOT NULL,
  job_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  source_completion_event_id uuid NOT NULL,
  source_route_event_id uuid,
  source_disposition_id uuid,
  inventory_set text NOT NULL,
  target_disposition text NOT NULL,
  normalized_reason_code text NOT NULL,
  precedence_rank smallint NOT NULL,
  precedence_code text NOT NULL,
  evidence_content_sha256 text NOT NULL,
  validator_packet_sha256 text NOT NULL,
  packet_storage_sha256 text NOT NULL,
  source_completion_event_sha256 text NOT NULL,
  source_classification_sha256 text NOT NULL,
  reconciliation_basis_sha256 text NOT NULL,
  policy_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id, operation_id),
  UNIQUE (owner_user_id, packet_id),
  FOREIGN KEY (owner_user_id, packet_id)
    REFERENCES memory.evidence_extraction_packet_v5_local(
      owner_user_id, packet_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id, job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, source_completion_event_id)
    REFERENCES memory.v5_local_inference_event(owner_user_id, event_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (source_route_event_id)
    REFERENCES memory.v5_2_local_packet_route_event(route_event_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (source_disposition_id)
    REFERENCES memory.v5_local_packet_disposition(disposition_id)
    ON DELETE RESTRICT,
  CHECK (
    inventory_set IN (
      'terminal_no_stage_verified',
      'deferral_only',
      'terminal_skipped'
    )
  ),
  CHECK (target_disposition IN ('skipped', 'deferred')),
  CHECK (
    normalized_reason_code IN (
      'empty_packet',
      'insufficient_evidence',
      'structured_domain',
      'transient_state',
      'question_only',
      'context_missing'
    )
  ),
  CHECK (precedence_rank BETWEEN 10 AND 90),
  CHECK (
    precedence_code IN (
      'p10_context_missing',
      'p20_question_only',
      'p30_structured_domain',
      'p40_transient_state',
      'p50_insufficient_evidence',
      'p60_empty_packet'
    )
  ),
  CHECK (evidence_content_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (validator_packet_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (packet_storage_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (source_completion_event_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (source_classification_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (reconciliation_basis_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (
    policy_version =
      'memory_v1_v5_2_terminal_reconciliation_policy_v1'
  )
);

CREATE INDEX v5_local_terminal_reconciliation_owner_time_idx
  ON memory.v5_local_terminal_reconciliation_v1(
    owner_user_id, created_at DESC, reconciliation_id
  );
CREATE INDEX v5_local_terminal_reconciliation_owner_disposition_idx
  ON memory.v5_local_terminal_reconciliation_v1(
    owner_user_id, target_disposition, normalized_reason_code
  );

ALTER TABLE memory.v5_local_terminal_reconciliation_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_local_terminal_reconciliation_v1
  FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_isolation
  ON memory.v5_local_terminal_reconciliation_v1
  TO memory_v5_local_inference_maintainer
  USING (owner_user_id = memory.current_actor_user_id())
  WITH CHECK (owner_user_id = memory.current_actor_user_id());

CREATE TRIGGER v5_local_terminal_reconciliation_append_only_guard
BEFORE UPDATE OR DELETE
ON memory.v5_local_terminal_reconciliation_v1
FOR EACH ROW
EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

CREATE POLICY v5_terminal_reconciliation_packet_read
  ON memory.evidence_extraction_packet_v5_local
  FOR SELECT TO memory_v5_local_inference_maintainer
  USING (owner_user_id = memory.current_actor_user_id());

CREATE POLICY v5_terminal_reconciliation_route_read
  ON memory.v5_2_local_packet_route_event
  FOR SELECT TO memory_v5_local_inference_maintainer
  USING (owner_user_id = memory.current_actor_user_id());

CREATE POLICY v5_terminal_reconciliation_disposition_read
  ON memory.v5_local_packet_disposition
  FOR SELECT TO memory_v5_local_inference_maintainer
  USING (owner_user_id = memory.current_actor_user_id());

CREATE POLICY v5_terminal_reconciliation_stage_read
  ON memory.relational_stage_batch
  FOR SELECT TO memory_v5_local_inference_maintainer
  USING (owner_user_id = memory.current_actor_user_id());

GRANT SELECT ON
  memory.evidence_extraction_packet_v5_local,
  memory.v5_2_local_packet_route_event,
  memory.v5_local_packet_disposition,
  memory.relational_stage_batch,
  memory.v5_local_inference_outcome_event
TO memory_v5_local_inference_maintainer;

GRANT SELECT, INSERT ON
  memory.v5_local_inference_outcome_supersession_v1,
  memory.v5_local_terminal_reconciliation_v1
TO memory_v5_local_inference_maintainer;

CREATE OR REPLACE FUNCTION memory.v5_local_event_basis_sha256_v1(
  p_event_id uuid
)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
  SELECT encode(public.digest(convert_to(jsonb_build_object(
    'event_id', event.event_id,
    'operation_id', event.operation_id,
    'run_id', event.run_id,
    'job_id', event.job_id,
    'target_job_sha256', event.target_job_sha256,
    'target_content_sha256', event.target_content_sha256,
    'reservation_event_id', event.reservation_event_id,
    'action', event.action,
    'outcome', event.outcome,
    'provider_id', event.provider_id,
    'provider_version', event.provider_version,
    'provider_model_sha256', event.provider_model_sha256,
    'model_file_sha256', event.model_file_sha256,
    'runtime_revision_sha256', event.runtime_revision_sha256,
    'policy_compiler_sha256', event.policy_compiler_sha256,
    'local_model_calls', event.local_model_calls,
    'external_model_calls', event.external_model_calls,
    'rejection_code', event.rejection_code,
    'provider_output_sha256', event.provider_output_sha256,
    'validator_packet_sha256', event.validator_packet_sha256,
    'packet_storage_sha256', event.packet_storage_sha256
  )::text, 'UTF8'), 'sha256'), 'hex')
  FROM memory.v5_local_inference_event AS event
  WHERE event.owner_user_id = memory.current_actor_user_id()
    AND event.event_id = p_event_id;
$function$;

CREATE OR REPLACE FUNCTION
  memory.classify_owner_v5_local_terminal_item_v1(
    p_packet_id uuid
  )
RETURNS TABLE(
  packet_id uuid,
  job_id uuid,
  evidence_id uuid,
  source_completion_event_id uuid,
  source_route_event_id uuid,
  source_disposition_id uuid,
  inventory_set text,
  safe_to_transition boolean,
  target_disposition text,
  normalized_reason_code text,
  precedence_rank smallint,
  precedence_code text,
  evidence_content_sha256 text,
  validator_packet_sha256 text,
  packet_storage_sha256 text,
  source_completion_event_sha256 text,
  source_classification_sha256 text,
  reconciliation_basis_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
  packet_row memory.evidence_extraction_packet_v5_local%ROWTYPE;
  job_row memory.evidence_extraction_job%ROWTYPE;
  completion_row memory.v5_local_inference_event%ROWTYPE;
  route_row memory.v5_2_local_packet_route_event%ROWTYPE;
  disposition_row memory.v5_local_packet_disposition%ROWTYPE;
  completion_count integer;
  source_reasons text[];
  has_manual_reason boolean;
  has_source_conflict boolean;
  has_semantic_conflict boolean;
  has_downstream_stage boolean;
  terminal_source boolean;
  terminal_shape boolean;
  empty_shape boolean;
  approved_terminal_reasons boolean;
  selected_inventory_set text;
  selected_safe boolean := false;
  selected_disposition text;
  selected_reason text;
  selected_rank smallint;
  selected_precedence text;
  completion_sha text;
  classification_sha text;
  basis_sha text;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'terminal reconciliation classification requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL OR p_packet_id IS NULL THEN
    RAISE EXCEPTION 'terminal reconciliation owner and packet are required'
      USING ERRCODE = '22023';
  END IF;

  SELECT packet.* INTO packet_row
  FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id = actor
    AND packet.packet_id = p_packet_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'terminal reconciliation packet is absent'
      USING ERRCODE = '23514';
  END IF;

  SELECT job.* INTO job_row
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id = actor
    AND job.job_id = packet_row.job_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'terminal reconciliation job is absent'
      USING ERRCODE = '23514';
  END IF;

  SELECT count(*)::integer INTO completion_count
  FROM memory.v5_local_inference_event AS event
  WHERE event.owner_user_id = actor
    AND event.job_id = packet_row.job_id
    AND event.action = 'completed'
    AND event.outcome = 'accepted'
    AND event.packet_storage_sha256 = packet_row.packet_storage_sha256;
  IF completion_count <> 1 THEN
    RAISE EXCEPTION 'terminal reconciliation completion cardinality is invalid'
      USING ERRCODE = '23514';
  END IF;

  SELECT event.* INTO completion_row
  FROM memory.v5_local_inference_event AS event
  WHERE event.owner_user_id = actor
    AND event.job_id = packet_row.job_id
    AND event.action = 'completed'
    AND event.outcome = 'accepted'
    AND event.packet_storage_sha256 = packet_row.packet_storage_sha256;

  SELECT route.* INTO route_row
  FROM memory.v5_2_local_packet_route_event AS route
  WHERE route.owner_user_id = actor
    AND route.packet_id = packet_row.packet_id;

  SELECT disposition.* INTO disposition_row
  FROM memory.v5_local_packet_disposition AS disposition
  WHERE disposition.owner_user_id = actor
    AND disposition.packet_id = packet_row.packet_id;

  source_reasons := ARRAY(
    SELECT DISTINCT item->>'reason_code'
    FROM jsonb_array_elements(CASE
      WHEN jsonb_typeof(packet_row.normalized_packet->'deferrals') = 'array'
        THEN packet_row.normalized_packet->'deferrals'
      ELSE '[]'::jsonb
    END) AS item
    WHERE item->>'reason_code' IS NOT NULL
    ORDER BY item->>'reason_code'
  )::text[];

  has_manual_reason := source_reasons && ARRAY[
    'ambiguous_transcription',
    'compound_requires_split',
    'entity_resolution_unresolved',
    'mixed_authorship',
    'project_scope_unresolved',
    'sensitive_manual_review',
    'unregistered_predicate'
  ]::text[];
  has_source_conflict :=
    route_row.route = 'manual_review_artifact_ready'
    OR disposition_row.reason_code = 'ambiguous_transcription';
  has_downstream_stage := EXISTS (
    SELECT 1
    FROM memory.relational_stage_batch AS stage
    WHERE stage.owner_user_id = actor
      AND stage.evidence_id = packet_row.evidence_id
  );
  has_semantic_conflict :=
    packet_row.observation_count > 0
    OR packet_row.comparison_hint_count > 0
    OR (
      packet_row.entity_mention_count > 0
      AND packet_row.deferral_count > 0
    )
    OR has_source_conflict
    OR has_downstream_stage;
  terminal_source :=
    route_row.route = 'terminal_no_stage'
    OR disposition_row.reason_code = 'deferral_only_no_stage';
  terminal_shape :=
    packet_row.entity_mention_count = 0
    AND packet_row.observation_count = 0
    AND packet_row.comparison_hint_count = 0
    AND packet_row.deferral_count > 0
    AND NOT packet_row.manual_review_required;
  empty_shape :=
    packet_row.entity_mention_count = 0
    AND packet_row.observation_count = 0
    AND packet_row.comparison_hint_count = 0
    AND packet_row.deferral_count = 0
    AND NOT packet_row.manual_review_required;
  approved_terminal_reasons :=
    cardinality(source_reasons) > 0
    AND source_reasons <@ ARRAY[
      'context_missing',
      'insufficient_evidence',
      'question_only',
      'structured_domain',
      'transient_state'
    ]::text[];

  IF has_semantic_conflict THEN
    selected_inventory_set := 'proposed_observation_or_conflict';
  ELSIF packet_row.manual_review_required
        OR packet_row.entity_mention_count > 0
        OR has_manual_reason
        OR disposition_row.reason_code = 'deferral_only_review_unresolved'
  THEN
    selected_inventory_set := 'genuine_manual_review';
  ELSIF terminal_source
        AND terminal_shape
        AND approved_terminal_reasons
  THEN
    selected_inventory_set := 'terminal_no_stage_verified';
    selected_safe := true;
  ELSIF terminal_shape
        AND approved_terminal_reasons
        AND 'context_missing' = ANY(source_reasons)
  THEN
    selected_inventory_set := 'deferral_only';
    selected_safe := true;
  ELSIF (
    terminal_shape AND approved_terminal_reasons
  ) OR empty_shape THEN
    selected_inventory_set := 'terminal_skipped';
    selected_safe := true;
  ELSE
    selected_inventory_set := 'genuine_manual_review';
  END IF;

  IF selected_safe THEN
    IF 'context_missing' = ANY(source_reasons) THEN
      selected_disposition := 'deferred';
      selected_reason := 'context_missing';
      selected_rank := 10;
      selected_precedence := 'p10_context_missing';
    ELSIF 'question_only' = ANY(source_reasons) THEN
      selected_disposition := 'skipped';
      selected_reason := 'question_only';
      selected_rank := 20;
      selected_precedence := 'p20_question_only';
    ELSIF 'structured_domain' = ANY(source_reasons) THEN
      selected_disposition := 'skipped';
      selected_reason := 'structured_domain';
      selected_rank := 30;
      selected_precedence := 'p30_structured_domain';
    ELSIF 'transient_state' = ANY(source_reasons) THEN
      selected_disposition := 'skipped';
      selected_reason := 'transient_state';
      selected_rank := 40;
      selected_precedence := 'p40_transient_state';
    ELSIF 'insufficient_evidence' = ANY(source_reasons) THEN
      selected_disposition := 'skipped';
      selected_reason := 'insufficient_evidence';
      selected_rank := 50;
      selected_precedence := 'p50_insufficient_evidence';
    ELSIF empty_shape THEN
      selected_disposition := 'skipped';
      selected_reason := 'empty_packet';
      selected_rank := 60;
      selected_precedence := 'p60_empty_packet';
    ELSE
      RAISE EXCEPTION 'terminal reconciliation precedence is incomplete'
        USING ERRCODE = '23514';
    END IF;
  END IF;

  completion_sha :=
    memory.v5_local_event_basis_sha256_v1(completion_row.event_id);
  classification_sha := encode(public.digest(convert_to(jsonb_build_object(
    'packet_id', packet_row.packet_id,
    'job_id', packet_row.job_id,
    'evidence_id', packet_row.evidence_id,
    'packet_storage_sha256', packet_row.packet_storage_sha256,
    'validator_packet_sha256', packet_row.validator_packet_sha256,
    'source_completion_event_id', completion_row.event_id,
    'source_completion_event_sha256', completion_sha,
    'source_route_event_id', route_row.route_event_id,
    'source_route', route_row.route,
    'source_route_reason', route_row.reason_code,
    'source_routing_basis_sha256', route_row.routing_basis_sha256,
    'source_disposition_id', disposition_row.disposition_id,
    'source_disposition_reason', disposition_row.reason_code,
    'source_review_basis_sha256', disposition_row.review_basis_sha256,
    'has_downstream_stage', has_downstream_stage,
    'source_reason_codes', to_jsonb(source_reasons),
    'manual_review_required', packet_row.manual_review_required,
    'entity_mention_count', packet_row.entity_mention_count,
    'observation_count', packet_row.observation_count,
    'comparison_hint_count', packet_row.comparison_hint_count,
    'deferral_count', packet_row.deferral_count,
    'inventory_set', selected_inventory_set,
    'safe_to_transition', selected_safe,
    'target_disposition', selected_disposition,
    'normalized_reason_code', selected_reason,
    'precedence_code', selected_precedence
  )::text, 'UTF8'), 'sha256'), 'hex');
  basis_sha := encode(public.digest(convert_to(jsonb_build_object(
    'source_classification_sha256', classification_sha,
    'policy_version',
      'memory_v1_v5_2_terminal_reconciliation_policy_v1'
  )::text, 'UTF8'), 'sha256'), 'hex');

  RETURN QUERY SELECT
    packet_row.packet_id,
    packet_row.job_id,
    packet_row.evidence_id,
    completion_row.event_id,
    route_row.route_event_id,
    disposition_row.disposition_id,
    selected_inventory_set,
    selected_safe,
    selected_disposition,
    selected_reason,
    selected_rank,
    selected_precedence,
    packet_row.evidence_content_sha256,
    packet_row.validator_packet_sha256,
    packet_row.packet_storage_sha256,
    completion_sha,
    classification_sha,
    basis_sha;
END
$function$;

CREATE OR REPLACE FUNCTION
  memory.plan_owner_v5_local_inference_supersession_v1()
RETURNS TABLE(
  job_id uuid,
  original_event_id uuid,
  superseding_event_id uuid,
  original_event_sha256 text,
  superseding_event_sha256 text,
  supersession_basis_sha256 text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
  WITH actor AS (
    SELECT memory.current_actor_user_id() AS owner_user_id
  ),
  candidates AS (
    SELECT
      old.job_id,
      old.event_id AS original_event_id,
      newer.event_id AS superseding_event_id
    FROM memory.v5_local_inference_event AS old
    JOIN actor ON actor.owner_user_id = old.owner_user_id
    JOIN LATERAL (
      SELECT event.event_id, event.created_at
      FROM memory.v5_local_inference_event AS event
      JOIN memory.v5_local_inference_outcome_event AS outcome
        ON outcome.owner_user_id = event.owner_user_id
       AND outcome.completion_event_id = event.event_id
       AND outcome.job_id = event.job_id
       AND outcome.outcome_class = 'record_terminal'
       AND outcome.disposition = 'deferred'
       AND outcome.normalized_reason_code = 'context_missing'
      WHERE event.owner_user_id = old.owner_user_id
        AND event.job_id = old.job_id
        AND event.action = 'completed'
        AND event.outcome = 'rejected'
        AND event.rejection_code = 'context_missing'
        AND (event.created_at, event.event_id) >
            (old.created_at, old.event_id)
      ORDER BY event.created_at, event.event_id
      LIMIT 1
    ) AS newer ON true
    WHERE actor.owner_user_id IS NOT NULL
      AND old.action = 'completed'
      AND old.outcome = 'rejected'
      AND old.rejection_code = 'uncatalogued_validator_rejection'
      AND NOT EXISTS (
        SELECT 1
        FROM memory.v5_local_inference_outcome_supersession_v1 AS prior
        WHERE prior.owner_user_id = old.owner_user_id
          AND prior.original_event_id = old.event_id
      )
  ),
  hashed AS (
    SELECT
      candidate.*,
      memory.v5_local_event_basis_sha256_v1(
        candidate.original_event_id
      ) AS original_sha,
      memory.v5_local_event_basis_sha256_v1(
        candidate.superseding_event_id
      ) AS superseding_sha
    FROM candidates AS candidate
  )
  SELECT
    hashed.job_id,
    hashed.original_event_id,
    hashed.superseding_event_id,
    hashed.original_sha,
    hashed.superseding_sha,
    encode(public.digest(convert_to(jsonb_build_object(
      'job_id', hashed.job_id,
      'original_event_id', hashed.original_event_id,
      'superseding_event_id', hashed.superseding_event_id,
      'original_event_sha256', hashed.original_sha,
      'superseding_event_sha256', hashed.superseding_sha,
      'policy_version',
        'memory_v1_v5_2_terminal_reconciliation_policy_v1'
    )::text, 'UTF8'), 'sha256'), 'hex')
  FROM hashed
  ORDER BY hashed.original_event_id;
$function$;

CREATE OR REPLACE FUNCTION
  memory.plan_owner_v5_local_terminal_reconciliation_v1(
    p_limit integer DEFAULT 200
  )
RETURNS TABLE(
  packet_id uuid,
  job_id uuid,
  evidence_id uuid,
  source_completion_event_id uuid,
  source_route_event_id uuid,
  source_disposition_id uuid,
  inventory_set text,
  target_disposition text,
  normalized_reason_code text,
  precedence_rank smallint,
  precedence_code text,
  evidence_content_sha256 text,
  validator_packet_sha256 text,
  packet_storage_sha256 text,
  source_completion_event_sha256 text,
  source_classification_sha256 text,
  reconciliation_basis_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'terminal reconciliation plan requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL OR p_limit NOT BETWEEN 1 AND 500 THEN
    RAISE EXCEPTION 'terminal reconciliation plan inputs are invalid'
      USING ERRCODE = '22023';
  END IF;

  RETURN QUERY
  SELECT
    classified.packet_id,
    classified.job_id,
    classified.evidence_id,
    classified.source_completion_event_id,
    classified.source_route_event_id,
    classified.source_disposition_id,
    classified.inventory_set,
    classified.target_disposition,
    classified.normalized_reason_code,
    classified.precedence_rank,
    classified.precedence_code,
    classified.evidence_content_sha256,
    classified.validator_packet_sha256,
    classified.packet_storage_sha256,
    classified.source_completion_event_sha256,
    classified.source_classification_sha256,
    classified.reconciliation_basis_sha256
  FROM memory.evidence_extraction_packet_v5_local AS packet
  CROSS JOIN LATERAL
    memory.classify_owner_v5_local_terminal_item_v1(packet.packet_id)
      AS classified
  JOIN memory.evidence_extraction_job AS job
    ON job.owner_user_id = actor
   AND job.job_id = classified.job_id
  WHERE packet.owner_user_id = actor
    AND job.status = 'review_required'
    AND job.lease_token IS NULL
    AND job.lease_expires_at IS NULL
    AND classified.safe_to_transition
    AND NOT EXISTS (
      SELECT 1
      FROM memory.v5_local_terminal_reconciliation_v1 AS prior
      WHERE prior.owner_user_id = actor
        AND prior.packet_id = classified.packet_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM memory.relational_stage_batch AS stage
      WHERE stage.owner_user_id = actor
        AND stage.evidence_id = classified.evidence_id
    )
  ORDER BY classified.precedence_rank, packet.created_at, packet.packet_id
  LIMIT p_limit;
END
$function$;

CREATE OR REPLACE FUNCTION
  memory.apply_owner_v5_local_inference_supersession_v1(
    p_operation_id uuid,
    p_job_id uuid,
    p_original_event_id uuid,
    p_superseding_event_id uuid,
    p_expected_original_event_sha256 text,
    p_expected_superseding_event_sha256 text,
    p_expected_supersession_basis_sha256 text
  )
RETURNS TABLE(
  supersession_id uuid,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
  planned record;
  replayed memory.v5_local_inference_outcome_supersession_v1%ROWTYPE;
  new_id uuid := gen_random_uuid();
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'terminal outcome supersession requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL OR p_operation_id IS NULL OR p_job_id IS NULL
     OR p_original_event_id IS NULL OR p_superseding_event_id IS NULL
     OR p_expected_original_event_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_superseding_event_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_supersession_basis_sha256 !~ '^[0-9a-f]{64}$'
  THEN
    RAISE EXCEPTION 'terminal outcome supersession inputs are invalid'
      USING ERRCODE = '22023';
  END IF;

  SELECT value.* INTO replayed
  FROM memory.v5_local_inference_outcome_supersession_v1 AS value
  WHERE value.owner_user_id = actor
    AND value.operation_id = p_operation_id;
  IF FOUND THEN
    IF replayed.job_id <> p_job_id
       OR replayed.original_event_id <> p_original_event_id
       OR replayed.superseding_event_id <> p_superseding_event_id
       OR replayed.original_event_sha256 <>
          p_expected_original_event_sha256
       OR replayed.superseding_event_sha256 <>
          p_expected_superseding_event_sha256
       OR replayed.supersession_basis_sha256 <>
          p_expected_supersession_basis_sha256
    THEN
      RAISE EXCEPTION 'terminal outcome supersession replay conflicts'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT replayed.supersession_id, 'replayed'::text;
    RETURN;
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws(
      '|', 'memory_v1_v5_terminal_supersession',
      actor::text, p_original_event_id::text
    ), 0
  ));

  SELECT plan.* INTO planned
  FROM memory.plan_owner_v5_local_inference_supersession_v1() AS plan
  WHERE plan.job_id = p_job_id
    AND plan.original_event_id = p_original_event_id
    AND plan.superseding_event_id = p_superseding_event_id;
  IF NOT FOUND
     OR planned.original_event_sha256 <>
        p_expected_original_event_sha256
     OR planned.superseding_event_sha256 <>
        p_expected_superseding_event_sha256
     OR planned.supersession_basis_sha256 <>
        p_expected_supersession_basis_sha256
  THEN
    RAISE EXCEPTION 'terminal outcome supersession source changed'
      USING ERRCODE = '23514';
  END IF;

  INSERT INTO memory.v5_local_inference_outcome_supersession_v1(
    supersession_id,
    owner_user_id,
    operation_id,
    job_id,
    original_event_id,
    superseding_event_id,
    original_rejection_code,
    superseding_rejection_code,
    original_event_sha256,
    superseding_event_sha256,
    supersession_basis_sha256,
    policy_version
  ) VALUES (
    new_id,
    actor,
    p_operation_id,
    p_job_id,
    p_original_event_id,
    p_superseding_event_id,
    'uncatalogued_validator_rejection',
    'context_missing',
    p_expected_original_event_sha256,
    p_expected_superseding_event_sha256,
    p_expected_supersession_basis_sha256,
    'memory_v1_v5_2_terminal_reconciliation_policy_v1'
  );

  RETURN QUERY SELECT new_id, 'applied'::text;
END
$function$;

CREATE OR REPLACE FUNCTION
  memory.finalize_owner_v5_local_terminal_reconciliation_v1(
    p_operation_id uuid,
    p_packet_id uuid,
    p_expected_reconciliation_basis_sha256 text,
    p_expected_target_disposition text,
    p_expected_reason_code text
  )
RETURNS TABLE(
  reconciliation_id uuid,
  job_id uuid,
  status text,
  disposition text,
  normalized_reason_code text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
  classified record;
  current_job memory.evidence_extraction_job%ROWTYPE;
  replayed memory.v5_local_terminal_reconciliation_v1%ROWTYPE;
  new_id uuid := gen_random_uuid();
  next_result jsonb;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 'terminal packet reconciliation requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL OR p_operation_id IS NULL OR p_packet_id IS NULL
     OR p_expected_reconciliation_basis_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_target_disposition NOT IN ('skipped', 'deferred')
     OR p_expected_reason_code NOT IN (
       'empty_packet',
       'insufficient_evidence',
       'structured_domain',
       'transient_state',
       'question_only',
       'context_missing'
     )
  THEN
    RAISE EXCEPTION 'terminal packet reconciliation inputs are invalid'
      USING ERRCODE = '22023';
  END IF;

  SELECT value.* INTO replayed
  FROM memory.v5_local_terminal_reconciliation_v1 AS value
  WHERE value.owner_user_id = actor
    AND value.operation_id = p_operation_id;
  IF FOUND THEN
    IF replayed.packet_id <> p_packet_id
       OR replayed.reconciliation_basis_sha256 <>
          p_expected_reconciliation_basis_sha256
       OR replayed.target_disposition <> p_expected_target_disposition
       OR replayed.normalized_reason_code <> p_expected_reason_code
    THEN
      RAISE EXCEPTION 'terminal packet reconciliation replay conflicts'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT
      replayed.reconciliation_id,
      replayed.job_id,
      'skipped'::text,
      replayed.target_disposition,
      replayed.normalized_reason_code,
      'replayed'::text;
    RETURN;
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws(
      '|', 'memory_v1_v5_terminal_reconciliation',
      actor::text, p_packet_id::text
    ), 0
  ));

  SELECT classification.* INTO classified
  FROM memory.classify_owner_v5_local_terminal_item_v1(p_packet_id)
    AS classification;
  IF NOT FOUND OR NOT classified.safe_to_transition
     OR classified.reconciliation_basis_sha256 <>
        p_expected_reconciliation_basis_sha256
     OR classified.target_disposition <> p_expected_target_disposition
     OR classified.normalized_reason_code <> p_expected_reason_code
  THEN
    RAISE EXCEPTION 'terminal packet reconciliation classification changed'
      USING ERRCODE = '23514';
  END IF;

  SELECT job.* INTO current_job
  FROM memory.evidence_extraction_job AS job
  WHERE job.owner_user_id = actor
    AND job.job_id = classified.job_id
  FOR UPDATE;
  IF NOT FOUND OR current_job.status <> 'review_required'
     OR current_job.lease_token IS NOT NULL
     OR current_job.lease_expires_at IS NOT NULL
  THEN
    RAISE EXCEPTION 'terminal packet reconciliation job state changed'
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.relational_stage_batch AS stage
    WHERE stage.owner_user_id = actor
      AND stage.evidence_id = classified.evidence_id
  ) THEN
    RAISE EXCEPTION 'terminal packet reconciliation evidence is staged'
      USING ERRCODE = '23514';
  END IF;

  next_result := jsonb_set(
    coalesce(current_job.result, '{}'::jsonb),
    '{final}',
    jsonb_build_object(
      'status', 'skipped',
      'payload', jsonb_build_object(
        'contract_version',
          'memory_v1_v5_2_terminal_reconciliation_v1',
        'outcome_class', 'record_terminal',
        'disposition', classified.target_disposition,
        'reason_code', classified.normalized_reason_code,
        'inventory_set', classified.inventory_set,
        'precedence_code', classified.precedence_code,
        'reconciliation_basis_sha256',
          classified.reconciliation_basis_sha256,
        'write_counts', jsonb_build_object(
          'packets', 0,
          'claims', 0,
          'qdrant', 0,
          'prompt_influence', 0
        )
      )
    ),
    true
  );
  IF pg_column_size(next_result) > 32768 THEN
    RAISE EXCEPTION 'terminal packet reconciliation result exceeds budget'
      USING ERRCODE = '22023';
  END IF;

  INSERT INTO memory.v5_local_terminal_reconciliation_v1(
    reconciliation_id,
    owner_user_id,
    operation_id,
    packet_id,
    job_id,
    evidence_id,
    source_completion_event_id,
    source_route_event_id,
    source_disposition_id,
    inventory_set,
    target_disposition,
    normalized_reason_code,
    precedence_rank,
    precedence_code,
    evidence_content_sha256,
    validator_packet_sha256,
    packet_storage_sha256,
    source_completion_event_sha256,
    source_classification_sha256,
    reconciliation_basis_sha256,
    policy_version
  ) VALUES (
    new_id,
    actor,
    p_operation_id,
    classified.packet_id,
    classified.job_id,
    classified.evidence_id,
    classified.source_completion_event_id,
    classified.source_route_event_id,
    classified.source_disposition_id,
    classified.inventory_set,
    classified.target_disposition,
    classified.normalized_reason_code,
    classified.precedence_rank,
    classified.precedence_code,
    classified.evidence_content_sha256,
    classified.validator_packet_sha256,
    classified.packet_storage_sha256,
    classified.source_completion_event_sha256,
    classified.source_classification_sha256,
    classified.reconciliation_basis_sha256,
    'memory_v1_v5_2_terminal_reconciliation_policy_v1'
  );

  UPDATE memory.evidence_extraction_job AS job
  SET status = 'skipped',
      lease_token = NULL,
      lease_expires_at = NULL,
      last_error = NULL,
      result = next_result
  WHERE job.owner_user_id = actor
    AND job.job_id = classified.job_id;

  INSERT INTO memory.evidence_extraction_event(
    owner_user_id,
    job_id,
    operation_id,
    event_type,
    from_status,
    to_status,
    actor_type,
    actor_ref,
    details
  ) VALUES (
    actor,
    classified.job_id,
    p_operation_id,
    'skipped',
    'review_required',
    'skipped',
    'system',
    'memory_v1_v5_2_terminal_reconciliation_v1',
    jsonb_build_object(
      'reconciliation_id', new_id,
      'packet_id', classified.packet_id,
      'disposition', classified.target_disposition,
      'reason_code', classified.normalized_reason_code,
      'inventory_set', classified.inventory_set,
      'precedence_code', classified.precedence_code,
      'reconciliation_basis_sha256',
        classified.reconciliation_basis_sha256,
      'policy_version',
        'memory_v1_v5_2_terminal_reconciliation_policy_v1'
    )
  );

  RETURN QUERY SELECT
    new_id,
    classified.job_id,
    'skipped'::text,
    classified.target_disposition,
    classified.normalized_reason_code,
    'applied'::text;
END
$function$;

DO $circuit_patch$
DECLARE
  function_oid constant regprocedure :=
    'memory.owner_v5_local_inference_circuit_state_v2(text,text,text,text,text,text,integer,integer)'::regprocedure;
  expected_before constant text :=
    'e207e9376d0b0df19059645be66777aae18e8abb20b565313601b685ff4de899';
  failure_anchor constant text :=
    '    AND classification.circuit_impact' || E'\n' ||
    '    AND (';
  failure_replacement constant text :=
    '    AND classification.circuit_impact' || E'\n' ||
    '    AND NOT EXISTS (' || E'\n' ||
    '      SELECT 1' || E'\n' ||
    '      FROM memory.v5_local_inference_outcome_supersession_v1 AS supersession' || E'\n' ||
    '      WHERE supersession.owner_user_id=actor' || E'\n' ||
    '        AND supersession.original_event_id=completed.event_id' || E'\n' ||
    '    )' || E'\n' ||
    '    AND (';
  immediate_anchor constant text :=
    '      AND classification.immediate_open' || E'\n' ||
    '      AND (';
  immediate_replacement constant text :=
    '      AND classification.immediate_open' || E'\n' ||
    '      AND NOT EXISTS (' || E'\n' ||
    '        SELECT 1' || E'\n' ||
    '        FROM memory.v5_local_inference_outcome_supersession_v1 AS supersession' || E'\n' ||
    '        WHERE supersession.owner_user_id=actor' || E'\n' ||
    '          AND supersession.original_event_id=completed.event_id' || E'\n' ||
    '      )' || E'\n' ||
    '      AND (';
  source text;
  source_sha text;
BEGIN
  SELECT pg_get_functiondef(function_oid) INTO source;
  source_sha := encode(public.digest(
    convert_to(source, 'UTF8'), 'sha256'
  ), 'hex');
  IF source_sha <> expected_before THEN
    RAISE EXCEPTION 'terminal reconciliation circuit baseline changed: %',
      source_sha USING ERRCODE = '23514';
  END IF;
  IF (length(source) - length(replace(source, failure_anchor, '')))
       / length(failure_anchor) <> 1
     OR (length(source) - length(replace(source, immediate_anchor, '')))
       / length(immediate_anchor) <> 1
  THEN
    RAISE EXCEPTION 'terminal reconciliation circuit anchor changed'
      USING ERRCODE = '23514';
  END IF;
  source := replace(source, failure_anchor, failure_replacement);
  source := replace(source, immediate_anchor, immediate_replacement);
  EXECUTE source;
END
$circuit_patch$;

ALTER TABLE memory.v5_local_inference_outcome_supersession_v1
  OWNER TO memory_v5_local_inference_maintainer;
ALTER TABLE memory.v5_local_terminal_reconciliation_v1
  OWNER TO memory_v5_local_inference_maintainer;

ALTER FUNCTION memory.v5_local_event_basis_sha256_v1(uuid)
  OWNER TO memory_v5_local_inference_maintainer;
ALTER FUNCTION memory.classify_owner_v5_local_terminal_item_v1(uuid)
  OWNER TO memory_v5_local_inference_maintainer;
ALTER FUNCTION memory.plan_owner_v5_local_inference_supersession_v1()
  OWNER TO memory_v5_local_inference_maintainer;
ALTER FUNCTION memory.plan_owner_v5_local_terminal_reconciliation_v1(integer)
  OWNER TO memory_v5_local_inference_maintainer;
ALTER FUNCTION memory.apply_owner_v5_local_inference_supersession_v1(
  uuid, uuid, uuid, uuid, text, text, text
) OWNER TO memory_v5_local_inference_maintainer;
ALTER FUNCTION memory.finalize_owner_v5_local_terminal_reconciliation_v1(
  uuid, uuid, text, text, text
) OWNER TO memory_v5_local_inference_maintainer;

REVOKE ALL ON
  memory.v5_local_inference_outcome_supersession_v1,
  memory.v5_local_terminal_reconciliation_v1
FROM PUBLIC, brains_app;

REVOKE ALL ON FUNCTION
  memory.v5_local_event_basis_sha256_v1(uuid),
  memory.classify_owner_v5_local_terminal_item_v1(uuid)
FROM PUBLIC, brains_app, memory_v5_local_inference_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.v5_local_event_basis_sha256_v1(uuid),
  memory.classify_owner_v5_local_terminal_item_v1(uuid)
TO memory_v5_local_inference_maintainer;

REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_local_inference_supersession_v1(),
  memory.plan_owner_v5_local_terminal_reconciliation_v1(integer),
  memory.apply_owner_v5_local_inference_supersession_v1(
    uuid, uuid, uuid, uuid, text, text, text
  ),
  memory.finalize_owner_v5_local_terminal_reconciliation_v1(
    uuid, uuid, text, text, text
  )
FROM PUBLIC, brains_app, memory_v5_local_inference_maintainer;

GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_local_inference_supersession_v1(),
  memory.plan_owner_v5_local_terminal_reconciliation_v1(integer),
  memory.apply_owner_v5_local_inference_supersession_v1(
    uuid, uuid, uuid, uuid, text, text, text
  ),
  memory.finalize_owner_v5_local_terminal_reconciliation_v1(
    uuid, uuid, text, text, text
  )
TO brains_app;

GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_local_inference_supersession_v1()
TO memory_v5_local_inference_maintainer;

GRANT EXECUTE ON FUNCTION
  memory.owner_v5_local_inference_circuit_state_v2(
    text, text, text, text, text, text, integer, integer
  )
TO memory_v5_local_inference_maintainer;

COMMIT;
