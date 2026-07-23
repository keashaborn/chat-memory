BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'voice thread deletion migration must run as sage, current_user=%',
      current_user;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brains_app') THEN
    RAISE EXCEPTION 'brains_app role is required';
  END IF;
  IF to_regclass('memory.assistant_transcript_attestation_v1') IS NULL
     OR to_regclass('memory.final_answer_memory_binding_v1') IS NULL
     OR to_regclass('memory.retrieval_trace') IS NULL
     OR to_regclass('public.telemetry_event') IS NULL THEN
    RAISE EXCEPTION 'response trace and telemetry tables are required';
  END IF;
  IF to_regprocedure(
    'memory.transition_evidence_lifecycle(uuid,uuid,text,text,text,text,jsonb)'
  ) IS NULL THEN
    RAISE EXCEPTION 'Memory V1 evidence lifecycle is required';
  END IF;
END
$block$;

GRANT DELETE
  ON memory.assistant_transcript_attestation_v1,
     memory.final_answer_memory_binding_v1
  TO brains_app;

CREATE INDEX IF NOT EXISTS telemetry_event_actor_thread_idx
  ON public.telemetry_event(actor_user_id,thread_id)
  WHERE actor_user_id IS NOT NULL AND thread_id IS NOT NULL;

COMMENT ON INDEX public.telemetry_event_actor_thread_idx IS
  'Supports owner-scoped thread erasure; telemetry RLS and retention are governed separately.';

COMMIT;
