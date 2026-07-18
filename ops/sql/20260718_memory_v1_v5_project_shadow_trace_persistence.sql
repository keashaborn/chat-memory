BEGIN;

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5 project shadow trace migration requires sage';
  END IF;
  IF to_regrole('memory_v5_trace_writer') IS NULL
     OR to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure('memory.require_v5_shadow_trace_writer_context()') IS NULL
     OR to_regprocedure('memory.v5_shadow_trace_sha256_valid(text)') IS NULL
     OR to_regprocedure('memory.v5_shadow_rejection_counts_valid(jsonb)') IS NULL THEN
    RAISE EXCEPTION 'V5 project shadow trace prerequisites are missing';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION memory.guard_v5_project_shadow_trace_append_only()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
BEGIN
  RAISE EXCEPTION 'memory.v5_project_shadow_trace_event is append-only'
    USING ERRCODE='42501';
END
$function$;

CREATE TABLE IF NOT EXISTS memory.v5_project_shadow_trace_event (
  owner_user_id uuid NOT NULL,
  trace_event_id uuid NOT NULL DEFAULT gen_random_uuid(),
  trace_version text NOT NULL,
  request_id_sha256 text NOT NULL,
  thread_id_sha256 text NOT NULL,
  request_binding_sha256 text NOT NULL,
  query_sha256 text NOT NULL,
  status text NOT NULL,
  outcome_code text NOT NULL,
  intent text NOT NULL,
  domain text NOT NULL,
  project_scope_sha256 text NOT NULL,
  candidate_set_sha256 text NOT NULL,
  selection_set_sha256 text NOT NULL,
  candidate_count integer NOT NULL,
  selected_count integer NOT NULL,
  token_estimate integer NOT NULL,
  rejected_counts jsonb NOT NULL,
  max_items integer NOT NULL,
  max_tokens integer NOT NULL,
  database_writes integer NOT NULL DEFAULT 0,
  qdrant_reads integer NOT NULL DEFAULT 0,
  qdrant_writes integer NOT NULL DEFAULT 0,
  external_model_calls integer NOT NULL DEFAULT 0,
  prompt_injection boolean NOT NULL DEFAULT false,
  answer_model_exposure boolean NOT NULL DEFAULT false,
  retrieval_activation boolean NOT NULL DEFAULT false,
  trace_manifest_sha256 text NOT NULL,
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (owner_user_id,trace_event_id),
  UNIQUE (owner_user_id,request_id_sha256),
  CHECK (trace_version='memory_v1_v5_project_shadow_trace_v1'),
  CHECK (memory.v5_shadow_trace_sha256_valid(request_id_sha256)),
  CHECK (
    request_id_sha256<>'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
  ),
  CHECK (memory.v5_shadow_trace_sha256_valid(thread_id_sha256)),
  CHECK (memory.v5_shadow_trace_sha256_valid(request_binding_sha256)),
  CHECK (memory.v5_shadow_trace_sha256_valid(query_sha256)),
  CHECK (memory.v5_shadow_trace_sha256_valid(project_scope_sha256)),
  CHECK (memory.v5_shadow_trace_sha256_valid(candidate_set_sha256)),
  CHECK (memory.v5_shadow_trace_sha256_valid(selection_set_sha256)),
  CHECK (memory.v5_shadow_trace_sha256_valid(trace_manifest_sha256)),
  CHECK (status IN ('ok','skipped','error')),
  CHECK (outcome_code ~ '^[a-z0-9_:-]{1,80}$'),
  CHECK (intent ~ '^[a-z0-9_.:-]{1,64}$'),
  CHECK (domain ~ '^[a-z0-9_.:-]{1,64}$'),
  CHECK (max_items BETWEEN 1 AND 8),
  CHECK (max_tokens BETWEEN 1 AND 4000),
  CHECK (candidate_count BETWEEN 0 AND max_items),
  CHECK (selected_count BETWEEN 0 AND candidate_count),
  CHECK (token_estimate BETWEEN 0 AND max_tokens),
  CHECK (memory.v5_shadow_rejection_counts_valid(rejected_counts)),
  CHECK (database_writes=0),
  CHECK (qdrant_reads=0 AND qdrant_writes=0),
  CHECK (external_model_calls=0),
  CHECK (
    NOT prompt_injection
    AND NOT answer_model_exposure
    AND NOT retrieval_activation
  )
);

