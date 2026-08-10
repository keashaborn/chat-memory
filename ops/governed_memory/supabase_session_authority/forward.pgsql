-- STAGED ONLY. Do not apply without separate production authorization.
-- Installs a no-argument, owner-bound session-presence RPC for authenticated
-- requests. The private SECURITY DEFINER function is outside exposed schemas.

DO $preflight$
BEGIN
  IF current_user <> 'postgres' THEN
    RAISE EXCEPTION 'session authority migration requires postgres';
  END IF;
  IF pg_catalog.to_regnamespace('auth') IS NULL
     OR pg_catalog.to_regclass('auth.sessions') IS NULL
     OR pg_catalog.to_regprocedure('auth.uid()') IS NULL
     OR pg_catalog.to_regprocedure('auth.jwt()') IS NULL THEN
    RAISE EXCEPTION 'required Supabase auth surface is absent';
  END IF;
  IF pg_catalog.to_regnamespace('governed_memory_auth_private') IS NOT NULL
     OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_proc AS procedure
          JOIN pg_catalog.pg_namespace AS namespace
            ON namespace.oid = procedure.pronamespace
          WHERE namespace.nspname = 'public'
            AND procedure.proname = 'governed_memory_current_session_v1'
        ) THEN
    RAISE EXCEPTION 'session authority surface already exists';
  END IF;
  IF (
    SELECT pg_catalog.count(*)
    FROM pg_catalog.pg_roles
    WHERE rolname IN ('anon', 'authenticated', 'service_role')
  ) <> 3 THEN
    RAISE EXCEPTION 'required Supabase API roles are absent';
  END IF;
END;
$preflight$;

CREATE SCHEMA governed_memory_auth_private AUTHORIZATION postgres;
REVOKE ALL ON SCHEMA governed_memory_auth_private FROM PUBLIC;

CREATE FUNCTION governed_memory_auth_private.current_session_v1()
RETURNS TABLE(
  owner_user_id uuid,
  session_id uuid,
  session_present boolean
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $function$
DECLARE
  claims jsonb;
  owner_id uuid;
  raw_session_id text;
  checked_session_id uuid;
BEGIN
  claims := auth.jwt();
  owner_id := auth.uid();
  IF owner_id IS NULL
     OR pg_catalog.jsonb_typeof(claims) <> 'object'
     OR claims ->> 'role' <> 'authenticated'
     OR claims -> 'is_anonymous' IS DISTINCT FROM 'false'::jsonb THEN
    RAISE EXCEPTION 'authenticated user session required'
      USING ERRCODE = '42501';
  END IF;
  raw_session_id := claims ->> 'session_id';
  IF raw_session_id IS NULL
     OR raw_session_id !~
       '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
    RAISE EXCEPTION 'canonical session id required'
      USING ERRCODE = '42501';
  END IF;
  checked_session_id := raw_session_id::uuid;
  RETURN QUERY
  SELECT owner_id, checked_session_id, EXISTS (
    SELECT 1
    FROM auth.sessions AS session
    WHERE session.id = checked_session_id
      AND session.user_id = owner_id
  );
END;
$function$;

REVOKE EXECUTE ON FUNCTION
  governed_memory_auth_private.current_session_v1()
FROM PUBLIC, anon, authenticated, service_role;
GRANT USAGE ON SCHEMA governed_memory_auth_private TO authenticated;
GRANT EXECUTE ON FUNCTION
  governed_memory_auth_private.current_session_v1()
TO authenticated;

CREATE FUNCTION public.governed_memory_current_session_v1()
RETURNS TABLE(
  owner_user_id uuid,
  session_id uuid,
  session_present boolean
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path TO ''
AS $function$
  SELECT authority.owner_user_id,
         authority.session_id,
         authority.session_present
  FROM governed_memory_auth_private.current_session_v1() AS authority
$function$;

REVOKE EXECUTE ON FUNCTION public.governed_memory_current_session_v1()
FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.governed_memory_current_session_v1()
TO authenticated;
