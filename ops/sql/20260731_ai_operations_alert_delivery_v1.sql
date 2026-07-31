BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '180s';

DO $preflight$
BEGIN
  IF session_user <> 'sage' THEN
    RAISE EXCEPTION 'AI Operations alert delivery migration requires sage'
      USING ERRCODE = '42501';
  END IF;
  IF to_regnamespace('ai_operations') IS NULL
     OR to_regclass('ai_operations.monitor_incident_v1') IS NULL
     OR to_regclass('ai_operations.monitor_incident_event_v1') IS NULL THEN
    RAISE EXCEPTION 'AI Operations monitor inbox v1 is required'
      USING ERRCODE = '55000';
  END IF;
  IF to_regrole('ai_operations_store_v1') IS NULL
     OR to_regrole('brains_app') IS NULL THEN
    RAISE EXCEPTION 'AI Operations database roles are required'
      USING ERRCODE = '55000';
  END IF;
END
$preflight$;

CREATE TABLE ai_operations.monitor_alert_delivery_v1 (
  delivery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id uuid NOT NULL REFERENCES
    ai_operations.monitor_incident_v1 (incident_id) ON DELETE RESTRICT,
  source_event_id uuid NOT NULL REFERENCES
    ai_operations.monitor_incident_event_v1 (event_id) ON DELETE RESTRICT,
  channel text NOT NULL DEFAULT 'email',
  state text NOT NULL DEFAULT 'pending',
  attempt_count smallint NOT NULL DEFAULT 0,
  max_attempts smallint NOT NULL DEFAULT 5,
  next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  claimed_at timestamptz,
  claimed_by uuid,
  delivered_at timestamptz,
  provider_message_id text,
  last_error_code text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT monitor_alert_delivery_channel_check CHECK (
    channel = 'email'
  ),
  CONSTRAINT monitor_alert_delivery_state_check CHECK (
    state IN (
      'pending','delivering','retryable_failed',
      'delivered','permanent_failed'
    )
  ),
  CONSTRAINT monitor_alert_delivery_attempt_check CHECK (
    attempt_count BETWEEN 0 AND max_attempts
    AND max_attempts BETWEEN 1 AND 8
  ),
  CONSTRAINT monitor_alert_delivery_claim_check CHECK (
    (state = 'delivering' AND claimed_at IS NOT NULL AND claimed_by IS NOT NULL)
    OR (state <> 'delivering' AND claimed_at IS NULL AND claimed_by IS NULL)
  ),
  CONSTRAINT monitor_alert_delivery_result_check CHECK (
    (
      state = 'delivered'
      AND delivered_at IS NOT NULL
      AND provider_message_id IS NOT NULL
      AND last_error_code IS NULL
    )
    OR (
      state <> 'delivered'
      AND delivered_at IS NULL
      AND provider_message_id IS NULL
    )
  ),
  CONSTRAINT monitor_alert_delivery_provider_id_check CHECK (
    provider_message_id IS NULL
    OR (
      char_length(provider_message_id) BETWEEN 1 AND 256
      AND provider_message_id ~ '^[A-Za-z0-9_-]+$'
    )
  ),
  CONSTRAINT monitor_alert_delivery_error_check CHECK (
    last_error_code IS NULL
    OR (
      char_length(last_error_code) BETWEEN 2 AND 64
      AND last_error_code ~ '^[a-z][a-z0-9_]*$'
    )
  ),
  CONSTRAINT monitor_alert_delivery_time_check CHECK (
    created_at <= updated_at
  ),
  UNIQUE (incident_id, channel),
  UNIQUE (source_event_id, channel)
);

CREATE INDEX monitor_alert_delivery_ready_idx
  ON ai_operations.monitor_alert_delivery_v1
  (next_attempt_at, created_at)
  WHERE state IN ('pending','retryable_failed');
CREATE INDEX monitor_alert_delivery_incident_idx
  ON ai_operations.monitor_alert_delivery_v1
  (incident_id, created_at DESC);

