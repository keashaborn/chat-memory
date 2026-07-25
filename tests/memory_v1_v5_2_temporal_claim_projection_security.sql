\set ON_ERROR_STOP on
BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage'
     OR to_regprocedure(
       'memory.preflight_projection_temporal_state_v5_2(uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.render_projection_claim_text_temporal_v5_2(uuid)'
     ) IS NULL
     OR (
       SELECT proowner::regrole::text <> 'memory_v5_writer'
           OR NOT prosecdef
       FROM pg_proc
       WHERE oid=to_regprocedure(
         'memory.preflight_projection_temporal_state_v5_2(uuid)'
       )
     )
     OR has_function_privilege(
       'public',
       'memory.preflight_projection_temporal_state_v5_2(uuid)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.preflight_projection_temporal_state_v5_2(uuid)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'temporal projection ownership or ACL boundary failed';
  END IF;
END
$block$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

DO $block$
BEGIN
  IF memory.preflight_projection_temporal_state_v5_2(
       'a0ea633d-96df-4ad8-a0c1-b3f4f84e30cc'
     ) <> 'historical'
     OR memory.preflight_projection_temporal_state_v5_2(
       'c8ce8cd0-e058-4181-ae94-fd6fb1e7c6eb'
     ) <> 'historical'
     OR memory.preflight_projection_temporal_state_v5_2(
       '70d55f38-1e33-418f-8ec6-6bfd2051f4e6'
     ) <> 'current'
     OR memory.preflight_projection_temporal_state_v5_2(
       'bbd94cc7-e9d5-429f-8af1-1a029b119db0'
  ) <> 'current' THEN
    RAISE EXCEPTION 'temporal state classification mismatch';
  END IF;
END
$block$;

SELECT set_config(
  'app.user_id',
  '557ea042-cb82-48f8-9429-472e96c957ef',
  true
);
DO $block$
BEGIN
  PERFORM memory.preflight_projection_temporal_state_v5_2(
    'a0ea633d-96df-4ad8-a0c1-b3f4f84e30cc'
  );
  RAISE EXCEPTION 'cross-owner temporal source was exposed';
EXCEPTION
  WHEN SQLSTATE 'P0002' THEN NULL;
END
$block$;

ROLLBACK;
