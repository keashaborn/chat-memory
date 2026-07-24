BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF current_user<>'sage'
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.evidence_extraction_job') IS NULL
     OR to_regclass('memory.evidence') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure('memory.guard_v5_local_inference_append_only()') IS NULL
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('memory_v5_local_disposition_maintainer') IS NULL
     OR to_regrole('memory_v5_local_review_reader') IS NULL
     OR to_regrole('brains_app') IS NULL THEN
    RAISE EXCEPTION 'V5.2 local packet router prerequisites are absent';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_v5_2_local_router_maintainer') IS NULL THEN
    CREATE ROLE memory_v5_2_local_router_maintainer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$role$;

ALTER ROLE memory_v5_2_local_router_maintainer
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

ALTER POLICY owner_isolation ON memory.relational_stage_batch
  TO memory_v5_writer,memory_v5_local_disposition_maintainer,
     memory_v5_local_review_reader,memory_v5_2_local_router_maintainer;

CREATE TABLE IF NOT EXISTS memory.v5_2_local_packet_route_event (
  route_event_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  packet_id uuid NOT NULL,
  job_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  route text NOT NULL CHECK (
    route IN ('terminal_no_stage','manual_review_artifact_ready')
  ),
  reason_code text NOT NULL CHECK (
    reason_code IN (
      'deferral_only_no_stage_v5_2',
      'reviewable_relational_packet_v5_2'
    )
  ),
  routing_basis_sha256 text NOT NULL
    CHECK (routing_basis_sha256 ~ '^[0-9a-f]{64}$'),
  evidence_content_sha256 text NOT NULL
    CHECK (evidence_content_sha256 ~ '^[0-9a-f]{64}$'),
  validator_packet_sha256 text NOT NULL
    CHECK (validator_packet_sha256 ~ '^[0-9a-f]{64}$'),
  packet_storage_sha256 text NOT NULL
    CHECK (packet_storage_sha256 ~ '^[0-9a-f]{64}$'),
  entity_mention_count smallint NOT NULL
    CHECK (entity_mention_count BETWEEN 0 AND 24),
  observation_count smallint NOT NULL
    CHECK (observation_count BETWEEN 0 AND 32),
  comparison_hint_count smallint NOT NULL
    CHECK (comparison_hint_count BETWEEN 0 AND 32),
  deferral_count smallint NOT NULL
    CHECK (deferral_count BETWEEN 0 AND 32),
  source_deferral_reason_codes text[] NOT NULL DEFAULT ARRAY[]::text[],
  review_id uuid,
  request_id uuid,
  review_contract text,
  bundle_contract text,
  review_report_sha256 text,
  stage_bundle_sha256 text,
  repository_commit text,
  auto_link_count smallint,
  manual_review_count smallint,
  deferred_resolution_count smallint,
  rejected_count smallint,
  blocking_code_count smallint,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,packet_id),
  FOREIGN KEY(owner_user_id,packet_id)
    REFERENCES memory.evidence_extraction_packet_v5_local(owner_user_id,packet_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id,job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  CHECK (
    (
      route='terminal_no_stage'
      AND reason_code='deferral_only_no_stage_v5_2'
      AND entity_mention_count=0
      AND observation_count=0
      AND comparison_hint_count=0
      AND deferral_count BETWEEN 1 AND 32
      AND cardinality(source_deferral_reason_codes) BETWEEN 1 AND 4
      AND source_deferral_reason_codes <@ ARRAY[
        'structured_domain','question_only','transient_state',
        'insufficient_evidence'
      ]::text[]
      AND review_id IS NULL
      AND request_id IS NULL
      AND review_contract IS NULL
      AND bundle_contract IS NULL
      AND review_report_sha256 IS NULL
      AND stage_bundle_sha256 IS NULL
      AND repository_commit IS NULL
      AND auto_link_count IS NULL
      AND manual_review_count IS NULL
      AND deferred_resolution_count IS NULL
      AND rejected_count IS NULL
      AND blocking_code_count IS NULL
    ) OR (
      route='manual_review_artifact_ready'
      AND reason_code='reviewable_relational_packet_v5_2'
      AND entity_mention_count+observation_count+comparison_hint_count>=1
      AND cardinality(source_deferral_reason_codes)=0
      AND review_id IS NOT NULL
      AND request_id IS NOT NULL
      AND review_contract='memory_v1_v5_2_local_packet_review_v1'
      AND bundle_contract='memory_v1_v5_2_stage_preflight_v1'
      AND review_report_sha256 ~ '^[0-9a-f]{64}$'
      AND stage_bundle_sha256 ~ '^[0-9a-f]{64}$'
      AND repository_commit ~ '^[0-9a-f]{40}$'
      AND auto_link_count BETWEEN 0 AND 32
      AND manual_review_count BETWEEN 0 AND 32
      AND deferred_resolution_count BETWEEN 0 AND 32
      AND rejected_count BETWEEN 0 AND 32
      AND blocking_code_count BETWEEN 0 AND 32
      AND auto_link_count+manual_review_count+deferred_resolution_count
            +rejected_count=entity_mention_count
    )
  )
);

