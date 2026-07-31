BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '180s';

DO $preflight$
BEGIN
  IF session_user <> 'sage' THEN
    RAISE EXCEPTION 'AI Operations inbox migration requires sage'
      USING ERRCODE = '42501';
  END IF;
  IF to_regrole('brains_app') IS NULL THEN
    RAISE EXCEPTION 'brains_app role is required';
  END IF;
  IF to_regclass('trusted_web.retrieval_audit') IS NULL THEN
    RAISE EXCEPTION 'trusted-web retrieval audit is required';
  END IF;
  IF to_regnamespace('ai_operations') IS NOT NULL THEN
    RAISE EXCEPTION 'ai_operations schema already exists';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('ai_operations_store_v1') IS NULL THEN
    CREATE ROLE ai_operations_store_v1
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOBYPASSRLS;
  END IF;
  IF EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = 'ai_operations_store_v1'
      AND (
        rolcanlogin
        OR rolsuper
        OR rolcreatedb
        OR rolcreaterole
        OR rolinherit
        OR rolbypassrls
      )
  ) THEN
    RAISE EXCEPTION 'AI Operations store role is overprivileged';
  END IF;
END
$role$;

CREATE SCHEMA ai_operations AUTHORIZATION ai_operations_store_v1;
REVOKE ALL ON SCHEMA ai_operations FROM PUBLIC;

