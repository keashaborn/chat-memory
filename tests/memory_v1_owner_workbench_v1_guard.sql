\set ON_ERROR_STOP on
BEGIN;
SET LOCAL statement_timeout='120s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'owner memory workbench guard test requires sage';
  END IF;
  IF has_function_privilege(
    'brains_app',
    'memory.guard_owner_packet_feedback_promotion_v1()',
    'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'brains_app can execute the internal promotion guard';
  END IF;
END
$preflight$;

SELECT set_config('app.user_id', :'target_owner', true);

CREATE TEMP TABLE guard_fixture(
  owner_user_id uuid NOT NULL,
  packet_id uuid NOT NULL
);
INSERT INTO guard_fixture VALUES (
  :'target_owner'::uuid,
  :'target_packet'::uuid
);

CREATE TEMP TABLE promotion_probe(
  owner_user_id uuid NOT NULL,
  packet_id uuid NOT NULL
);
CREATE TRIGGER promotion_probe_guard
BEFORE INSERT ON promotion_probe
FOR EACH ROW EXECUTE FUNCTION memory.guard_owner_packet_feedback_promotion_v1();

INSERT INTO memory.owner_packet_feedback_v1(
  feedback_id,
  owner_user_id,
  operation_id,
  packet_id,
  packet_storage_sha256,
  decision,
  diagnostic_note,
  diagnostic_note_sha256,
  supersedes_feedback_id,
  policy_version
) VALUES (
  '44444444-4444-4444-8444-444444444444'::uuid,
  :'target_owner'::uuid,
  '55555555-5555-4555-8555-555555555555'::uuid,
  :'target_packet'::uuid,
  :'target_packet_sha256',
  'correct',
  NULL,
  NULL,
  NULL,
  'memory_owner_packet_feedback_v1'
);

INSERT INTO promotion_probe VALUES (
  :'target_owner'::uuid,
  :'target_packet'::uuid
);

INSERT INTO memory.owner_packet_feedback_v1(
  feedback_id,
  owner_user_id,
  operation_id,
  packet_id,
  packet_storage_sha256,
  decision,
  diagnostic_note,
  diagnostic_note_sha256,
  supersedes_feedback_id,
  policy_version
) VALUES (
  '66666666-6666-4666-8666-666666666666'::uuid,
  :'target_owner'::uuid,
  '77777777-7777-4777-8777-777777777777'::uuid,
  :'target_packet'::uuid,
  :'target_packet_sha256',
  'not_correct',
  NULL,
  NULL,
  '44444444-4444-4444-8444-444444444444'::uuid,
  'memory_owner_packet_feedback_v1'
);

DO $quarantine_guard$
BEGIN
  BEGIN
    INSERT INTO promotion_probe
    SELECT owner_user_id,packet_id FROM guard_fixture;
    RAISE EXCEPTION 'owner-rejected packet passed the staging guard';
  EXCEPTION
    WHEN insufficient_privilege THEN NULL;
  END;
END
$quarantine_guard$;

ROLLBACK;
