BEGIN;

DROP INDEX IF EXISTS public.telemetry_event_actor_thread_idx;

REVOKE DELETE
  ON memory.assistant_transcript_attestation_v1,
     memory.final_answer_memory_binding_v1
  FROM brains_app;

COMMIT;
