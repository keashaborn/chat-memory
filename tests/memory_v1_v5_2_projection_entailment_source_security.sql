\set ON_ERROR_STOP on
BEGIN;

DO $block$
DECLARE
  function_owner text;
  function_security_definer boolean;
  function_volatility "char";
BEGIN
  SELECT
    pg_get_userbyid(procedure.proowner),
    procedure.prosecdef,
    procedure.provolatile
  INTO function_owner,function_security_definer,function_volatility
  FROM pg_proc AS procedure
  WHERE procedure.oid=
    'memory.preflight_projection_entailment_source_v5_2(uuid)'::regprocedure;

  IF function_owner<>'memory_v5_writer'
     OR NOT function_security_definer
     OR function_volatility<>'s'
     OR has_function_privilege(
       'public',
       'memory.preflight_projection_entailment_source_v5_2(uuid)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.preflight_projection_entailment_source_v5_2(uuid)',
       'EXECUTE'
     )
     OR has_table_privilege('brains_app','memory.observation','SELECT')
     OR has_table_privilege('brains_app','memory.evidence','SELECT') THEN
    RAISE EXCEPTION 'V5.2 entailment-source privilege boundary mismatch';
  END IF;
END
$block$;

SET LOCAL ROLE brains_app;
SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

DO $block$
DECLARE
  source record;
BEGIN
  SELECT * INTO STRICT source
  FROM memory.preflight_projection_entailment_source_v5_2(
    '560261e2-7ac6-435d-bcb6-934315b78472'::uuid
  );
  IF source.observation_ref<>'o00'
     OR source.evidence_id<>
       '22bd0732-3539-4180-8f89-8f84114131c0'::uuid
     OR jsonb_array_length(source.source_spans)<>1 THEN
    RAISE EXCEPTION 'owner-self V5.2 entailment source drifted';
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
  PERFORM *
  FROM memory.preflight_projection_entailment_source_v5_2(
    '560261e2-7ac6-435d-bcb6-934315b78472'::uuid
  );
  RAISE EXCEPTION 'cross-owner V5.2 entailment source unexpectedly passed';
EXCEPTION
  WHEN no_data_found THEN NULL;
END
$block$;

ROLLBACK;
