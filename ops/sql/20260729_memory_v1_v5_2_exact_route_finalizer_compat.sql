BEGIN;

DO $migration$
DECLARE
  ddl text;
  actual_sha256 text;
  needle text;
  replacement text;
BEGIN
  SELECT pg_get_functiondef(
    'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'::regprocedure
  ) INTO ddl;
  actual_sha256 := encode(
    public.digest(convert_to(ddl, 'UTF8'), 'sha256'),
    'hex'
  );
  IF actual_sha256 <> '190e99d80d0b5d7ce6d742415384103b19d1dd68a20199a47b0ffcf772c7209b' THEN
    RAISE EXCEPTION 'exact packet planner definition drifted';
  END IF;

  needle := $needle$      packet.manual_review_required,
      ARRAY($needle$;
  replacement := $replacement$      packet.manual_review_required,
      packet.normalized_packet,
      ARRAY($replacement$;
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'exact packet planner payload insertion point drifted';
  END IF;
  ddl := replace(ddl, needle, replacement);

  needle := $needle$        WHEN value.entity_mention_count+value.observation_count
               +value.comparison_hint_count>=1
          THEN 'manual_review_artifact_ready'$needle$;
  replacement := $replacement$        WHEN value.entity_mention_count+value.observation_count
               +value.comparison_hint_count>=1
          OR (
            value.manual_review_required
            AND value.normalized_packet @? '$.deferrals[*] ? (
              @.review_required == true
              && @.reason_code != "structured_domain"
              && @.reason_code != "question_only"
              && @.reason_code != "transient_state"
              && @.reason_code != "insufficient_evidence"
              && @.reason_code != "entity_resolution_unresolved"
            )'
          )
          THEN 'manual_review_artifact_ready'$replacement$;
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'exact packet planner review branch drifted';
  END IF;
  EXECUTE replace(ddl, needle, replacement);

  SELECT pg_get_functiondef(
    'memory.plan_owner_v5_2_zero_atom_deferral_route_v1(integer)'::regprocedure
  ) INTO ddl;
  actual_sha256 := encode(
    public.digest(convert_to(ddl, 'UTF8'), 'sha256'),
    'hex'
  );
  IF actual_sha256 <> '893530014b98627cef1567400d8ed115896c561714ea91887e419d24bd428295' THEN
    RAISE EXCEPTION 'zero-atom planner definition drifted';
  END IF;

  needle := 'p_limit integer DEFAULT 1';
  replacement := 'p_packet_id uuid';
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'zero-atom planner signature drifted';
  END IF;
  ddl := replace(ddl, needle, replacement);

  needle := '      AND p_limit BETWEEN 1 AND 25';
  replacement := $replacement$      AND p_packet_id IS NOT NULL
      AND packet.packet_id=p_packet_id$replacement$;
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'zero-atom planner bound drifted';
  END IF;
  ddl := replace(ddl, needle, replacement);

  needle := $needle$    ORDER BY value.created_at,value.packet_id
    LIMIT p_limit$needle$;
  replacement := '    ORDER BY value.created_at,value.packet_id';
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'zero-atom planner limit clause drifted';
  END IF;
  EXECUTE replace(ddl, needle, replacement);

  SELECT pg_get_functiondef(
    'memory.finalize_owner_v5_2_terminal_route_v1(uuid,uuid,uuid,text,text,text[])'
      ::regprocedure
  ) INTO ddl;
  actual_sha256 := encode(
    public.digest(convert_to(ddl, 'UTF8'), 'sha256'),
    'hex'
  );
  IF actual_sha256 <> '28238c2a998615574f0df79580ab6a66eade4f1758a1e7a02f275d5e6f80b6fa' THEN
    RAISE EXCEPTION 'terminal finalizer definition drifted';
  END IF;
  needle := 'FROM memory.plan_owner_v5_2_local_packet_route_v1(25) AS planned';
  replacement := 'FROM memory.plan_owner_v5_2_exact_packet_route_v1(p_packet_id) AS planned';
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'terminal finalizer planner call drifted';
  END IF;
  EXECUTE replace(ddl, needle, replacement);

  SELECT pg_get_functiondef(
    'memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)'
      ::regprocedure
  ) INTO ddl;
  actual_sha256 := encode(
    public.digest(convert_to(ddl, 'UTF8'), 'sha256'),
    'hex'
  );
  IF actual_sha256 <> '21a0fbb82434331d6212c669aedaa3912c94ff4888571637e61e5bd88dc15ef0' THEN
    RAISE EXCEPTION 'review finalizer definition drifted';
  END IF;
  needle := 'FROM memory.plan_owner_v5_2_local_packet_route_v1(25) AS planned';
  replacement := 'FROM memory.plan_owner_v5_2_exact_packet_route_v1(p_packet_id) AS planned';
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'review finalizer planner call drifted';
  END IF;
  EXECUTE replace(ddl, needle, replacement);

  SELECT pg_get_functiondef(
    'memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(uuid,uuid,uuid,text,text,text,text[])'
      ::regprocedure
  ) INTO ddl;
  actual_sha256 := encode(
    public.digest(convert_to(ddl, 'UTF8'), 'sha256'),
    'hex'
  );
  IF actual_sha256 <> '37cc38c20b17eba9c2f0da94c4db7181fbf5842a225e28ce8519571c433c16bc' THEN
    RAISE EXCEPTION 'zero-atom finalizer definition drifted';
  END IF;
  needle := 'FROM memory.plan_owner_v5_2_zero_atom_deferral_route_v1(25) AS planned';
  replacement := 'FROM memory.plan_owner_v5_2_zero_atom_deferral_route_v1(p_packet_id) AS planned';
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'zero-atom finalizer planner call drifted';
  END IF;
  EXECUTE replace(ddl, needle, replacement);
END
$migration$;

ALTER FUNCTION memory.plan_owner_v5_2_exact_packet_route_v1(uuid)
  OWNER TO memory_v5_2_local_router_maintainer;
ALTER FUNCTION memory.plan_owner_v5_2_zero_atom_deferral_route_v1(integer)
  OWNER TO memory_v5_2_local_router_maintainer;
ALTER FUNCTION memory.plan_owner_v5_2_zero_atom_deferral_route_v1(uuid)
  OWNER TO memory_v5_2_local_router_maintainer;
ALTER FUNCTION memory.finalize_owner_v5_2_terminal_route_v1(
  uuid,uuid,uuid,text,text,text[]
) OWNER TO memory_v5_2_local_router_maintainer;
ALTER FUNCTION memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(
  uuid,uuid,uuid,text,text,text,text[]
) OWNER TO memory_v5_2_local_router_maintainer;
ALTER FUNCTION memory.record_owner_v5_2_review_route_v1(
  uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,
  integer,integer,integer,integer,integer
) OWNER TO memory_v5_2_local_router_maintainer;

REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_2_zero_atom_deferral_route_v1(uuid)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_2_zero_atom_deferral_route_v1(uuid)
FROM memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_2_zero_atom_deferral_route_v1(uuid)
FROM brains_app;

GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_2_zero_atom_deferral_route_v1(uuid)
TO memory_v5_2_local_router_maintainer;
GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_2_zero_atom_deferral_route_v1(uuid)
TO brains_app;

COMMENT ON FUNCTION
  memory.plan_owner_v5_2_zero_atom_deferral_route_v1(uuid)
IS 'Owner-scoped exact-packet V5.2 zero-atom deferral planner. It removes global queue-order dependence without widening packet eligibility.';

COMMIT;
