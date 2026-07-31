BEGIN;

DROP FUNCTION IF EXISTS
  memory.plan_owner_contextual_chat_capture_v1(text,integer);

REVOKE SELECT ON memory.evidence_contextual_span_v2,
  memory.evidence_intake_terminal
  FROM memory_context_rebind_maintainer;

COMMIT;