CREATE INDEX IF NOT EXISTS v5_2_local_packet_route_event_owner_time_idx
  ON memory.v5_2_local_packet_route_event(
    owner_user_id,created_at,route_event_id
  );

ALTER TABLE memory.v5_2_local_packet_route_event OWNER TO sage;
ALTER TABLE memory.v5_2_local_packet_route_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_2_local_packet_route_event FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.v5_2_local_packet_route_event;
CREATE POLICY owner_isolation ON memory.v5_2_local_packet_route_event
  TO memory_v5_2_local_router_maintainer
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());

DROP TRIGGER IF EXISTS v5_2_local_packet_route_event_append_only_guard
  ON memory.v5_2_local_packet_route_event;
CREATE TRIGGER v5_2_local_packet_route_event_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_2_local_packet_route_event
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_2_local_packet_route_v1(
  p_limit integer DEFAULT 1
)
RETURNS TABLE(
  packet_id uuid,
  job_id uuid,
  evidence_id uuid,
  packet_storage_sha256 text,
  route text,
  reason_code text,
  routing_basis_sha256 text,
  source_deferral_reason_codes text[],
  entity_mention_count integer,
  observation_count integer,
  comparison_hint_count integer,
  deferral_count integer
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
  WITH actor AS (
    SELECT memory.current_actor_user_id() AS owner_user_id
  ),
  eligible AS (
    SELECT
      packet.packet_id,
      packet.job_id,
      packet.evidence_id,
      packet.packet_storage_sha256,
      packet.entity_mention_count,
      packet.observation_count,
      packet.comparison_hint_count,
      packet.deferral_count,
      packet.manual_review_required,
      packet.created_at,
      ARRAY(
        SELECT DISTINCT item->>'reason_code'
        FROM jsonb_array_elements(CASE
          WHEN jsonb_typeof(packet.normalized_packet->'deferrals')='array'
            THEN packet.normalized_packet->'deferrals'
          ELSE '[]'::jsonb
        END) AS item
        ORDER BY item->>'reason_code'
      )::text[] AS source_reason_codes,
      (
        packet.entity_mention_count=0
        AND packet.observation_count=0
        AND packet.comparison_hint_count=0
        AND packet.deferral_count>0
        AND NOT packet.manual_review_required
        AND NOT packet.normalized_packet @? '$.deferrals[*] ? (
          @.memory_shape != "none"
          || @.review_required != false
          || (
            @.reason_code != "structured_domain"
            && @.reason_code != "question_only"
            && @.reason_code != "transient_state"
            && @.reason_code != "insufficient_evidence"
          )
        )'
      ) AS terminal_eligible
    FROM memory.evidence_extraction_packet_v5_local AS packet
    JOIN actor ON actor.owner_user_id=packet.owner_user_id
    JOIN memory.evidence_extraction_job AS job
      ON job.owner_user_id=packet.owner_user_id
     AND job.job_id=packet.job_id
     AND job.evidence_id=packet.evidence_id
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=packet.owner_user_id
     AND evidence.evidence_id=packet.evidence_id
    WHERE actor.owner_user_id IS NOT NULL
      AND p_limit BETWEEN 1 AND 25
      AND packet.normalized_packet->>'contract_version'
            ='memory_v1_relational_extraction_v5_2'
      AND packet.normalized_packet->>'predicate_registry_version'
            ='memory_predicate_registry_v5_2'
      AND packet.provider_id='local_llama_cpp'
      AND packet.local_model_calls=1
      AND packet.external_model_calls=0
      AND packet.validator_packet_sha256 ~ '^[0-9a-f]{64}$'
      AND packet.packet_storage_sha256=encode(public.digest(convert_to(
        packet.normalized_packet::text,'UTF8'
      ),'sha256'),'hex')
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'entity_mentions')='array'
        THEN jsonb_array_length(packet.normalized_packet->'entity_mentions')
        ELSE -1
      END=packet.entity_mention_count
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'observations')='array'
        THEN jsonb_array_length(packet.normalized_packet->'observations')
        ELSE -1
      END=packet.observation_count
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'comparison_hints')='array'
        THEN jsonb_array_length(packet.normalized_packet->'comparison_hints')
        ELSE -1
      END=packet.comparison_hint_count
      AND CASE
        WHEN jsonb_typeof(packet.normalized_packet->'deferrals')='array'
        THEN jsonb_array_length(packet.normalized_packet->'deferrals')
        ELSE -1
      END=packet.deferral_count
      AND job.status::text='review_required'
      AND job.route='relational_extraction'
      AND job.lease_token IS NULL
      AND job.lease_expires_at IS NULL
      AND job.last_error IS NULL
      AND evidence.status::text='active'
      AND evidence.content_sha256=packet.evidence_content_sha256
      AND NOT EXISTS (
        SELECT 1
        FROM memory.v5_2_local_packet_route_event AS prior
        WHERE prior.owner_user_id=packet.owner_user_id
          AND prior.packet_id=packet.packet_id
      )
      AND NOT EXISTS (
        SELECT 1
        FROM memory.v5_local_packet_disposition AS prior
        WHERE prior.owner_user_id=packet.owner_user_id
          AND prior.packet_id=packet.packet_id
      )
      AND NOT EXISTS (
        SELECT 1
        FROM memory.relational_stage_batch AS stage
        WHERE stage.owner_user_id=packet.owner_user_id
          AND stage.evidence_id=packet.evidence_id
      )
  ),
  routed AS (
    SELECT
      value.*,
      CASE
        WHEN value.terminal_eligible THEN 'terminal_no_stage'
        WHEN value.entity_mention_count+value.observation_count
               +value.comparison_hint_count>=1
          THEN 'manual_review_artifact_ready'
      END AS selected_route
    FROM eligible AS value
  ),
  bounded AS (
    SELECT *
    FROM routed
    WHERE selected_route IS NOT NULL
    ORDER BY created_at,packet_id
    LIMIT p_limit
  )
  SELECT
    value.packet_id,
    value.job_id,
    value.evidence_id,
    value.packet_storage_sha256,
    value.selected_route,
    CASE value.selected_route
      WHEN 'terminal_no_stage' THEN 'deferral_only_no_stage_v5_2'
      ELSE 'reviewable_relational_packet_v5_2'
    END,
    encode(public.digest(convert_to(jsonb_build_object(
      'packet_storage_sha256',value.packet_storage_sha256,
      'route',value.selected_route,
      'reason_codes',CASE
        WHEN value.selected_route='terminal_no_stage'
          THEN to_jsonb(value.source_reason_codes)
        ELSE '[]'::jsonb
      END,
      'entity_mention_count',value.entity_mention_count,
      'observation_count',value.observation_count,
      'comparison_hint_count',value.comparison_hint_count,
      'deferral_count',value.deferral_count
    )::text,'UTF8'),'sha256'),'hex'),
    CASE
      WHEN value.selected_route='terminal_no_stage'
        THEN value.source_reason_codes
      ELSE ARRAY[]::text[]
    END,
    value.entity_mention_count,
    value.observation_count,
    value.comparison_hint_count,
    value.deferral_count
  FROM bounded AS value;