CREATE TABLE ai_operations.monitor_incident_v1 (
  incident_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  monitor_name text NOT NULL,
  incident_key text NOT NULL,
  state text NOT NULL,
  severity text NOT NULL,
  is_drill boolean NOT NULL DEFAULT false,
  observation_status text NOT NULL,
  first_seen_at timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL,
  acknowledged_at timestamptz,
  acknowledged_by uuid,
  resolved_at timestamptz,
  resolved_by uuid,
  observation_count bigint NOT NULL DEFAULT 1,
  reason_codes text[] NOT NULL DEFAULT ARRAY[]::text[],
  window_hours smallint NOT NULL,
  request_count bigint NOT NULL DEFAULT 0,
  completed_count bigint NOT NULL DEFAULT 0,
  fail_closed_count bigint NOT NULL DEFAULT 0,
  relevance_fail_closed_count bigint NOT NULL DEFAULT 0,
  dependency_failure_count bigint NOT NULL DEFAULT 0,
  fail_closed_rate numeric(8,7) NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT monitor_incident_monitor_name_check CHECK (
    monitor_name ~ '^[a-z][a-z0-9_]{2,63}$'
  ),
  CONSTRAINT monitor_incident_key_check CHECK (
    incident_key ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT monitor_incident_state_check CHECK (
    state IN ('open', 'acknowledged', 'resolved')
  ),
  CONSTRAINT monitor_incident_severity_check CHECK (
    severity IN ('info', 'warning', 'critical', 'test')
  ),
  CONSTRAINT monitor_incident_observation_status_check CHECK (
    observation_status IN ('violated', 'unavailable', 'drill')
  ),
  CONSTRAINT monitor_incident_reason_count_check CHECK (
    cardinality(reason_codes) BETWEEN 0 AND 16
  ),
  CONSTRAINT monitor_incident_window_check CHECK (
    window_hours BETWEEN 1 AND 168
  ),
  CONSTRAINT monitor_incident_counts_check CHECK (
    observation_count >= 1
    AND request_count >= 0
    AND completed_count >= 0
    AND fail_closed_count >= 0
    AND relevance_fail_closed_count >= 0
    AND dependency_failure_count >= 0
  ),
  CONSTRAINT monitor_incident_rate_check CHECK (
    fail_closed_rate BETWEEN 0 AND 1
  ),
  CONSTRAINT monitor_incident_time_order_check CHECK (
    first_seen_at <= last_seen_at
    AND created_at <= updated_at
  ),
  CONSTRAINT monitor_incident_ack_check CHECK (
    (acknowledged_at IS NULL) = (acknowledged_by IS NULL)
  ),
  CONSTRAINT monitor_incident_resolved_check CHECK (
    (state = 'resolved' AND resolved_at IS NOT NULL)
    OR (state <> 'resolved' AND resolved_at IS NULL AND resolved_by IS NULL)
  ),
  CONSTRAINT monitor_incident_drill_check CHECK (
    NOT is_drill OR (state = 'resolved' AND severity = 'test')
  )
);

CREATE UNIQUE INDEX monitor_incident_active_key_uq
  ON ai_operations.monitor_incident_v1 (monitor_name, incident_key)
  WHERE state IN ('open', 'acknowledged');
CREATE INDEX monitor_incident_state_last_seen_idx
  ON ai_operations.monitor_incident_v1 (state, last_seen_at DESC);
CREATE INDEX monitor_incident_severity_last_seen_idx
  ON ai_operations.monitor_incident_v1 (severity, last_seen_at DESC);

CREATE TABLE ai_operations.monitor_incident_event_v1 (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id uuid NOT NULL REFERENCES
    ai_operations.monitor_incident_v1 (incident_id) ON DELETE RESTRICT,
  event_type text NOT NULL,
  observed_at timestamptz NOT NULL,
  actor_user_id uuid,
  status text NOT NULL,
  severity text NOT NULL,
  reason_codes text[] NOT NULL DEFAULT ARRAY[]::text[],
  window_hours smallint NOT NULL,
  request_count bigint NOT NULL DEFAULT 0,
  completed_count bigint NOT NULL DEFAULT 0,
  fail_closed_count bigint NOT NULL DEFAULT 0,
  relevance_fail_closed_count bigint NOT NULL DEFAULT 0,
  dependency_failure_count bigint NOT NULL DEFAULT 0,
  fail_closed_rate numeric(8,7) NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT monitor_event_type_check CHECK (
    event_type IN (
      'observed',
      'repeated',
      'acknowledged',
      'resolved',
      'drill'
    )
  ),
  CONSTRAINT monitor_event_status_check CHECK (
    status IN ('pass', 'violated', 'unavailable', 'drill')
  ),
  CONSTRAINT monitor_event_severity_check CHECK (
    severity IN ('info', 'warning', 'critical', 'test')
  ),
  CONSTRAINT monitor_event_reason_count_check CHECK (
    cardinality(reason_codes) BETWEEN 0 AND 16
  ),
  CONSTRAINT monitor_event_window_check CHECK (
    window_hours BETWEEN 1 AND 168
  ),
  CONSTRAINT monitor_event_counts_check CHECK (
    request_count >= 0
    AND completed_count >= 0
    AND fail_closed_count >= 0
    AND relevance_fail_closed_count >= 0
    AND dependency_failure_count >= 0
  ),
  CONSTRAINT monitor_event_rate_check CHECK (
    fail_closed_rate BETWEEN 0 AND 1
  )
);

CREATE INDEX monitor_event_incident_observed_idx
  ON ai_operations.monitor_incident_event_v1
  (incident_id, observed_at DESC);
CREATE INDEX monitor_event_observed_idx
  ON ai_operations.monitor_incident_event_v1 (observed_at DESC);

ALTER TABLE ai_operations.monitor_incident_v1
  OWNER TO ai_operations_store_v1;
ALTER TABLE ai_operations.monitor_incident_event_v1
  OWNER TO ai_operations_store_v1;
ALTER TABLE ai_operations.monitor_incident_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_operations.monitor_incident_v1
  FORCE ROW LEVEL SECURITY;
ALTER TABLE ai_operations.monitor_incident_event_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_operations.monitor_incident_event_v1
  FORCE ROW LEVEL SECURITY;

CREATE POLICY monitor_incident_store_v1
  ON ai_operations.monitor_incident_v1
  FOR ALL TO ai_operations_store_v1
  USING (true)
  WITH CHECK (true);
CREATE POLICY monitor_incident_event_store_v1
  ON ai_operations.monitor_incident_event_v1
  FOR ALL TO ai_operations_store_v1
  USING (true)
  WITH CHECK (true);

CREATE OR REPLACE FUNCTION ai_operations.reject_monitor_event_mutation_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION 'AI Operations monitor events are append-only'
    USING ERRCODE = '42501';
END
$function$;
ALTER FUNCTION ai_operations.reject_monitor_event_mutation_v1()
  OWNER TO ai_operations_store_v1;

CREATE TRIGGER monitor_event_append_only_v1
BEFORE UPDATE OR DELETE
ON ai_operations.monitor_incident_event_v1
FOR EACH ROW
EXECUTE FUNCTION ai_operations.reject_monitor_event_mutation_v1();

CREATE OR REPLACE FUNCTION ai_operations.record_monitor_observation_v1(
  p_monitor_name text,
  p_status text,
  p_severity text,
  p_is_drill boolean,
  p_reason_codes text[],
  p_window_hours integer,
  p_request_count bigint,
  p_completed_count bigint,
  p_fail_closed_count bigint,
  p_relevance_fail_closed_count bigint,
  p_dependency_failure_count bigint,
  p_fail_closed_rate numeric,
  p_observed_at timestamptz
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  normalized_reasons text[];
  derived_key text;
  target ai_operations.monitor_incident_v1%ROWTYPE;
  resolved ai_operations.monitor_incident_v1%ROWTYPE;
  resolved_count integer := 0;
  action text;
BEGIN
  IF session_user <> 'brains_app'
     OR current_user <> 'ai_operations_store_v1' THEN
    RAISE EXCEPTION 'monitor observation requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  IF p_monitor_name IS NULL
     OR p_monitor_name !~ '^[a-z][a-z0-9_]{2,63}$' THEN
    RAISE EXCEPTION 'invalid monitor name' USING ERRCODE = '22023';
  END IF;
  IF p_status NOT IN ('pass', 'violated', 'unavailable', 'drill') THEN
    RAISE EXCEPTION 'invalid monitor status' USING ERRCODE = '22023';
  END IF;
  IF p_severity NOT IN ('info', 'warning', 'critical', 'test') THEN
    RAISE EXCEPTION 'invalid monitor severity' USING ERRCODE = '22023';
  END IF;
  IF p_window_hours NOT BETWEEN 1 AND 168
     OR p_request_count < 0
     OR p_completed_count < 0
     OR p_fail_closed_count < 0
     OR p_relevance_fail_closed_count < 0
     OR p_dependency_failure_count < 0
     OR p_fail_closed_rate NOT BETWEEN 0 AND 1 THEN
    RAISE EXCEPTION 'invalid monitor aggregate' USING ERRCODE = '22023';
  END IF;
  IF p_observed_at IS NULL
     OR p_observed_at > clock_timestamp() + interval '5 minutes'
     OR p_observed_at < clock_timestamp() - interval '30 days' THEN
    RAISE EXCEPTION 'invalid observation time' USING ERRCODE = '22023';
  END IF;

  SELECT coalesce(array_agg(value ORDER BY value), ARRAY[]::text[])
  INTO normalized_reasons
  FROM (
    SELECT DISTINCT btrim(reason) AS value
    FROM unnest(coalesce(p_reason_codes, ARRAY[]::text[])) AS reason
    WHERE btrim(reason) <> ''
  ) AS normalized;
  IF cardinality(normalized_reasons) > 16
     OR EXISTS (
       SELECT 1
       FROM unnest(normalized_reasons) AS reason
       WHERE reason !~ '^[a-z][a-z0-9_]{1,63}$'
     ) THEN
    RAISE EXCEPTION 'invalid monitor reason codes' USING ERRCODE = '22023';
  END IF;

  IF p_status = 'pass' THEN
    IF p_is_drill OR p_severity <> 'info'
       OR cardinality(normalized_reasons) <> 0 THEN
      RAISE EXCEPTION 'invalid pass observation' USING ERRCODE = '22023';
    END IF;
    FOR resolved IN
      UPDATE ai_operations.monitor_incident_v1
      SET state = 'resolved',
          resolved_at = p_observed_at,
          resolved_by = NULL,
          last_seen_at = p_observed_at,
          updated_at = clock_timestamp()
      WHERE monitor_name = p_monitor_name
        AND state IN ('open', 'acknowledged')
        AND NOT is_drill
      RETURNING *
    LOOP
      resolved_count := resolved_count + 1;
      INSERT INTO ai_operations.monitor_incident_event_v1 (
        incident_id,event_type,observed_at,actor_user_id,status,severity,
        reason_codes,window_hours,request_count,completed_count,
        fail_closed_count,relevance_fail_closed_count,
        dependency_failure_count,fail_closed_rate
      ) VALUES (
        resolved.incident_id,'resolved',p_observed_at,NULL,'pass','info',
        ARRAY[]::text[],p_window_hours,p_request_count,p_completed_count,
        p_fail_closed_count,p_relevance_fail_closed_count,
        p_dependency_failure_count,p_fail_closed_rate
      );
    END LOOP;
    RETURN jsonb_build_object(
      'contract_version','ai_operations_monitor_record_v1',
      'action','resolved',
      'resolved_count',resolved_count
    );
  END IF;

  IF p_status = 'drill' THEN
    IF NOT p_is_drill OR p_severity <> 'test'
       OR normalized_reasons <> ARRAY['synthetic_failure_drill']::text[] THEN
      RAISE EXCEPTION 'invalid drill observation' USING ERRCODE = '22023';
    END IF;
  ELSIF p_is_drill OR p_severity NOT IN ('warning', 'critical')
        OR cardinality(normalized_reasons) = 0 THEN
    RAISE EXCEPTION 'invalid failure observation' USING ERRCODE = '22023';
  END IF;

  derived_key := encode(
    public.digest(
      convert_to(
        p_monitor_name || E'\\n' || p_status || E'\\n'
        || array_to_string(normalized_reasons, ','),
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );

  IF p_status = 'drill' THEN
    INSERT INTO ai_operations.monitor_incident_v1 (
      monitor_name,incident_key,state,severity,is_drill,observation_status,
      first_seen_at,last_seen_at,resolved_at,observation_count,
      reason_codes,window_hours,request_count,completed_count,
      fail_closed_count,relevance_fail_closed_count,
      dependency_failure_count,fail_closed_rate
    ) VALUES (
      p_monitor_name,derived_key,'resolved','test',true,'drill',
      p_observed_at,p_observed_at,p_observed_at,1,
      normalized_reasons,p_window_hours,p_request_count,p_completed_count,
      p_fail_closed_count,p_relevance_fail_closed_count,
      p_dependency_failure_count,p_fail_closed_rate
    ) RETURNING * INTO target;
    action := 'drill_recorded';
  ELSE
    INSERT INTO ai_operations.monitor_incident_v1 (
      monitor_name,incident_key,state,severity,is_drill,observation_status,
      first_seen_at,last_seen_at,observation_count,
      reason_codes,window_hours,request_count,completed_count,
      fail_closed_count,relevance_fail_closed_count,
      dependency_failure_count,fail_closed_rate
    ) VALUES (
      p_monitor_name,derived_key,'open',p_severity,false,p_status,
      p_observed_at,p_observed_at,1,
      normalized_reasons,p_window_hours,p_request_count,p_completed_count,
      p_fail_closed_count,p_relevance_fail_closed_count,
      p_dependency_failure_count,p_fail_closed_rate
    )
    ON CONFLICT (monitor_name, incident_key)
      WHERE state IN ('open', 'acknowledged')
    DO UPDATE SET
      severity = excluded.severity,
      observation_status = excluded.observation_status,
      last_seen_at = excluded.last_seen_at,
      observation_count =
        ai_operations.monitor_incident_v1.observation_count + 1,
      reason_codes = excluded.reason_codes,
      window_hours = excluded.window_hours,
      request_count = excluded.request_count,
      completed_count = excluded.completed_count,
      fail_closed_count = excluded.fail_closed_count,
      relevance_fail_closed_count =
        excluded.relevance_fail_closed_count,
      dependency_failure_count = excluded.dependency_failure_count,
      fail_closed_rate = excluded.fail_closed_rate,
      updated_at = clock_timestamp()
    RETURNING * INTO target;
    action := CASE
      WHEN target.observation_count = 1 THEN 'opened'
      ELSE 'updated'
    END;
  END IF;

  INSERT INTO ai_operations.monitor_incident_event_v1 (
    incident_id,event_type,observed_at,actor_user_id,status,severity,
    reason_codes,window_hours,request_count,completed_count,
    fail_closed_count,relevance_fail_closed_count,
    dependency_failure_count,fail_closed_rate
  ) VALUES (
    target.incident_id,
    CASE
      WHEN p_status = 'drill' THEN 'drill'
      WHEN target.observation_count = 1 THEN 'observed'
      ELSE 'repeated'
    END,
    p_observed_at,NULL,p_status,p_severity,normalized_reasons,
    p_window_hours,p_request_count,p_completed_count,
    p_fail_closed_count,p_relevance_fail_closed_count,
    p_dependency_failure_count,p_fail_closed_rate
  );

  RETURN jsonb_build_object(
    'contract_version','ai_operations_monitor_record_v1',
    'action',action,
    'incident_id',target.incident_id,
    'observation_count',target.observation_count
  );
END
$function$;

CREATE OR REPLACE FUNCTION ai_operations.list_monitor_incidents_v1(
  p_state text DEFAULT NULL,
  p_limit integer DEFAULT 50
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
  payload jsonb;
BEGIN
  IF session_user <> 'brains_app'
     OR current_user <> 'ai_operations_store_v1' THEN
    RAISE EXCEPTION 'AI Operations inspection requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := nullif(current_setting('app.user_id', true), '')::uuid;
  IF actor IS NULL
     OR current_setting('app.ai_operations_capability', true)
        <> 'inspector.view' THEN
    RAISE EXCEPTION 'verified inspector context is required'
      USING ERRCODE = '42501';
  END IF;
  IF p_state IS NOT NULL
     AND p_state NOT IN ('open', 'acknowledged', 'resolved') THEN
    RAISE EXCEPTION 'invalid incident state' USING ERRCODE = '22023';
  END IF;
  IF p_limit NOT BETWEEN 1 AND 100 THEN
    RAISE EXCEPTION 'invalid incident limit' USING ERRCODE = '22023';
  END IF;

  SELECT jsonb_build_object(
    'contract_version','ai_operations_monitor_inbox_v1',
    'items',coalesce(jsonb_agg(item ORDER BY item.last_seen_at DESC),'[]'::jsonb)
  )
  INTO payload
  FROM (
    SELECT
      incident_id,monitor_name,state,severity,is_drill,observation_status,
      first_seen_at,last_seen_at,acknowledged_at,acknowledged_by,
      resolved_at,resolved_by,observation_count,reason_codes,
      window_hours,request_count,completed_count,fail_closed_count,
      relevance_fail_closed_count,dependency_failure_count,
      fail_closed_rate
    FROM ai_operations.monitor_incident_v1
    WHERE p_state IS NULL OR state = p_state
    ORDER BY last_seen_at DESC
    LIMIT p_limit
  ) AS item;
  RETURN payload;
END
$function$;

CREATE OR REPLACE FUNCTION ai_operations.acknowledge_monitor_incident_v1(
  p_incident_id uuid,
  p_actor_user_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
  target ai_operations.monitor_incident_v1%ROWTYPE;
BEGIN
  IF session_user <> 'brains_app'
     OR current_user <> 'ai_operations_store_v1' THEN
    RAISE EXCEPTION 'AI Operations acknowledgement requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := nullif(current_setting('app.user_id', true), '')::uuid;
  IF actor IS NULL OR actor <> p_actor_user_id
     OR current_setting('app.ai_operations_capability', true)
        <> 'incident.manage' THEN
    RAISE EXCEPTION 'verified incident manager context is required'
      USING ERRCODE = '42501';
  END IF;

  SELECT * INTO target
  FROM ai_operations.monitor_incident_v1
  WHERE incident_id = p_incident_id
  FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'monitor incident not found' USING ERRCODE = 'P0002';
  END IF;
  IF target.state = 'resolved' THEN
    RAISE EXCEPTION 'resolved incident cannot be acknowledged'
      USING ERRCODE = '22023';
  END IF;
  IF target.state = 'open' THEN
    UPDATE ai_operations.monitor_incident_v1
    SET state = 'acknowledged',
        acknowledged_at = clock_timestamp(),
        acknowledged_by = actor,
        updated_at = clock_timestamp()
    WHERE incident_id = target.incident_id
    RETURNING * INTO target;
    INSERT INTO ai_operations.monitor_incident_event_v1 (
      incident_id,event_type,observed_at,actor_user_id,status,severity,
      reason_codes,window_hours,request_count,completed_count,
      fail_closed_count,relevance_fail_closed_count,
      dependency_failure_count,fail_closed_rate
    ) VALUES (
      target.incident_id,'acknowledged',clock_timestamp(),actor,
      target.observation_status,
      target.severity,target.reason_codes,target.window_hours,
      target.request_count,target.completed_count,target.fail_closed_count,
      target.relevance_fail_closed_count,target.dependency_failure_count,
      target.fail_closed_rate
    );
  END IF;
  RETURN jsonb_build_object(
    'contract_version','ai_operations_monitor_mutation_v1',
    'action','acknowledged',
    'incident_id',target.incident_id,
    'state',target.state
  );
END
$function$;

CREATE OR REPLACE FUNCTION ai_operations.resolve_monitor_incident_v1(
  p_incident_id uuid,
  p_actor_user_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  actor uuid;
  target ai_operations.monitor_incident_v1%ROWTYPE;
BEGIN
  IF session_user <> 'brains_app'
     OR current_user <> 'ai_operations_store_v1' THEN
    RAISE EXCEPTION 'AI Operations resolution requires brains_app session'
      USING ERRCODE = '42501';
  END IF;
  actor := nullif(current_setting('app.user_id', true), '')::uuid;
  IF actor IS NULL OR actor <> p_actor_user_id
     OR current_setting('app.ai_operations_capability', true)
        <> 'incident.manage' THEN
    RAISE EXCEPTION 'verified incident manager context is required'
      USING ERRCODE = '42501';
  END IF;

  UPDATE ai_operations.monitor_incident_v1
  SET state = 'resolved',
      resolved_at = clock_timestamp(),
      resolved_by = actor,
      updated_at = clock_timestamp()
  WHERE incident_id = p_incident_id
    AND state IN ('open', 'acknowledged')
  RETURNING * INTO target;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'active monitor incident not found'
      USING ERRCODE = 'P0002';
  END IF;
  INSERT INTO ai_operations.monitor_incident_event_v1 (
    incident_id,event_type,observed_at,actor_user_id,status,severity,
    reason_codes,window_hours,request_count,completed_count,
    fail_closed_count,relevance_fail_closed_count,
    dependency_failure_count,fail_closed_rate
  ) VALUES (
    target.incident_id,'resolved',clock_timestamp(),actor,
    target.observation_status,
    target.severity,target.reason_codes,target.window_hours,
    target.request_count,target.completed_count,target.fail_closed_count,
    target.relevance_fail_closed_count,target.dependency_failure_count,
    target.fail_closed_rate
  );
  RETURN jsonb_build_object(
    'contract_version','ai_operations_monitor_mutation_v1',
    'action','resolved',
    'incident_id',target.incident_id,
    'state',target.state
  );
END
$function$;

ALTER FUNCTION ai_operations.record_monitor_observation_v1(
  text,text,text,boolean,text[],integer,bigint,bigint,bigint,bigint,
  bigint,numeric,timestamptz
) OWNER TO ai_operations_store_v1;
ALTER FUNCTION ai_operations.list_monitor_incidents_v1(text,integer)
  OWNER TO ai_operations_store_v1;
ALTER FUNCTION ai_operations.acknowledge_monitor_incident_v1(uuid,uuid)
  OWNER TO ai_operations_store_v1;
ALTER FUNCTION ai_operations.resolve_monitor_incident_v1(uuid,uuid)
  OWNER TO ai_operations_store_v1;

REVOKE ALL ON ALL TABLES IN SCHEMA ai_operations FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ai_operations FROM PUBLIC;
DO $acl$
DECLARE
  role_name text;
BEGIN
  FOREACH role_name IN ARRAY ARRAY['anon','authenticated','service_role']
  LOOP
    IF to_regrole(role_name) IS NOT NULL THEN
      EXECUTE format('REVOKE ALL ON SCHEMA ai_operations FROM %I', role_name);
      EXECUTE format(
        'REVOKE ALL ON ALL TABLES IN SCHEMA ai_operations FROM %I',
        role_name
      );
      EXECUTE format(
        'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ai_operations FROM %I',
        role_name
      );
    END IF;
  END LOOP;
END
$acl$;
REVOKE ALL ON ALL TABLES IN SCHEMA ai_operations FROM brains_app;
GRANT USAGE ON SCHEMA ai_operations TO brains_app;
GRANT EXECUTE ON FUNCTION ai_operations.record_monitor_observation_v1(
  text,text,text,boolean,text[],integer,bigint,bigint,bigint,bigint,
  bigint,numeric,timestamptz
) TO brains_app;
GRANT EXECUTE ON FUNCTION ai_operations.list_monitor_incidents_v1(
  text,integer
) TO brains_app;
GRANT EXECUTE ON FUNCTION ai_operations.acknowledge_monitor_incident_v1(
  uuid,uuid
) TO brains_app;
GRANT EXECUTE ON FUNCTION ai_operations.resolve_monitor_incident_v1(
  uuid,uuid
) TO brains_app;
GRANT EXECUTE ON FUNCTION public.digest(bytea,text)
  TO ai_operations_store_v1;

COMMENT ON SCHEMA ai_operations IS
  'Private operational AI reliability state; not exposed through the Data API.';
COMMENT ON TABLE ai_operations.monitor_incident_v1 IS
  'Metadata-only current and resolved monitor incidents; no prompts, queries, URLs, response bodies, or user content.';
COMMENT ON TABLE ai_operations.monitor_incident_event_v1 IS
  'Append-only metadata history for monitor observations and verified operator actions.';
COMMENT ON FUNCTION ai_operations.record_monitor_observation_v1(
  text,text,text,boolean,text[],integer,bigint,bigint,bigint,bigint,
  bigint,numeric,timestamptz
) IS
  'Server-only bounded monitor writer; opens, updates, resolves, or records a synthetic drill without user content.';
COMMENT ON FUNCTION ai_operations.list_monitor_incidents_v1(text,integer) IS
  'Server-only inspector reader requiring a verified actor and explicit inspector.view context.';

COMMIT;
