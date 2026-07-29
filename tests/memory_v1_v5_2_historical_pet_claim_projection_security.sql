\set ON_ERROR_STOP on
BEGIN;

DO $security$
BEGIN
  IF current_user <> 'sage'
     OR to_regprocedure(
       'memory.render_projection_claim_text_temporal_v5_2(uuid)'
     ) IS NULL
     OR (
       SELECT proowner::regrole::text <> 'memory_v5_writer'
           OR NOT prosecdef
       FROM pg_proc
       WHERE oid=to_regprocedure(
         'memory.render_projection_claim_text_temporal_v5_2(uuid)'
       )
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
    RAISE EXCEPTION
      'historical pet renderer ownership or ACL boundary failed';
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
         'e5939e3f-b732-4f72-b7c4-b60ee7c55244'
       )
     ) <> 'The user formerly had a pet named Keasha von Steffen Haus.'
     OR (
       SELECT payload->>'canonical_text'
       FROM memory.expected_projection_payload_v5_2(
         '978c1972-82d5-4d19-a32f-b45c7f4cfbaa'
       )
     ) <> 'The user formerly had a pet named Max.'
     OR (
       SELECT payload->>'canonical_text'
       FROM memory.expected_projection_payload_v5_2(
         'bc8866ad-95e8-4413-832e-813f601eece6'
       )
     ) <> 'The user formerly had a pet named Neko.'
     OR (
       SELECT payload->>'canonical_text'
       FROM memory.expected_projection_payload_v5_2(
         'cb711085-b49b-4b9c-be3b-c34d2d8456da'
       )
     ) <> 'Max has recorded species dog.' THEN
    RAISE EXCEPTION
      'historical pet or ordinary pet rendering mismatch';
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
    '978c1972-82d5-4d19-a32f-b45c7f4cfbaa'
  );
  RAISE EXCEPTION 'cross-owner historical pet source was exposed';
EXCEPTION
  WHEN SQLSTATE 'P0002' THEN NULL;
END
$isolation$;

ROLLBACK;