$function$;

CREATE OR REPLACE FUNCTION memory.finalize_owner_v5_2_terminal_route_v1(
  p_operation_id uuid,
  p_route_event_id uuid,
  p_packet_id uuid,
  p_expected_packet_storage_sha256 text,
  p_expected_routing_basis_sha256 text,
  p_source_deferral_reason_codes text[]
)
RETURNS TABLE(
  route_event_id uuid,
  packet_id uuid,
  route text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  target record;
  replayed memory.v5_2_local_packet_route_event%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5.2 terminal route requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_route_event_id IS NULL OR p_packet_id IS NULL
     OR p_expected_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_routing_basis_sha256 !~ '^[0-9a-f]{64}$'
     OR cardinality(p_source_deferral_reason_codes) NOT BETWEEN 1 AND 4
     OR NOT (
       p_source_deferral_reason_codes <@ ARRAY[
         'structured_domain','question_only','transient_state',
         'insufficient_evidence'
       ]::text[]
     ) THEN
    RAISE EXCEPTION 'V5.2 terminal route inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_2_packet_route',actor::text,p_packet_id::text
  ),0));

  SELECT value.* INTO replayed
  FROM memory.v5_2_local_packet_route_event AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id OR value.packet_id=p_packet_id);
  IF FOUND THEN
    IF replayed.operation_id<>p_operation_id
       OR replayed.route_event_id<>p_route_event_id
       OR replayed.packet_id<>p_packet_id
       OR replayed.route<>'terminal_no_stage'
       OR replayed.reason_code<>'deferral_only_no_stage_v5_2'
       OR replayed.packet_storage_sha256<>p_expected_packet_storage_sha256
       OR replayed.routing_basis_sha256<>p_expected_routing_basis_sha256
       OR replayed.source_deferral_reason_codes
            <>p_source_deferral_reason_codes THEN
      RAISE EXCEPTION 'V5.2 terminal route replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.route_event_id,replayed.packet_id,
      replayed.route,'replayed'::text;
    RETURN;
  END IF;

  SELECT planned.* INTO target
  FROM memory.plan_owner_v5_2_local_packet_route_v1(25) AS planned
  WHERE planned.packet_id=p_packet_id
    AND planned.route='terminal_no_stage';
  IF NOT FOUND
     OR target.packet_storage_sha256<>p_expected_packet_storage_sha256
     OR target.routing_basis_sha256<>p_expected_routing_basis_sha256
     OR target.source_deferral_reason_codes<>p_source_deferral_reason_codes THEN
    RAISE EXCEPTION 'packet is not eligible for V5.2 terminal routing'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_2_local_packet_route_event(
    route_event_id,owner_user_id,operation_id,packet_id,job_id,evidence_id,
    route,reason_code,routing_basis_sha256,evidence_content_sha256,
    validator_packet_sha256,packet_storage_sha256,entity_mention_count,
    observation_count,comparison_hint_count,deferral_count,
    source_deferral_reason_codes
  )
  SELECT
    p_route_event_id,actor,p_operation_id,packet.packet_id,packet.job_id,
    packet.evidence_id,'terminal_no_stage','deferral_only_no_stage_v5_2',
    p_expected_routing_basis_sha256,packet.evidence_content_sha256,
    packet.validator_packet_sha256,packet.packet_storage_sha256,
    packet.entity_mention_count,packet.observation_count,
    packet.comparison_hint_count,packet.deferral_count,
    p_source_deferral_reason_codes
  FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id=actor
    AND packet.packet_id=p_packet_id;

  RETURN QUERY SELECT p_route_event_id,p_packet_id,
    'terminal_no_stage'::text,'applied'::text;
