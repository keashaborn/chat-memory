-- STAGED ONLY. Data-preserving rollback for the exact session authority RPC.

DO $preflight$
DECLARE
  private_object_count integer;
  private_owner text;
  public_owner text;
BEGIN
  IF current_user <> 'postgres' THEN
    RAISE EXCEPTION 'session authority rollback requires postgres';
  END IF;
  IF pg_catalog.to_regprocedure(
       'public.governed_memory_current_session_v1()'
     ) IS NULL
     OR pg_catalog.to_regprocedure(
          'governed_memory_auth_private.current_session_v1()'
  ) IS NULL THEN
    RAISE EXCEPTION 'session authority surface is incomplete';
  END IF;
  SELECT pg_catalog.pg_get_userbyid(procedure.proowner)
    INTO public_owner
  FROM pg_catalog.pg_proc AS procedure
  WHERE procedure.oid = pg_catalog.to_regprocedure(
    'public.governed_memory_current_session_v1()'
  );
  SELECT pg_catalog.pg_get_userbyid(procedure.proowner)
    INTO private_owner
  FROM pg_catalog.pg_proc AS procedure
  WHERE procedure.oid = pg_catalog.to_regprocedure(
    'governed_memory_auth_private.current_session_v1()'
  );
  IF public_owner IS DISTINCT FROM 'postgres'
     OR private_owner IS DISTINCT FROM 'postgres' THEN
    RAISE EXCEPTION 'session authority function owner differs';
  END IF;
  SELECT pg_catalog.count(*)::integer
    INTO private_object_count
  FROM pg_catalog.pg_proc AS procedure
  JOIN pg_catalog.pg_namespace AS namespace
    ON namespace.oid = procedure.pronamespace
  WHERE namespace.nspname = 'governed_memory_auth_private';
  IF private_object_count <> 1 THEN
    RAISE EXCEPTION 'private session authority schema has unexpected objects';
  END IF;
END;
$preflight$;

DROP FUNCTION public.governed_memory_current_session_v1();
DROP FUNCTION governed_memory_auth_private.current_session_v1();
DROP SCHEMA governed_memory_auth_private;