CREATE TABLE ai_operations.monitor_alert_delivery_event_v1 (
  delivery_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  delivery_id uuid NOT NULL REFERENCES
    ai_operations.monitor_alert_delivery_v1 (delivery_id)
    ON DELETE RESTRICT,
  event_type text NOT NULL,
  attempt_count smallint NOT NULL,
  error_code text,
  provider_message_id text,
  occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT monitor_alert_delivery_event_type_check CHECK (
    event_type IN (
      'queued','claimed','retry_scheduled',
      'delivered','permanent_failed'
    )
  ),
  CONSTRAINT monitor_alert_delivery_event_attempt_check CHECK (
    attempt_count BETWEEN 0 AND 8
  ),
  CONSTRAINT monitor_alert_delivery_event_error_check CHECK (
    error_code IS NULL
    OR (
      char_length(error_code) BETWEEN 2 AND 64
      AND error_code ~ '^[a-z][a-z0-9_]*$'
    )
  ),
  CONSTRAINT monitor_alert_delivery_event_provider_check CHECK (
    provider_message_id IS NULL
    OR (
      char_length(provider_message_id) BETWEEN 1 AND 256
      AND provider_message_id ~ '^[A-Za-z0-9_-]+$'
    )
  )
);

CREATE INDEX monitor_alert_delivery_event_delivery_idx
  ON ai_operations.monitor_alert_delivery_event_v1
  (delivery_id, occurred_at DESC);

ALTER TABLE ai_operations.monitor_alert_delivery_v1
  OWNER TO ai_operations_store_v1;
ALTER TABLE ai_operations.monitor_alert_delivery_event_v1
  OWNER TO ai_operations_store_v1;
ALTER TABLE ai_operations.monitor_alert_delivery_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_operations.monitor_alert_delivery_v1
  FORCE ROW LEVEL SECURITY;
ALTER TABLE ai_operations.monitor_alert_delivery_event_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_operations.monitor_alert_delivery_event_v1
  FORCE ROW LEVEL SECURITY;

CREATE POLICY monitor_alert_delivery_store_v1
  ON ai_operations.monitor_alert_delivery_v1
  FOR ALL TO ai_operations_store_v1
  USING (true)
  WITH CHECK (true);
CREATE POLICY monitor_alert_delivery_event_store_v1
  ON ai_operations.monitor_alert_delivery_event_v1
  FOR ALL TO ai_operations_store_v1
  USING (true)
  WITH CHECK (true);

CREATE OR REPLACE FUNCTION ai_operations.reject_monitor_alert_event_mutation_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION 'AI Operations alert delivery events are append-only'
    USING ERRCODE = '42501';
END
$function$;
ALTER FUNCTION ai_operations.reject_monitor_alert_event_mutation_v1()
  OWNER TO ai_operations_store_v1;

CREATE TRIGGER monitor_alert_delivery_event_append_only_v1
BEFORE UPDATE OR DELETE
ON ai_operations.monitor_alert_delivery_event_v1
FOR EACH ROW
EXECUTE FUNCTION ai_operations.reject_monitor_alert_event_mutation_v1();

CREATE OR REPLACE FUNCTION ai_operations.enqueue_monitor_alert_delivery_v1()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  queued ai_operations.monitor_alert_delivery_v1%ROWTYPE;
BEGIN
  IF NEW.event_type <> 'observed'
     OR NEW.status NOT IN ('violated','unavailable')
     OR NEW.severity <> 'critical' THEN
    RETURN NEW;
  END IF;
  INSERT INTO ai_operations.monitor_alert_delivery_v1 (
    incident_id,source_event_id,channel,state
  ) VALUES (
    NEW.incident_id,NEW.event_id,'email','pending'
  )
  ON CONFLICT (incident_id, channel) DO NOTHING
  RETURNING * INTO queued;
  IF FOUND THEN
    INSERT INTO ai_operations.monitor_alert_delivery_event_v1 (
      delivery_id,event_type,attempt_count,occurred_at
    ) VALUES (
      queued.delivery_id,'queued',0,clock_timestamp()
    );
  END IF;
  RETURN NEW;
