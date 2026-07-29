\set ON_ERROR_STOP on
BEGIN;

DO $security$
BEGIN
  IF current_user <> 'sage'
     OR (
       SELECT proowner::regrole::text <> 'memory_v5_writer'
           OR NOT prosecdef
       FROM pg_proc
       WHERE oid='memory.render_projection_claim_text_temporal_v5_2(uuid)'::regprocedure
     )
     OR has_function_privilege(
       'public',
       'memory.render_projection_claim_text_temporal_v5_2(uuid)',
       'EXECUTE'
     )
     OR has_function_privilege(
       'brains_app',
       'memory.render_projection_claim_text_temporal_v5_2(uuid)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'identity renderer ownership or ACL boundary failed';
  END IF;
END
$security$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

DO $semantics$
BEGIN
  IF (
       SELECT payload->>'canonical_text'
       FROM memory.expected_projection_payload_v5_2(
         '917ab793-6f03-4af4-847b-c87f5632fa91'
       )
     ) <> 'This animal''s name is Dahlia.'
     OR (
       SELECT payload->>'canonical_text'
       FROM memory.expected_projection_payload_v5_2(
         'c0194481-bed5-438f-9407-07e398f14e50'
       )
     ) <> 'This animal''s name is Keasha von Steffen Haus.'
     OR (
       SELECT payload->>'canonical_text'
       FROM memory.expected_projection_payload_v5_2(
         '5261da41-f863-42cd-8e3f-6e947f9743f2'
       )
     ) <> 'This animal''s canonical name is Neko.'
     OR (
       SELECT payload->>'canonical_text'
       FROM memory.expected_projection_payload_v5_2(
         '14e21c6b-1728-439b-9613-7d9b933d33b8'
       )
     ) <> 'Helsing died.' THEN
    RAISE EXCEPTION 'identity-name or unchanged renderer output mismatch';
  END IF;
END
$semantics$;

SELECT set_config(
  'app.user_id',
  '557ea042-cb82-48f8-9429-472e96c957ef',
  true
);
DO $isolation$
BEGIN
  PERFORM *
  FROM memory.expected_projection_payload_v5_2(
    '917ab793-6f03-4af4-847b-c87f5632fa91'
  );
  RAISE EXCEPTION 'cross-owner identity-name source was exposed';
EXCEPTION
  WHEN SQLSTATE 'P0002' THEN NULL;
END
$isolation$;

ROLLBACK;
