BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 extraction v4 migration must run as sage, current_user=%',
      current_user;
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema='memory'
      AND table_name='consolidation_job'
      AND column_name='pipeline_version'
  ) THEN
    RAISE EXCEPTION 'memory V1 extraction v3 migration is required';
  END IF;
END
$block$;

ALTER TABLE memory.consolidation_job
  ALTER COLUMN pipeline_version SET DEFAULT '20260714_v4';

CREATE OR REPLACE FUNCTION memory.enqueue_chat_log_consolidation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
  queued_job_id uuid;
BEGIN
  IF NEW.owner_user_id IS NULL OR NEW.source <> 'frontend/chat:user' THEN
    RETURN NEW;
  END IF;

  PERFORM set_config('app.user_id', NEW.owner_user_id::text, true);
  INSERT INTO memory.consolidation_job(
    owner_user_id,
    source_system,
    source_external_id,
    source_sha256,
    source_recorded_at,
    pipeline_version,
    status,
    result
  ) VALUES (
    NEW.owner_user_id,
    'public.chat_log',
    NEW.id::text,
    encode(digest(COALESCE(NEW.text, ''), 'sha256'), 'hex'),
    COALESCE(NEW.created_at, clock_timestamp()),
    '20260714_v4',
    'pending',
    jsonb_build_object('capture', 'chat_log_trigger')
  )
  ON CONFLICT (
    owner_user_id,
    source_system,
    source_external_id,
    pipeline_version
  ) DO NOTHING
  RETURNING job_id INTO queued_job_id;

  IF queued_job_id IS NOT NULL THEN
    INSERT INTO memory.consolidation_event(
      owner_user_id, job_id, event_type, from_status, to_status,
      actor_type, actor_ref, details
    ) VALUES (
      NEW.owner_user_id, queued_job_id, 'queued', NULL, 'pending',
      'capture', 'public.chat_log',
      jsonb_build_object('pipeline_version', '20260714_v4')
    );
  END IF;
  RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION memory.enqueue_consolidation_reextract(
  p_pipeline_version text,
  p_sources jsonb
)
RETURNS TABLE(enqueued_count integer, existing_count integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
  actor uuid;
  item jsonb;
  source_id uuid;
  expected_sha256 text;
  actual_sha256 text;
  recorded_at timestamptz;
  queued_job_id uuid;
  seen_ids uuid[] := ARRAY[]::uuid[];
  manifest_sha256 text;
BEGIN
  IF session_user <> 'brains_app' THEN
    RAISE EXCEPTION 're-extraction enqueue requires brains_app'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF p_pipeline_version <> '20260714_v4' THEN
    RAISE EXCEPTION 'unsupported re-extraction pipeline version'
      USING ERRCODE='22023';
  END IF;
  IF jsonb_typeof(p_sources) <> 'array'
     OR jsonb_array_length(p_sources) < 1
     OR jsonb_array_length(p_sources) > 50 THEN
    RAISE EXCEPTION 'p_sources must contain between 1 and 50 rows'
      USING ERRCODE='22023';
  END IF;

  enqueued_count := 0;
  existing_count := 0;
  manifest_sha256 := encode(digest(p_sources::text, 'sha256'), 'hex');

  FOR item IN SELECT value FROM jsonb_array_elements(p_sources)
  LOOP
    IF jsonb_typeof(item) <> 'object'
       OR NOT (item ? 'source_external_id')
       OR NOT (item ? 'source_sha256')
       OR (SELECT count(*) FROM jsonb_object_keys(item)) <> 2 THEN
      RAISE EXCEPTION 'each source row must contain only source_external_id and source_sha256'
        USING ERRCODE='22023';
    END IF;
    source_id := (item->>'source_external_id')::uuid;
    expected_sha256 := lower(item->>'source_sha256');
    IF expected_sha256 !~ '^[0-9a-f]{64}$' THEN
      RAISE EXCEPTION 'invalid source_sha256 for source %', source_id
        USING ERRCODE='22023';
    END IF;
    IF source_id = ANY(seen_ids) THEN
      RAISE EXCEPTION 'duplicate source_external_id in manifest: %', source_id
        USING ERRCODE='22023';
    END IF;
    seen_ids := array_append(seen_ids, source_id);

    SELECT
      encode(digest(COALESCE(log.text, ''), 'sha256'), 'hex'),
      COALESCE(log.created_at, clock_timestamp())
    INTO actual_sha256, recorded_at
    FROM public.chat_log AS log
    WHERE log.id=source_id
      AND log.owner_user_id=actor
      AND log.source='frontend/chat:user';

    IF NOT FOUND THEN
      RAISE EXCEPTION 'owner-scoped source not found: %', source_id
        USING ERRCODE='P0002';
    END IF;
    IF actual_sha256 <> expected_sha256 THEN
      RAISE EXCEPTION 'source hash mismatch for %', source_id
        USING ERRCODE='23514';
    END IF;

    queued_job_id := NULL;
    INSERT INTO memory.consolidation_job(
      owner_user_id,
      source_system,
      source_external_id,
      source_sha256,
      source_recorded_at,
      pipeline_version,
      status,
      result
    ) VALUES (
      actor,
      'public.chat_log',
      source_id::text,
      expected_sha256,
      recorded_at,
      p_pipeline_version,
      'pending',
      jsonb_build_object(
        'capture', 'controlled_reextract',
        'manifest_sha256', manifest_sha256
      )
    )
    ON CONFLICT (
      owner_user_id,
      source_system,
      source_external_id,
      pipeline_version
    ) DO NOTHING
    RETURNING job_id INTO queued_job_id;

    IF queued_job_id IS NULL THEN
      existing_count := existing_count + 1;
    ELSE
      enqueued_count := enqueued_count + 1;
      INSERT INTO memory.consolidation_event(
        owner_user_id, job_id, event_type, from_status, to_status,
        actor_type, actor_ref, details
      ) VALUES (
        actor, queued_job_id, 'queued', NULL, 'pending',
        'admin', p_pipeline_version,
        jsonb_build_object(
          'capture', 'controlled_reextract',
          'manifest_sha256', manifest_sha256
        )
      );
    END IF;
  END LOOP;
  RETURN NEXT;
END
$function$;

ALTER FUNCTION memory.enqueue_chat_log_consolidation() OWNER TO sage;
ALTER FUNCTION memory.enqueue_consolidation_reextract(text, jsonb) OWNER TO sage;
REVOKE ALL ON FUNCTION memory.enqueue_chat_log_consolidation() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.enqueue_consolidation_reextract(text, jsonb)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory.enqueue_consolidation_reextract(text, jsonb)
  TO brains_app;

COMMIT;