END
$function$;
ALTER FUNCTION ai_operations.enqueue_monitor_alert_delivery_v1()
  OWNER TO ai_operations_store_v1;

CREATE TRIGGER enqueue_monitor_alert_delivery_v1
AFTER INSERT
ON ai_operations.monitor_incident_event_v1
FOR EACH ROW
EXECUTE FUNCTION ai_operations.enqueue_monitor_alert_delivery_v1();

CREATE OR REPLACE FUNCTION ai_operations.claim_monitor_alert_delivery_v1(
  p_worker_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  configured_worker uuid;
  expired ai_operations.monitor_alert_delivery_v1%ROWTYPE;
  claimed ai_operations.monitor_alert_delivery_v1%ROWTYPE;
  incident ai_operations.monitor_incident_v1%ROWTYPE;
BEGIN
  IF session_user <> 'brains_app'
     OR current_user <> 'ai_operations_store_v1' THEN
    RAISE EXCEPTION 'AI Operations alert claim requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  configured_worker := nullif(
    current_setting('app.ai_operations_alert_worker_id', true),''
  )::uuid;
  IF current_setting('app.ai_operations_alert_delivery', true)
       <> 'authorized'
     OR configured_worker IS NULL
     OR p_worker_id IS NULL
     OR configured_worker <> p_worker_id THEN
    RAISE EXCEPTION 'authorized alert delivery worker is required'
      USING ERRCODE = '42501';
  END IF;

  FOR expired IN
    UPDATE ai_operations.monitor_alert_delivery_v1
    SET state = 'permanent_failed',
        claimed_at = NULL,
        claimed_by = NULL,
        next_attempt_at = clock_timestamp(),
        last_error_code = 'worker_lease_expired',
        updated_at = clock_timestamp()
    WHERE state = 'delivering'
      AND claimed_at < clock_timestamp() - interval '10 minutes'
      AND attempt_count >= max_attempts
    RETURNING *
  LOOP
    INSERT INTO ai_operations.monitor_alert_delivery_event_v1 (
      delivery_id,event_type,attempt_count,error_code,occurred_at
    ) VALUES (
      expired.delivery_id,'permanent_failed',expired.attempt_count,
      expired.last_error_code,clock_timestamp()
    );
  END LOOP;

  WITH candidate AS (
    SELECT delivery_id
    FROM ai_operations.monitor_alert_delivery_v1
    WHERE (
      state IN ('pending','retryable_failed')
      AND next_attempt_at <= clock_timestamp()
    ) OR (
      state = 'delivering'
      AND claimed_at < clock_timestamp() - interval '10 minutes'
      AND attempt_count < max_attempts
    )
    ORDER BY next_attempt_at,created_at,delivery_id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
  )
  UPDATE ai_operations.monitor_alert_delivery_v1 AS delivery
  SET state = 'delivering',
      attempt_count = delivery.attempt_count + 1,
      next_attempt_at = clock_timestamp(),
      claimed_at = clock_timestamp(),
      claimed_by = p_worker_id,
      last_error_code = NULL,
      updated_at = clock_timestamp()
  FROM candidate
  WHERE delivery.delivery_id = candidate.delivery_id
  RETURNING delivery.* INTO claimed;

  IF NOT FOUND THEN
    RETURN jsonb_build_object(
      'contract_version','ai_operations_alert_delivery_claim_v1',
      'status','empty'
    );
  END IF;

  SELECT * INTO STRICT incident
  FROM ai_operations.monitor_incident_v1
  WHERE incident_id = claimed.incident_id;

  INSERT INTO ai_operations.monitor_alert_delivery_event_v1 (
    delivery_id,event_type,attempt_count,occurred_at
  ) VALUES (
    claimed.delivery_id,'claimed',claimed.attempt_count,clock_timestamp()
  );

  RETURN jsonb_build_object(
    'contract_version','ai_operations_alert_delivery_claim_v1',
    'status','claimed',
    'delivery_id',claimed.delivery_id,
    'incident_id',incident.incident_id,
    'monitor_name',incident.monitor_name,
    'severity',incident.severity,
    'observation_status',incident.observation_status,
    'reason_codes',incident.reason_codes,
    'first_seen_at',incident.first_seen_at,
    'last_seen_at',incident.last_seen_at,
    'observation_count',incident.observation_count,
    'window_hours',incident.window_hours,
    'request_count',incident.request_count,
    'completed_count',incident.completed_count,
    'fail_closed_count',incident.fail_closed_count,
    'dependency_failure_count',incident.dependency_failure_count,
    'fail_closed_rate',incident.fail_closed_rate,
    'attempt_count',claimed.attempt_count
  );
