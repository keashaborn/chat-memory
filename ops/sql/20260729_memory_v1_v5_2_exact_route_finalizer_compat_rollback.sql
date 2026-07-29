BEGIN;

DO $rollback$
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
  IF actual_sha256 <> '9e03c2d36054e276bc1ebfd0992073b2efd56db99398669cc0a1dcbdee7a55f8' THEN
    RAISE EXCEPTION 'exact packet planner rollback definition drifted';
  END IF;

  needle := $needle$      packet.manual_review_required,
      packet.normalized_packet,
      ARRAY($needle$;
  replacement := $replacement$      packet.manual_review_required,
      ARRAY($replacement$;
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'exact packet planner rollback payload point drifted';
  END IF;
  ddl := replace(ddl, needle, replacement);

  needle := $needle$        WHEN value.entity_mention_count+value.observation_count
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
          THEN 'manual_review_artifact_ready'$needle$;
  replacement := $replacement$        WHEN value.entity_mention_count+value.observation_count
               +value.comparison_hint_count>=1
          THEN 'manual_review_artifact_ready'$replacement$;
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'exact packet planner rollback review branch drifted';
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
  IF actual_sha256 <> 'ce07a88cd4157a38b0816b1136d7d4f16d812764d6fce1772b1a85815cabcb94' THEN
    RAISE EXCEPTION 'terminal finalizer rollback definition drifted';
  END IF;
  needle := 'FROM memory.plan_owner_v5_2_exact_packet_route_v1(p_packet_id) AS planned';
  replacement := 'FROM memory.plan_owner_v5_2_local_packet_route_v1(25) AS planned';
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'terminal finalizer rollback planner call drifted';
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
  IF actual_sha256 <> 'b1ac9bd51e8e3d894d573786f4da5312aac49fca98bbcf61c476afbcb8e9d8c4' THEN
    RAISE EXCEPTION 'review finalizer rollback definition drifted';
  END IF;
  needle := 'FROM memory.plan_owner_v5_2_exact_packet_route_v1(p_packet_id) AS planned';
  replacement := 'FROM memory.plan_owner_v5_2_local_packet_route_v1(25) AS planned';
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'review finalizer rollback planner call drifted';
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
  IF actual_sha256 <> '28f417a147837062c821a99dc9c55d0d40c778d978bfd208e392a72ef64862a6' THEN
    RAISE EXCEPTION 'zero-atom finalizer rollback definition drifted';
  END IF;
  needle := 'FROM memory.plan_owner_v5_2_zero_atom_deferral_route_v1(p_packet_id) AS planned';
  replacement := 'FROM memory.plan_owner_v5_2_zero_atom_deferral_route_v1(25) AS planned';
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'zero-atom finalizer rollback planner call drifted';
  END IF;
  EXECUTE replace(ddl, needle, replacement);
END
$rollback$;

DROP FUNCTION memory.plan_owner_v5_2_zero_atom_deferral_route_v1(uuid);

ALTER FUNCTION memory.plan_owner_v5_2_exact_packet_route_v1(uuid)
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

DO $verify$
DECLARE
  actual_sha256 text;
BEGIN
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'::regprocedure
  ),'UTF8'),'sha256'),'hex') INTO actual_sha256;
  IF actual_sha256 <> '190e99d80d0b5d7ce6d742415384103b19d1dd68a20199a47b0ffcf772c7209b' THEN
    RAISE EXCEPTION 'exact packet planner rollback verification failed';
  END IF;

  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.finalize_owner_v5_2_terminal_route_v1(uuid,uuid,uuid,text,text,text[])'
      ::regprocedure
  ),'UTF8'),'sha256'),'hex') INTO actual_sha256;
  IF actual_sha256 <> '28238c2a998615574f0df79580ab6a66eade4f1758a1e7a02f275d5e6f80b6fa' THEN
    RAISE EXCEPTION 'terminal finalizer rollback verification failed';
  END IF;

  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(uuid,uuid,uuid,text,text,text,text[])'
      ::regprocedure
  ),'UTF8'),'sha256'),'hex') INTO actual_sha256;
  IF actual_sha256 <> '37cc38c20b17eba9c2f0da94c4db7181fbf5842a225e28ce8519571c433c16bc' THEN
    RAISE EXCEPTION 'zero-atom finalizer rollback verification failed';
  END IF;

  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.record_owner_v5_2_review_route_v1(uuid,uuid,uuid,text,text,uuid,uuid,text,text,text,integer,integer,integer,integer,integer)'
      ::regprocedure
  ),'UTF8'),'sha256'),'hex') INTO actual_sha256;
  IF actual_sha256 <> '21a0fbb82434331d6212c669aedaa3912c94ff4888571637e61e5bd88dc15ef0' THEN
    RAISE EXCEPTION 'review finalizer rollback verification failed';
  END IF;

  IF to_regprocedure(
    'memory.plan_owner_v5_2_zero_atom_deferral_route_v1(uuid)'
  ) IS NOT NULL THEN
    RAISE EXCEPTION 'exact zero-atom planner rollback left an overload';
  END IF;
END
$verify$;

COMMIT;
