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
     OR has_table_privilege('brains_app','memory.observation','SELECT') THEN
    RAISE EXCEPTION 'V5.2 entailment-source privilege boundary mismatch';
  END IF;
END
$block$;

ROLLBACK;