END
$function$;

CREATE OR REPLACE FUNCTION ai_operations.complete_monitor_alert_delivery_v1(
  p_delivery_id uuid,
  p_worker_id uuid,
  p_outcome text,
  p_provider_message_id text,
  p_error_code text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  configured_worker uuid;
  target ai_operations.monitor_alert_delivery_v1%ROWTYPE;
  final_outcome text;
  retry_delay interval;
BEGIN
  IF session_user <> 'brains_app'
     OR current_user <> 'ai_operations_store_v1' THEN
    RAISE EXCEPTION 'AI Operations alert completion requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  configured_worker := nullif(
    current_setting('app.ai_operations_alert_worker_id', true),''
  )::uuid;
  IF current_setting('app.ai_operations_alert_delivery', true)
       <> 'authorized'
     OR configured_worker IS NULL
     OR p_worker_id IS NULL
     OR configured_worker <> p_worker_id THEN
    RAISE EXCEPTION 'authorized alert delivery worker is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_outcome NOT IN ('delivered','retryable_failed','permanent_failed') THEN
    RAISE EXCEPTION 'invalid alert delivery outcome'
      USING ERRCODE = '22023';
  END IF;
  IF p_provider_message_id IS NOT NULL
     AND (
       char_length(p_provider_message_id) NOT BETWEEN 1 AND 256
       OR p_provider_message_id !~ '^[A-Za-z0-9_-]+$'
     ) THEN
    RAISE EXCEPTION 'invalid provider message id'
      USING ERRCODE = '22023';
  END IF;
  IF p_error_code IS NOT NULL
     AND (
       char_length(p_error_code) NOT BETWEEN 2 AND 64
       OR p_error_code !~ '^[a-z][a-z0-9_]*$'
     ) THEN
    RAISE EXCEPTION 'invalid alert delivery error code'
      USING ERRCODE = '22023';
  END IF;
  IF (p_outcome = 'delivered')
       <> (p_provider_message_id IS NOT NULL AND p_error_code IS NULL) THEN
    RAISE EXCEPTION 'invalid delivered alert result'
      USING ERRCODE = '22023';
  END IF;
  IF p_outcome <> 'delivered'
     AND (p_provider_message_id IS NOT NULL OR p_error_code IS NULL) THEN
    RAISE EXCEPTION 'invalid failed alert result'
      USING ERRCODE = '22023';
  END IF;

  SELECT * INTO target
  FROM ai_operations.monitor_alert_delivery_v1
  WHERE delivery_id = p_delivery_id
    AND state = 'delivering'
    AND claimed_by = p_worker_id
  FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'claimed alert delivery not found'
      USING ERRCODE = 'P0002';
  END IF;

  IF p_outcome = 'delivered' THEN
    UPDATE ai_operations.monitor_alert_delivery_v1
    SET state = 'delivered',
        claimed_at = NULL,
        claimed_by = NULL,
        delivered_at = clock_timestamp(),
        provider_message_id = p_provider_message_id,
        last_error_code = NULL,
        updated_at = clock_timestamp()
    WHERE delivery_id = target.delivery_id;
    final_outcome := 'delivered';
  ELSIF p_outcome = 'permanent_failed'
        OR target.attempt_count >= target.max_attempts THEN
    UPDATE ai_operations.monitor_alert_delivery_v1
    SET state = 'permanent_failed',
        claimed_at = NULL,
        claimed_by = NULL,
        last_error_code = p_error_code,
        updated_at = clock_timestamp()
    WHERE delivery_id = target.delivery_id;
    final_outcome := 'permanent_failed';
  ELSE
    retry_delay := CASE target.attempt_count
      WHEN 1 THEN interval '1 minute'
      WHEN 2 THEN interval '5 minutes'
      WHEN 3 THEN interval '30 minutes'
      WHEN 4 THEN interval '2 hours'
      ELSE interval '12 hours'
    END;
    UPDATE ai_operations.monitor_alert_delivery_v1
    SET state = 'retryable_failed',
        claimed_at = NULL,
        claimed_by = NULL,
        next_attempt_at = clock_timestamp() + retry_delay,
        last_error_code = p_error_code,
        updated_at = clock_timestamp()
    WHERE delivery_id = target.delivery_id;
    final_outcome := 'retry_scheduled';
  END IF;

  INSERT INTO ai_operations.monitor_alert_delivery_event_v1 (
    delivery_id,event_type,attempt_count,error_code,
    provider_message_id,occurred_at
  ) VALUES (
    target.delivery_id,final_outcome,target.attempt_count,
    p_error_code,p_provider_message_id,clock_timestamp()
  );

  RETURN jsonb_build_object(
    'contract_version','ai_operations_alert_delivery_complete_v1',
    'delivery_id',target.delivery_id,
    'outcome',final_outcome
  );