END
$function$;

CREATE OR REPLACE FUNCTION memory.record_owner_v5_2_review_route_v1(
  p_operation_id uuid,
  p_route_event_id uuid,
  p_packet_id uuid,
  p_expected_packet_storage_sha256 text,
  p_expected_routing_basis_sha256 text,
  p_review_id uuid,
  p_request_id uuid,
  p_review_report_sha256 text,
  p_stage_bundle_sha256 text,
  p_repository_commit text,
  p_auto_link_count integer,
  p_manual_review_count integer,
  p_deferred_resolution_count integer,
  p_rejected_count integer,
  p_blocking_code_count integer
)
RETURNS TABLE(
  route_event_id uuid,
  packet_id uuid,
  route text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  target record;
  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  replayed memory.v5_2_local_packet_route_event%ROWTYPE;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'V5.2 review route requires brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'app.user_id actor context is required'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL OR p_route_event_id IS NULL OR p_packet_id IS NULL
     OR p_review_id IS NULL OR p_request_id IS NULL
     OR p_expected_packet_storage_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_routing_basis_sha256 !~ '^[0-9a-f]{64}$'
     OR p_review_report_sha256 !~ '^[0-9a-f]{64}$'
     OR p_stage_bundle_sha256 !~ '^[0-9a-f]{64}$'
     OR p_repository_commit !~ '^[0-9a-f]{40}$'
     OR p_auto_link_count NOT BETWEEN 0 AND 32
     OR p_manual_review_count NOT BETWEEN 0 AND 32
     OR p_deferred_resolution_count NOT BETWEEN 0 AND 32
     OR p_rejected_count NOT BETWEEN 0 AND 32
     OR p_blocking_code_count NOT BETWEEN 0 AND 32 THEN
    RAISE EXCEPTION 'V5.2 review route inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    'memory_v1_v5_2_packet_route',actor::text,p_packet_id::text
  ),0));

  SELECT value.* INTO replayed
  FROM memory.v5_2_local_packet_route_event AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id OR value.packet_id=p_packet_id);
  IF FOUND THEN
    IF replayed.operation_id<>p_operation_id
       OR replayed.route_event_id<>p_route_event_id
       OR replayed.packet_id<>p_packet_id
       OR replayed.route<>'manual_review_artifact_ready'
       OR replayed.reason_code<>'reviewable_relational_packet_v5_2'
       OR replayed.packet_storage_sha256<>p_expected_packet_storage_sha256
       OR replayed.routing_basis_sha256<>p_expected_routing_basis_sha256
       OR replayed.review_id<>p_review_id
       OR replayed.request_id<>p_request_id
       OR replayed.review_report_sha256<>p_review_report_sha256
       OR replayed.stage_bundle_sha256<>p_stage_bundle_sha256
       OR replayed.repository_commit<>p_repository_commit
       OR replayed.auto_link_count<>p_auto_link_count
       OR replayed.manual_review_count<>p_manual_review_count
       OR replayed.deferred_resolution_count<>p_deferred_resolution_count
       OR replayed.rejected_count<>p_rejected_count
       OR replayed.blocking_code_count<>p_blocking_code_count THEN
      RAISE EXCEPTION 'V5.2 review route replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT replayed.route_event_id,replayed.packet_id,
      replayed.route,'replayed'::text;
    RETURN;
  END IF;

  SELECT planned.* INTO target
  FROM memory.plan_owner_v5_2_local_packet_route_v1(25) AS planned
  WHERE planned.packet_id=p_packet_id
    AND planned.route='manual_review_artifact_ready';
  IF NOT FOUND
     OR target.packet_storage_sha256<>p_expected_packet_storage_sha256
     OR target.routing_basis_sha256<>p_expected_routing_basis_sha256 THEN
    RAISE EXCEPTION 'packet is not eligible for V5.2 review routing'
      USING ERRCODE='23514';
  END IF;

  SELECT value.* INTO STRICT packet
  FROM memory.evidence_extraction_packet_v5_local AS value
  WHERE value.owner_user_id=actor
    AND value.packet_id=p_packet_id;
  IF p_auto_link_count+p_manual_review_count+p_deferred_resolution_count
       +p_rejected_count<>packet.entity_mention_count THEN
    RAISE EXCEPTION 'V5.2 review resolution counts do not match packet'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_2_local_packet_route_event(
    route_event_id,owner_user_id,operation_id,packet_id,job_id,evidence_id,
    route,reason_code,routing_basis_sha256,evidence_content_sha256,
    validator_packet_sha256,packet_storage_sha256,entity_mention_count,
    observation_count,comparison_hint_count,deferral_count,
    source_deferral_reason_codes,review_id,request_id,review_contract,
    bundle_contract,review_report_sha256,stage_bundle_sha256,
    repository_commit,auto_link_count,manual_review_count,
    deferred_resolution_count,rejected_count,blocking_code_count
  ) VALUES (
    p_route_event_id,actor,p_operation_id,packet.packet_id,packet.job_id,
    packet.evidence_id,'manual_review_artifact_ready',
    'reviewable_relational_packet_v5_2',p_expected_routing_basis_sha256,
    packet.evidence_content_sha256,packet.validator_packet_sha256,
    packet.packet_storage_sha256,packet.entity_mention_count,
    packet.observation_count,packet.comparison_hint_count,
    packet.deferral_count,ARRAY[]::text[],p_review_id,p_request_id,
    'memory_v1_v5_2_local_packet_review_v1',
    'memory_v1_v5_2_stage_preflight_v1',p_review_report_sha256,
    p_stage_bundle_sha256,p_repository_commit,p_auto_link_count,
    p_manual_review_count,p_deferred_resolution_count,p_rejected_count,
    p_blocking_code_count
  );

  RETURN QUERY SELECT p_route_event_id,p_packet_id,
    'manual_review_artifact_ready'::text,'applied'::text;
