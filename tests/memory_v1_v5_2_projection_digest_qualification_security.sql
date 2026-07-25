BEGIN;

DO $contract$
DECLARE
  definition text;
  qualified_count integer;
  unqualified_count integer;
  owner_name text;
  security_definer boolean;
  config text[];
BEGIN
  SELECT
    pg_get_functiondef(procedure.oid),
    owner.rolname,
    procedure.prosecdef,
    procedure.proconfig
  INTO STRICT definition, owner_name, security_definer, config
  FROM pg_proc AS procedure
  JOIN pg_namespace AS namespace
    ON namespace.oid = procedure.pronamespace
  JOIN pg_roles AS owner
    ON owner.oid = procedure.proowner
  WHERE namespace.nspname = 'memory'
    AND procedure.proname = 'expected_projection_payload_v5_2'
    AND pg_get_function_identity_arguments(procedure.oid) = 'p_observation_id uuid';

  qualified_count :=
    (length(definition) - length(replace(
      definition, 'public.digest(convert_to', ''
    ))) / length('public.digest(convert_to');
  unqualified_count :=
    (length(definition) - length(replace(
      definition, 'digest(convert_to', ''
    ))) / length('digest(convert_to') - qualified_count;
  IF qualified_count <> 2
     OR unqualified_count <> 0
     OR owner_name <> 'memory_v5_writer'
     OR security_definer IS NOT TRUE
     OR config IS DISTINCT FROM ARRAY['search_path=""']::text[] THEN
    RAISE EXCEPTION 'V5.2 projection payload security contract drifted';
  END IF;
  IF EXISTS (
       SELECT 1
       FROM pg_proc AS procedure
       JOIN pg_namespace AS namespace
         ON namespace.oid = procedure.pronamespace
       CROSS JOIN LATERAL aclexplode(coalesce(
         procedure.proacl,
         acldefault('f', procedure.proowner)
       )) AS privilege
       WHERE namespace.nspname = 'memory'
         AND procedure.proname = 'expected_projection_payload_v5_2'
         AND pg_get_function_identity_arguments(procedure.oid)
               = 'p_observation_id uuid'
         AND privilege.grantee = 0
         AND privilege.privilege_type = 'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.expected_projection_payload_v5_2(uuid)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'V5.2 projection payload ACL drifted';
  END IF;
END
$contract$;

SET LOCAL ROLE brains_app;
SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

DO $owner_probe$
DECLARE
  source record;
BEGIN
  SELECT * INTO STRICT source
  FROM memory.expected_projection_payload_v5_2(
    'd3c936dc-01c2-4288-9050-b709afa511d8'::uuid
  );
  IF source.lane <> 'preference'
     OR source.payload->>'kind' <> 'preference'
     OR source.payload->>'preference_class' <> 'life'
     OR source.payload->>'domain' <> 'music'
     OR source.payload->'value'
          <> '{"target":"audiobooks","context":null}'::jsonb
     OR source.payload->>'preference_polarity' <> 'likes'
     OR source.payload->>'stability' <> 'stable' THEN
    RAISE EXCEPTION 'owner-scoped audiobook preference payload drifted';
  END IF;
END
$owner_probe$;

SELECT set_config(
  'app.user_id',
  '557ea042-cb82-48f8-9429-472e96c957ef',
  true
);

DO $cross_owner_probe$
BEGIN
  BEGIN
    PERFORM *
    FROM memory.expected_projection_payload_v5_2(
      'd3c936dc-01c2-4288-9050-b709afa511d8'::uuid
    );
    RAISE EXCEPTION 'cross-owner preference payload was visible';
  EXCEPTION
    WHEN SQLSTATE 'P0002' THEN
      NULL;
  END;
END
$cross_owner_probe$;

ROLLBACK;