END
$function$;

ALTER FUNCTION ai_operations.claim_monitor_alert_delivery_v1(uuid)
  OWNER TO ai_operations_store_v1;
ALTER FUNCTION ai_operations.complete_monitor_alert_delivery_v1(
  uuid,uuid,text,text,text
) OWNER TO ai_operations_store_v1;

REVOKE ALL ON ai_operations.monitor_alert_delivery_v1 FROM PUBLIC;
REVOKE ALL ON ai_operations.monitor_alert_delivery_event_v1 FROM PUBLIC;
REVOKE ALL ON FUNCTION ai_operations.claim_monitor_alert_delivery_v1(uuid)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION ai_operations.complete_monitor_alert_delivery_v1(
  uuid,uuid,text,text,text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION ai_operations.enqueue_monitor_alert_delivery_v1()
  FROM PUBLIC;
REVOKE ALL ON FUNCTION ai_operations.reject_monitor_alert_event_mutation_v1()
  FROM PUBLIC;
REVOKE ALL ON ai_operations.monitor_alert_delivery_v1 FROM brains_app;
REVOKE ALL ON ai_operations.monitor_alert_delivery_event_v1 FROM brains_app;
GRANT EXECUTE ON FUNCTION ai_operations.claim_monitor_alert_delivery_v1(uuid)
  TO brains_app;
GRANT EXECUTE ON FUNCTION ai_operations.complete_monitor_alert_delivery_v1(
  uuid,uuid,text,text,text
) TO brains_app;

COMMENT ON TABLE ai_operations.monitor_alert_delivery_v1 IS
  'Private metadata-only email outbox for AI Operations incidents; recipient addresses and message bodies are not stored.';
COMMENT ON TABLE ai_operations.monitor_alert_delivery_event_v1 IS
  'Append-only metadata history for AI Operations alert delivery attempts.';
COMMENT ON FUNCTION ai_operations.claim_monitor_alert_delivery_v1(uuid) IS
  'Server-only bounded claim with lease recovery and no direct table access.';
COMMENT ON FUNCTION ai_operations.complete_monitor_alert_delivery_v1(
  uuid,uuid,text,text,text
) IS
  'Server-only alert completion with bounded retry scheduling.';

COMMIT;
