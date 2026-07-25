BEGIN;

DO $preflight$
DECLARE
  definition text;
  qualified_count integer;
  unqualified_count integer;
BEGIN
  IF current_user <> 'sage'
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('brains_app') IS NULL
     OR to_regprocedure(
       'memory.expected_projection_payload_v5_2(uuid)'
     ) IS NULL
     OR to_regprocedure('public.digest(bytea,text)') IS NULL THEN
    RAISE EXCEPTION
      'V5.2 projection digest-qualification prerequisites are absent';
  END IF;

  SELECT pg_get_functiondef(
    'memory.expected_projection_payload_v5_2(uuid)'::regprocedure
  )
  INTO STRICT definition;

  qualified_count :=
    (length(definition) - length(replace(
      definition, 'public.digest(convert_to', ''
    ))) / length('public.digest(convert_to');
  unqualified_count :=
    (length(definition) - length(replace(
      definition, 'digest(convert_to', ''
    ))) / length('digest(convert_to') - qualified_count;

  IF qualified_count = 2 AND unqualified_count = 0 THEN
    RETURN;
  END IF;
  IF qualified_count <> 0 OR unqualified_count <> 2 THEN
    RAISE EXCEPTION
      'V5.2 projection payload digest call sites are outside the expected baseline';
  END IF;

  definition := replace(
    definition,
    'digest(convert_to',
    'public.digest(convert_to'
  );
  EXECUTE definition;

  SELECT pg_get_functiondef(
    'memory.expected_projection_payload_v5_2(uuid)'::regprocedure
  )
  INTO STRICT definition;
  qualified_count :=
    (length(definition) - length(replace(
      definition, 'public.digest(convert_to', ''
    ))) / length('public.digest(convert_to');
  unqualified_count :=
    (length(definition) - length(replace(
      definition, 'digest(convert_to', ''
    ))) / length('digest(convert_to') - qualified_count;
  IF qualified_count <> 2 OR unqualified_count <> 0 THEN
    RAISE EXCEPTION 'V5.2 projection digest qualification failed';
  END IF;
END
$preflight$;

ALTER FUNCTION memory.expected_projection_payload_v5_2(uuid)
  OWNER TO memory_v5_writer;
REVOKE ALL ON FUNCTION memory.expected_projection_payload_v5_2(uuid)
  FROM PUBLIC, brains_app;
GRANT EXECUTE ON FUNCTION memory.expected_projection_payload_v5_2(uuid)
  TO brains_app;

COMMIT;