ALTER FUNCTION memory.guard_v5_project_shadow_trace_append_only()
  OWNER TO memory_v5_trace_writer;
ALTER TABLE memory.v5_project_shadow_trace_event
  OWNER TO memory_v5_trace_writer;

CREATE INDEX IF NOT EXISTS v5_project_shadow_trace_owner_created_idx
  ON memory.v5_project_shadow_trace_event(
    owner_user_id,created_at DESC,trace_event_id
  );

ALTER TABLE memory.v5_project_shadow_trace_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_project_shadow_trace_event FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation ON memory.v5_project_shadow_trace_event;
CREATE POLICY owner_isolation ON memory.v5_project_shadow_trace_event
  FOR ALL TO memory_v5_trace_writer
  USING (owner_user_id=(SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()));

DROP TRIGGER IF EXISTS v5_project_shadow_trace_append_only_guard
  ON memory.v5_project_shadow_trace_event;
CREATE TRIGGER v5_project_shadow_trace_append_only_guard
BEFORE UPDATE OR DELETE ON memory.v5_project_shadow_trace_event
FOR EACH ROW
EXECUTE FUNCTION memory.guard_v5_project_shadow_trace_append_only();

CREATE OR REPLACE FUNCTION memory.record_v5_project_shadow_trace_v1(
  p_trace_version text,
  p_request_id_sha256 text,
  p_thread_id_sha256 text,
  p_request_binding_sha256 text,
  p_query_sha256 text,
  p_status text,
  p_outcome_code text,
  p_intent text,
  p_domain text,
  p_project_scope_sha256 text,
  p_candidate_set_sha256 text,
  p_selection_set_sha256 text,
  p_candidate_count integer,
  p_selected_count integer,
  p_token_estimate integer,
  p_rejected_counts jsonb,
  p_max_items integer,
  p_max_tokens integer
)
RETURNS TABLE(
  trace_event_id uuid,
  outcome text,
  trace_manifest_sha256 text,
  rows_written integer
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  stored memory.v5_project_shadow_trace_event%ROWTYPE;
  event_id uuid;
  manifest_value jsonb;
  manifest_sha text;
  rejected_total integer;
BEGIN
  actor := memory.require_v5_shadow_trace_writer_context();
  IF p_trace_version<>'memory_v1_v5_project_shadow_trace_v1'
     OR NOT memory.v5_shadow_trace_sha256_valid(p_request_id_sha256)
     OR p_request_id_sha256='e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
     OR NOT memory.v5_shadow_trace_sha256_valid(p_thread_id_sha256)
     OR NOT memory.v5_shadow_trace_sha256_valid(p_request_binding_sha256)
     OR NOT memory.v5_shadow_trace_sha256_valid(p_query_sha256)
     OR NOT memory.v5_shadow_trace_sha256_valid(p_project_scope_sha256)
     OR NOT memory.v5_shadow_trace_sha256_valid(p_candidate_set_sha256)
     OR NOT memory.v5_shadow_trace_sha256_valid(p_selection_set_sha256)
     OR p_status NOT IN ('ok','skipped','error')
     OR p_outcome_code IS NULL
     OR p_outcome_code !~ '^[a-z0-9_:-]{1,80}$'
     OR p_intent IS NULL OR p_intent !~ '^[a-z0-9_.:-]{1,64}$'
     OR p_domain IS NULL OR p_domain !~ '^[a-z0-9_.:-]{1,64}$'
     OR p_max_items NOT BETWEEN 1 AND 8
     OR p_max_tokens NOT BETWEEN 1 AND 4000
     OR p_candidate_count NOT BETWEEN 0 AND p_max_items
     OR p_selected_count NOT BETWEEN 0 AND p_candidate_count
     OR p_token_estimate NOT BETWEEN 0 AND p_max_tokens
     OR NOT memory.v5_shadow_rejection_counts_valid(p_rejected_counts) THEN
    RAISE EXCEPTION 'V5 project shadow trace payload is invalid'
      USING ERRCODE='22023';
  END IF;

  SELECT COALESCE(sum((entry.value::text)::integer),0)::integer
  INTO rejected_total
  FROM jsonb_each(p_rejected_counts) AS entry;
  IF rejected_total<>p_candidate_count-p_selected_count THEN
    RAISE EXCEPTION 'V5 project trace rejection counts do not reconcile'
      USING ERRCODE='23514';
  END IF;

  manifest_value := jsonb_build_object(
    'trace_version',p_trace_version,
    'memory_lane','project_knowledge',
    'owner_user_id',actor::text,
    'request_id_sha256',p_request_id_sha256,
    'thread_id_sha256',p_thread_id_sha256,
    'request_binding_sha256',p_request_binding_sha256,
    'query_sha256',p_query_sha256,
    'status',p_status,
    'outcome_code',p_outcome_code,
    'intent',p_intent,
    'domain',p_domain,
    'project_scope_sha256',p_project_scope_sha256,
    'candidate_set_sha256',p_candidate_set_sha256,
    'selection_set_sha256',p_selection_set_sha256,
    'candidate_count',p_candidate_count,
    'selected_count',p_selected_count,
    'token_estimate',p_token_estimate,
    'rejected_counts',p_rejected_counts,
    'max_items',p_max_items,
    'max_tokens',p_max_tokens,
    'database_writes',0,
    'qdrant_reads',0,
    'qdrant_writes',0,
    'external_model_calls',0,
    'prompt_injection',false,
    'answer_model_exposure',false,
    'retrieval_activation',false
  );
  manifest_sha := encode(
    public.digest(convert_to(manifest_value::text,'UTF8'),'sha256'),'hex'
  );

  PERFORM pg_advisory_xact_lock(hashtextextended(
    actor::text||'|v5_project_shadow_trace|'||p_request_id_sha256,0
  ));
  SELECT existing.* INTO stored
  FROM memory.v5_project_shadow_trace_event AS existing
  WHERE existing.owner_user_id=actor
    AND existing.request_id_sha256=p_request_id_sha256;
  IF FOUND THEN
    IF stored.trace_manifest_sha256<>manifest_sha THEN
      RAISE EXCEPTION 'V5 project trace replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      stored.trace_event_id,'replayed',stored.trace_manifest_sha256,0;
    RETURN;
  END IF;

  INSERT INTO memory.v5_project_shadow_trace_event(
    owner_user_id,trace_version,request_id_sha256,thread_id_sha256,
    request_binding_sha256,query_sha256,status,outcome_code,intent,domain,
    project_scope_sha256,candidate_set_sha256,selection_set_sha256,
    candidate_count,selected_count,token_estimate,rejected_counts,
    max_items,max_tokens,trace_manifest_sha256,invoked_by_session
  ) VALUES (
    actor,p_trace_version,p_request_id_sha256,p_thread_id_sha256,
    p_request_binding_sha256,p_query_sha256,p_status,p_outcome_code,
    p_intent,p_domain,p_project_scope_sha256,p_candidate_set_sha256,
    p_selection_set_sha256,p_candidate_count,p_selected_count,
    p_token_estimate,p_rejected_counts,p_max_items,p_max_tokens,
    manifest_sha,session_user
  ) RETURNING v5_project_shadow_trace_event.trace_event_id INTO event_id;

  RETURN QUERY SELECT event_id,'applied',manifest_sha,1;
END
$function$;

ALTER FUNCTION memory.record_v5_project_shadow_trace_v1(
  text,text,text,text,text,text,text,text,text,text,text,text,
  integer,integer,integer,jsonb,integer,integer
) OWNER TO memory_v5_trace_writer;

GRANT USAGE ON SCHEMA memory TO memory_v5_trace_writer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_trace_writer;
GRANT SELECT,INSERT ON memory.v5_project_shadow_trace_event
  TO memory_v5_trace_writer;
GRANT EXECUTE ON FUNCTION memory.guard_v5_project_shadow_trace_append_only()
  TO memory_v5_trace_writer;

REVOKE ALL ON memory.v5_project_shadow_trace_event FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.guard_v5_project_shadow_trace_append_only()
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.record_v5_project_shadow_trace_v1(
  text,text,text,text,text,text,text,text,text,text,text,text,
  integer,integer,integer,jsonb,integer,integer
) FROM PUBLIC,brains_app;
GRANT EXECUTE ON FUNCTION memory.record_v5_project_shadow_trace_v1(
  text,text,text,text,text,text,text,text,text,text,text,text,
  integer,integer,integer,jsonb,integer,integer
) TO brains_app;

COMMIT;