END
$function$;

CREATE OR REPLACE FUNCTION memory.guard_v5_2_terminal_evidence_from_stage_v1()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
BEGIN
  IF TG_OP<>'INSERT' OR NEW.owner_user_id IS NULL OR NEW.evidence_id IS NULL THEN
    RAISE EXCEPTION 'V5.2 stage guard accepts bounded inserts only'
      USING ERRCODE='23514';
  END IF;
  IF memory.current_actor_user_id() IS DISTINCT FROM NEW.owner_user_id THEN
    RAISE EXCEPTION 'V5.2 stage guard requires exact owner context'
      USING ERRCODE='42501';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.v5_2_local_packet_route_event AS event
    WHERE event.owner_user_id=NEW.owner_user_id
      AND event.evidence_id=NEW.evidence_id
      AND event.route='terminal_no_stage'
  ) THEN
    RAISE EXCEPTION 'terminal V5.2 evidence cannot enter relational staging'
      USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END
$function$;

GRANT USAGE ON SCHEMA memory TO memory_v5_2_local_router_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_2_local_router_maintainer;
GRANT EXECUTE ON FUNCTION public.digest(bytea,text)
  TO memory_v5_2_local_router_maintainer;
GRANT SELECT,INSERT ON memory.v5_2_local_packet_route_event
  TO memory_v5_2_local_router_maintainer;
