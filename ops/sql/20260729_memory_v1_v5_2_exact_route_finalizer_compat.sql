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
  ddl := replace(ddl, needle, replacement);

  needle := $needle$@.reason_code == "entity_resolution_unresolved"$needle$;
  replacement := $replacement$(
                  @.reason_code == "entity_resolution_unresolved"
                  || @.reason_code == "unregistered_predicate"
                )$replacement$;
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'zero-atom review reason branch drifted';
  END IF;
  ddl := replace(ddl, needle, replacement);

  needle := $needle$            && @.reason_code != "entity_resolution_unresolved"
          )'$needle$;
  replacement := $replacement$            && @.reason_code != "entity_resolution_unresolved"
            && @.reason_code != "unregistered_predicate"
          )'$replacement$;
  IF (length(ddl) - length(replace(ddl, needle, ''))) / length(needle) <> 1 THEN
    RAISE EXCEPTION 'zero-atom review exclusion branch drifted';
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

DO $constraint_guard$
DECLARE
  actual_sha256 text;
BEGIN
  SELECT encode(public.digest(convert_to(pg_get_constraintdef(oid),'UTF8'),'sha256'),'hex')
    INTO actual_sha256
  FROM pg_constraint
  WHERE conrelid='memory.v5_2_local_packet_route_event'::regclass
    AND conname='v5_2_local_packet_route_event_check';
  IF actual_sha256 <> '7a097abfe9800c2b192d29e532b4200ca5b4a5bdd16e0b8c38e85227bf7c8d00' THEN
    RAISE EXCEPTION 'local packet route event constraint drifted';
  END IF;
END
$constraint_guard$;

ALTER TABLE memory.v5_2_local_packet_route_event
  DROP CONSTRAINT v5_2_local_packet_route_event_check,
  ADD CONSTRAINT v5_2_local_packet_route_event_check
  CHECK (
    (
      route='terminal_no_stage'
      AND reason_code='deferral_only_no_stage_v5_2'
      AND entity_mention_count=0
      AND observation_count=0
      AND comparison_hint_count=0
      AND deferral_count BETWEEN 1 AND 32
      AND cardinality(source_deferral_reason_codes) BETWEEN 1 AND 4
      AND source_deferral_reason_codes <@ ARRAY[
        'structured_domain','question_only','transient_state',
        'insufficient_evidence'
      ]::text[]
      AND review_id IS NULL
      AND request_id IS NULL
      AND review_contract IS NULL
      AND bundle_contract IS NULL
      AND review_report_sha256 IS NULL
      AND stage_bundle_sha256 IS NULL
      AND repository_commit IS NULL
      AND auto_link_count IS NULL
      AND manual_review_count IS NULL
      AND deferred_resolution_count IS NULL
      AND rejected_count IS NULL
      AND blocking_code_count IS NULL
    )
    OR
    (
      route='terminal_no_stage'
      AND reason_code='deferral_only_review_unresolved_v5_2'
      AND entity_mention_count=0
      AND observation_count=0
      AND comparison_hint_count=0
      AND deferral_count BETWEEN 1 AND 32
      AND cardinality(source_deferral_reason_codes) BETWEEN 1 AND 6
      AND source_deferral_reason_codes <@ ARRAY[
        'structured_domain','question_only','transient_state',
        'insufficient_evidence','entity_resolution_unresolved',
        'unregistered_predicate'
      ]::text[]
      AND source_deferral_reason_codes && ARRAY[
        'entity_resolution_unresolved','unregistered_predicate'
      ]::text[]
      AND review_id IS NULL
      AND request_id IS NULL
      AND review_contract IS NULL
      AND bundle_contract IS NULL
      AND review_report_sha256 IS NULL
      AND stage_bundle_sha256 IS NULL
      AND repository_commit IS NULL
      AND auto_link_count IS NULL
      AND manual_review_count IS NULL
      AND deferred_resolution_count IS NULL
      AND rejected_count IS NULL
      AND blocking_code_count IS NULL
    )
    OR
    (
      route='manual_review_artifact_ready'
      AND reason_code='reviewable_relational_packet_v5_2'
      AND entity_mention_count+observation_count+comparison_hint_count>=1
      AND cardinality(source_deferral_reason_codes)=0
      AND review_id IS NOT NULL
      AND request_id IS NOT NULL
      AND review_contract='memory_v1_v5_2_local_packet_review_v1'
      AND bundle_contract='memory_v1_v5_2_stage_preflight_v1'
      AND review_report_sha256 ~ '^[0-9a-f]{64}$'
      AND stage_bundle_sha256 ~ '^[0-9a-f]{64}$'
      AND repository_commit ~ '^[0-9a-f]{40}$'
      AND auto_link_count BETWEEN 0 AND 32
      AND manual_review_count BETWEEN 0 AND 32
      AND deferred_resolution_count BETWEEN 0 AND 32
      AND rejected_count BETWEEN 0 AND 32
      AND blocking_code_count BETWEEN 0 AND 32
      AND auto_link_count+manual_review_count
            +deferred_resolution_count+rejected_count=entity_mention_count
    )
  );

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

DO $verify$
DECLARE
  actual_sha256 text;
BEGIN
  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'::regprocedure
  ),'UTF8'),'sha256'),'hex') INTO actual_sha256;
  IF actual_sha256 <> '190e99d80d0b5d7ce6d742415384103b19d1dd68a20199a47b0ffcf772c7209b' THEN
    RAISE EXCEPTION 'exact packet planner verification failed';
  END IF;

  SELECT encode(public.digest(convert_to(pg_get_functiondef(
    'memory.plan_owner_v5_2_zero_atom_deferral_route_v1(uuid)'::regprocedure
  ),'UTF8'),'sha256'),'hex') INTO actual_sha256;
  IF actual_sha256 <> '57ade956b6b1bb34e937536adff359d08d797ac2ad0ec45f7794830d72494aa6' THEN
    RAISE EXCEPTION 'exact zero-atom planner verification failed';
  END IF;

  SELECT encode(public.digest(convert_to(pg_get_constraintdef(oid),'UTF8'),'sha256'),'hex')
    INTO actual_sha256
  FROM pg_constraint
  WHERE conrelid='memory.v5_2_local_packet_route_event'::regclass
    AND conname='v5_2_local_packet_route_event_check';
  IF actual_sha256 <> 'd86e11b41c1a4a88f6d2d4dea45acff757293f26be86ec3ec300ed268202985c' THEN
    RAISE EXCEPTION 'route event constraint verification failed';
  END IF;
END
$verify$;

COMMIT;