GRANT SELECT ON
  memory.evidence_extraction_packet_v5_local,
  memory.evidence_extraction_job,
  memory.evidence,
  memory.relational_stage_batch,
  memory.v5_local_packet_disposition
TO memory_v5_2_local_router_maintainer;

ALTER FUNCTION memory.plan_owner_v5_2_local_packet_route_v1(integer)
  OWNER TO memory_v5_2_local_router_maintainer;
ALTER FUNCTION memory.finalize_owner_v5_2_terminal_route_v1(
  uuid,uuid,uuid,text,text,text[]
) OWNER TO memory_v5_2_local_router_maintainer;
ALTER FUNCTION memory.record_owner_v5_2_review_route_v1(
  uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,
  integer,integer,integer,integer,integer
) OWNER TO memory_v5_2_local_router_maintainer;
ALTER FUNCTION memory.guard_v5_2_terminal_evidence_from_stage_v1()
  OWNER TO memory_v5_2_local_router_maintainer;

REVOKE ALL ON memory.v5_2_local_packet_route_event
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_local_packet_route_v1(integer)
  FROM PUBLIC,brains_app,memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION memory.finalize_owner_v5_2_terminal_route_v1(
  uuid,uuid,uuid,text,text,text[]
) FROM PUBLIC,brains_app,memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION memory.record_owner_v5_2_review_route_v1(
  uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,
  integer,integer,integer,integer,integer
) FROM PUBLIC,brains_app,memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION memory.guard_v5_2_terminal_evidence_from_stage_v1()
  FROM PUBLIC,brains_app,memory_v5_2_local_router_maintainer;

GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_2_local_packet_route_v1(integer)
  TO brains_app;
GRANT EXECUTE ON FUNCTION memory.finalize_owner_v5_2_terminal_route_v1(
  uuid,uuid,uuid,text,text,text[]
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.record_owner_v5_2_review_route_v1(
  uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,
  integer,integer,integer,integer,integer
) TO brains_app;

DROP TRIGGER IF EXISTS v5_2_terminal_evidence_stage_guard
  ON memory.relational_stage_batch;
CREATE TRIGGER v5_2_terminal_evidence_stage_guard
BEFORE INSERT ON memory.relational_stage_batch
FOR EACH ROW EXECUTE FUNCTION
  memory.guard_v5_2_terminal_evidence_from_stage_v1();

COMMENT ON TABLE memory.v5_2_local_packet_route_event
IS 'Append-only owner-scoped V5.2 routing ledger. Stores terminal decisions or sanitized review-artifact hashes only; never stages or promotes memory.';
COMMENT ON FUNCTION memory.plan_owner_v5_2_local_packet_route_v1(integer)
IS 'Plans exact V5.2 packets into a closed terminal-deferral allowlist or the zero-write manual-review artifact lane.';
COMMENT ON FUNCTION memory.finalize_owner_v5_2_terminal_route_v1(
  uuid,uuid,uuid,text,text,text[]
) IS 'Transactionally records one exact owner-scoped content-free V5.2 terminal route with replay verification.';
COMMENT ON FUNCTION memory.record_owner_v5_2_review_route_v1(
  uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,
  integer,integer,integer,integer,integer
) IS 'Transactionally records hashes and counts for one zero-write V5.2 review artifact with replay verification.';

COMMIT;
