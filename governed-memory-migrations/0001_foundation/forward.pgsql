-- Clean governed Memory foundation. Apply only to the empty `governed_memory`
-- database after roles_preflight.pgsql succeeds. The migration runner supplies
-- BEGIN/COMMIT, timeouts, and an advisory transaction lock.

CREATE SCHEMA memory AUTHORIZATION governed_memory_owner;
CREATE SCHEMA memory_private AUTHORIZATION governed_memory_owner;

REVOKE ALL ON SCHEMA memory FROM PUBLIC;
REVOKE ALL ON SCHEMA memory_private FROM PUBLIC;
GRANT USAGE ON SCHEMA memory TO governed_memory_api, governed_memory_worker;
GRANT USAGE ON SCHEMA memory_private TO governed_memory_api, governed_memory_worker;

ALTER DEFAULT PRIVILEGES FOR ROLE governed_memory_owner IN SCHEMA memory
  REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE governed_memory_owner IN SCHEMA memory
  REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE governed_memory_owner IN SCHEMA memory
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE governed_memory_owner IN SCHEMA memory_private
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE governed_memory_owner IN SCHEMA memory_private
  REVOKE ALL ON SEQUENCES FROM PUBLIC;

CREATE SEQUENCE memory_private.worker_lane_sequence AS bigint
  START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE memory_private.worker_lane_sequence
  OWNER TO governed_memory_owner;
REVOKE ALL ON SEQUENCE memory_private.worker_lane_sequence
  FROM PUBLIC, governed_memory_api, governed_memory_worker;

CREATE FUNCTION memory_private.next_worker_lane()
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  cursor_value bigint;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  cursor_value := pg_catalog.nextval(
    'memory_private.worker_lane_sequence'::pg_catalog.regclass
  );
  RETURN (ARRAY['bridge', 'extraction', 'projection']::text[])[
    (((cursor_value - 1) % 3) + 1)::integer
  ];
END;
$function$;
ALTER FUNCTION memory_private.next_worker_lane()
  OWNER TO governed_memory_owner;
REVOKE ALL ON FUNCTION memory_private.next_worker_lane()
  FROM PUBLIC, governed_memory_api, governed_memory_worker;

CREATE FUNCTION memory_private.is_sorted_unique_allowlist(
  p_values text[],
  p_allowed text[]
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.cardinality(p_values) > 0
    AND pg_catalog.array_ndims(p_values) = 1
    AND pg_catalog.array_position(p_values, NULL) IS NULL
    AND p_values <@ p_allowed
    AND p_values = ARRAY(
      SELECT DISTINCT value
      FROM pg_catalog.unnest(p_values) AS item(value)
      ORDER BY value
    )
$function$;
REVOKE ALL ON FUNCTION memory_private.is_sorted_unique_allowlist(
  text[],text[]
) FROM PUBLIC;

CREATE FUNCTION memory_private.is_ascii_key_array(p_values text[])
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT NOT EXISTS (
    SELECT 1
    FROM pg_catalog.unnest(p_values) AS item(value)
    WHERE item.value !~ '^[a-z][a-z0-9_]{0,127}$'
  )
$function$;
REVOKE ALL ON FUNCTION memory_private.is_ascii_key_array(text[])
  FROM PUBLIC;

CREATE FUNCTION memory_private.is_sorted_unique_key_array(
  p_values text[],
  p_minimum integer,
  p_maximum integer
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.cardinality(p_values) BETWEEN p_minimum AND p_maximum
    AND pg_catalog.array_position(p_values, NULL) IS NULL
    AND NOT EXISTS (
      SELECT 1
      FROM pg_catalog.unnest(p_values) AS item(value)
      WHERE item.value !~ '^[a-z][a-z0-9_.:-]{0,127}$'
    )
    AND p_values = ARRAY(
      SELECT DISTINCT value
      FROM pg_catalog.unnest(p_values) AS item(value)
      ORDER BY value
    )
$function$;
REVOKE ALL ON FUNCTION memory_private.is_sorted_unique_key_array(
  text[],integer,integer
) FROM PUBLIC;

CREATE FUNCTION memory_private.framed_utf8_field(
  p_name text,
  p_value text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT p_name || ':' || CASE
    WHEN p_value IS NULL THEN '-:' || E'\n'
    ELSE pg_catalog.octet_length(
           pg_catalog.convert_to(normalize(p_value, NFC), 'UTF8')
         )::text
         || ':' || normalize(p_value, NFC) || E'\n'
  END
$function$;
REVOKE ALL ON FUNCTION memory_private.framed_utf8_field(text,text)
  FROM PUBLIC;

CREATE FUNCTION memory_private.timestamp_epoch_us(p_value timestamptz)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT CASE WHEN p_value IS NULL THEN NULL::text
    ELSE (
      EXTRACT(epoch FROM p_value) * 1000000
    )::bigint::text END
$function$;
REVOKE ALL ON FUNCTION memory_private.timestamp_epoch_us(timestamptz)
  FROM PUBLIC;

CREATE FUNCTION memory_private.timestamp_utc_text(p_value timestamptz)
RETURNS text
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT CASE WHEN p_value IS NULL THEN NULL::text ELSE pg_catalog.to_char(
    p_value AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
  ) END
$function$;
REVOKE ALL ON FUNCTION memory_private.timestamp_utc_text(timestamptz)
  FROM PUBLIC;

CREATE FUNCTION memory_private.answer_binding_retention_receipt_sha256(
  p_owner_user_id uuid,
  p_operation_id uuid,
  p_binding_id uuid,
  p_expires_at timestamptz
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.answer_binding_retention_purge.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'owner_user_id', p_owner_user_id::text
         )
      || memory_private.framed_utf8_field(
           'operation_id', p_operation_id::text
         )
      || memory_private.framed_utf8_field('binding_id', p_binding_id::text)
      || memory_private.framed_utf8_field(
           'expires_at', memory_private.timestamp_utc_text(p_expires_at)
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION
  memory_private.answer_binding_retention_receipt_sha256(
    uuid,uuid,uuid,timestamptz
  ) FROM PUBLIC;

CREATE FUNCTION memory_private.derived_uuid(
  p_operation_id uuid,
  p_purpose text
)
RETURNS uuid
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  WITH raw AS (
    SELECT public.digest(
      pg_catalog.decode('6ba7b8119dad11d180b400c04fd430c8', 'hex')
      || pg_catalog.convert_to(
        'governed-memory:' || p_operation_id::text || ':' || p_purpose,
        'UTF8'
      ), 'sha1'
    ) AS value
  ), versioned AS (
    SELECT pg_catalog.set_byte(
      pg_catalog.set_byte(
        value, 6, (pg_catalog.get_byte(value, 6) & 15) | 80
      ),
      8, (pg_catalog.get_byte(value, 8) & 63) | 128
    ) AS value
    FROM raw
  ), digest AS (
    SELECT pg_catalog.encode(
      substring(value FROM 1 FOR 16), 'hex'
    ) AS value
    FROM versioned
  )
  SELECT (
    pg_catalog.substr(value, 1, 8) || '-'
    || pg_catalog.substr(value, 9, 4) || '-'
    || pg_catalog.substr(value, 13, 4) || '-'
    || pg_catalog.substr(value, 17, 4) || '-'
    || pg_catalog.substr(value, 21, 12)
  )::uuid
  FROM digest
$function$;
REVOKE ALL ON FUNCTION memory_private.derived_uuid(uuid,text) FROM PUBLIC;

CREATE FUNCTION memory_private.uuid5(p_namespace uuid, p_name text)
RETURNS uuid
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
DECLARE
  digest bytea;
  value text;
BEGIN
  digest := public.digest(
    pg_catalog.decode(pg_catalog.replace(p_namespace::text, '-', ''), 'hex')
      || pg_catalog.convert_to(normalize(p_name, NFC), 'UTF8'),
    'sha1'
  );
  digest := pg_catalog.set_byte(
    digest, 6, (pg_catalog.get_byte(digest, 6) & 15) | 80
  );
  digest := pg_catalog.set_byte(
    digest, 8, (pg_catalog.get_byte(digest, 8) & 63) | 128
  );
  value := pg_catalog.encode(substring(digest FROM 1 FOR 16), 'hex');
  RETURN (
    pg_catalog.substr(value, 1, 8) || '-'
    || pg_catalog.substr(value, 9, 4) || '-'
    || pg_catalog.substr(value, 13, 4) || '-'
    || pg_catalog.substr(value, 17, 4) || '-'
    || pg_catalog.substr(value, 21, 12)
  )::uuid;
END;
$function$;
REVOKE ALL ON FUNCTION memory_private.uuid5(uuid,text) FROM PUBLIC;

CREATE FUNCTION memory_private.validated_fact_sha256(
  p_selection_binding_sha256 text,
  p_fact_index integer,
  p_subject_entity_type text,
  p_subject_entity_key text,
  p_subject_display_name text,
  p_predicate text,
  p_object_kind text,
  p_object_entity_type text,
  p_object_entity_key text,
  p_object_display_name text,
  p_object_literal jsonb,
  p_epistemic_state text,
  p_sensitivity text,
  p_semantic_key_sha256 text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.validated_fact.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'selection_binding_sha256', p_selection_binding_sha256
         )
      || memory_private.framed_utf8_field('fact_index', p_fact_index::text)
      || memory_private.framed_utf8_field(
           'subject_entity_type', p_subject_entity_type
         )
      || memory_private.framed_utf8_field(
           'subject_entity_key', p_subject_entity_key
         )
      || memory_private.framed_utf8_field(
           'subject_display_name', p_subject_display_name
         )
      || memory_private.framed_utf8_field('predicate', p_predicate)
      || memory_private.framed_utf8_field('object_kind', p_object_kind)
      || memory_private.framed_utf8_field(
           'object_entity_type', p_object_entity_type
         )
      || memory_private.framed_utf8_field(
           'object_entity_key', p_object_entity_key
         )
      || memory_private.framed_utf8_field(
           'object_display_name', p_object_display_name
         )
      || memory_private.framed_utf8_field(
           'object_literal', CASE WHEN p_object_kind = 'literal'
             AND pg_catalog.jsonb_typeof(p_object_literal) = 'string'
             THEN p_object_literal #>> '{}' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'epistemic_state', p_epistemic_state
         )
      || memory_private.framed_utf8_field('sensitivity', p_sensitivity)
      || memory_private.framed_utf8_field(
           'semantic_key_sha256', p_semantic_key_sha256
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.validated_fact_sha256(
  text,integer,text,text,text,text,text,text,text,text,jsonb,text,text,text
) FROM PUBLIC;

CREATE FUNCTION memory_private.correction_window_sha256(
  p_owner_user_id uuid,
  p_claim_id uuid,
  p_revision_id uuid,
  p_operation_id uuid,
  p_evidence_id uuid,
  p_source_message_id uuid,
  p_source_thread_id uuid,
  p_source_window_id uuid,
  p_source_sha256 text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.correction_window.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'owner_user_id', p_owner_user_id::text
         )
      || memory_private.framed_utf8_field('claim_id', p_claim_id::text)
      || memory_private.framed_utf8_field(
           'revision_id', p_revision_id::text
         )
      || memory_private.framed_utf8_field(
           'operation_id', p_operation_id::text
         )
      || memory_private.framed_utf8_field(
           'evidence_id', p_evidence_id::text
         )
      || memory_private.framed_utf8_field(
           'source_message_id', p_source_message_id::text
         )
      || memory_private.framed_utf8_field(
           'source_thread_id', p_source_thread_id::text
         )
      || memory_private.framed_utf8_field(
           'source_window_id', p_source_window_id::text
         )
      || memory_private.framed_utf8_field('source_sha256', p_source_sha256),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.correction_window_sha256(
  uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text
) FROM PUBLIC;

CREATE FUNCTION memory_private.selection_binding_sha256(
  p_owner_user_id uuid,
  p_source_kind text,
  p_source_message_id uuid,
  p_source_thread_id uuid,
  p_source_window_id uuid,
  p_source_window_sha256 text,
  p_source_sha256 text,
  p_selected_sha256 text,
  p_selected_start_utf8 integer,
  p_selected_end_utf8 integer,
  p_context_message_id uuid,
  p_context_sha256 text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.selection_binding.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'owner_user_id', p_owner_user_id::text
         )
      || memory_private.framed_utf8_field('source_kind', p_source_kind)
      || memory_private.framed_utf8_field(
           'source_message_id', p_source_message_id::text
         )
      || memory_private.framed_utf8_field(
           'source_thread_id', p_source_thread_id::text
         )
      || memory_private.framed_utf8_field(
           'source_window_id', p_source_window_id::text
         )
      || memory_private.framed_utf8_field(
           'window_sha256', p_source_window_sha256
         )
      || memory_private.framed_utf8_field('source_sha256', p_source_sha256)
      || memory_private.framed_utf8_field(
           'selected_sha256', p_selected_sha256
         )
      || memory_private.framed_utf8_field(
           'start_utf8', p_selected_start_utf8::text
         )
      || memory_private.framed_utf8_field(
           'end_utf8', p_selected_end_utf8::text
         )
      || memory_private.framed_utf8_field(
           'context_message_id', p_context_message_id::text
         )
      || memory_private.framed_utf8_field('context_sha256', p_context_sha256),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.selection_binding_sha256(
  uuid,text,uuid,uuid,uuid,text,text,text,integer,integer,uuid,text
) FROM PUBLIC;

CREATE FUNCTION memory_private.ingest_successor_receipt_sha256(
  p_owner_user_id uuid,
  p_operation_id uuid,
  p_bridge_source_binding_sha256 text,
  p_decision text,
  p_evidence_id uuid,
  p_extraction_job_id uuid
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.ingest_successor_receipt.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'owner_user_id', p_owner_user_id::text
         )
      || memory_private.framed_utf8_field(
           'operation_id', p_operation_id::text
         )
      || memory_private.framed_utf8_field(
           'bridge_source_binding_sha256', p_bridge_source_binding_sha256
         )
      || memory_private.framed_utf8_field('decision', p_decision)
      || memory_private.framed_utf8_field('evidence_id', p_evidence_id::text)
      || memory_private.framed_utf8_field(
           'extraction_job_id', p_extraction_job_id::text
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.ingest_successor_receipt_sha256(
  uuid,uuid,text,text,uuid,uuid
) FROM PUBLIC;

CREATE FUNCTION memory_private.semantic_key_sha256(
  p_subject_entity_key text,
  p_predicate text,
  p_object_kind text,
  p_object_entity_key text,
  p_object_literal jsonb
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.semantic_key.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'subject_entity_key', p_subject_entity_key
         )
      || memory_private.framed_utf8_field('predicate', p_predicate)
      || memory_private.framed_utf8_field('object_kind', p_object_kind)
      || memory_private.framed_utf8_field(
           'object_entity_key', CASE WHEN p_object_kind = 'entity'
             THEN p_object_entity_key ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'object_literal', CASE WHEN p_object_kind = 'literal'
             AND pg_catalog.jsonb_typeof(p_object_literal) = 'string'
             THEN p_object_literal #>> '{}' ELSE NULL::text END
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.semantic_key_sha256(
  text,text,text,text,jsonb
) FROM PUBLIC;

CREATE FUNCTION memory_private.claim_identity_sha256(
  p_subject_entity_key text,
  p_predicate text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.claim_identity.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'subject_entity_key', p_subject_entity_key
         )
      || memory_private.framed_utf8_field('predicate', p_predicate),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.claim_identity_sha256(text,text)
  FROM PUBLIC;

CREATE FUNCTION memory_private.source_local_entity_key(
  p_owner_user_id uuid,
  p_selection_binding_sha256 text,
  p_slot_index integer,
  p_role text,
  p_entity_type text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT CASE
    WHEN p_owner_user_id IS NULL
      OR p_selection_binding_sha256 IS NULL
      OR p_selection_binding_sha256 !~ '^[0-9a-f]{64}$'
      OR p_slot_index IS NULL
      OR p_slot_index NOT BETWEEN 0 AND 7
      OR p_role IS NULL
      OR p_role NOT IN ('subject', 'object')
      OR p_entity_type IS NULL
      OR p_entity_type NOT IN (
        'self', 'person', 'pet', 'organization', 'place', 'other'
      ) THEN NULL::text
    WHEN p_entity_type = 'self' THEN 'self'
    ELSE 'local:' || pg_catalog.encode(pg_catalog.sha256(
      pg_catalog.convert_to(
        'governed_memory.source_local_entity.v1' || E'\n'
          || memory_private.framed_utf8_field(
               'owner_user_id', p_owner_user_id::text
             )
          || memory_private.framed_utf8_field(
               'selection_binding_sha256', p_selection_binding_sha256
             )
          || memory_private.framed_utf8_field(
               'fact_index', p_slot_index::text
             )
          || memory_private.framed_utf8_field('role', p_role)
          || memory_private.framed_utf8_field('entity_type', p_entity_type),
        'UTF8'
      )
    ), 'hex')
  END
$function$;
REVOKE ALL ON FUNCTION memory_private.source_local_entity_key(
  uuid,text,integer,text,text
) FROM PUBLIC;

CREATE FUNCTION memory_private.correction_source_material(
  p_subject_entity_key text,
  p_predicate text,
  p_object_kind text,
  p_object_entity_type text,
  p_object_display_name text,
  p_object_literal jsonb,
  p_epistemic_state text,
  p_sensitivity text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT '{"epistemic_state":' || pg_catalog.to_json(p_epistemic_state)::text
    || ',"object_display_name":'
    || COALESCE(pg_catalog.to_json(p_object_display_name)::text, 'null')
    || ',"object_entity_type":'
    || COALESCE(pg_catalog.to_json(p_object_entity_type)::text, 'null')
    || ',"object_kind":' || pg_catalog.to_json(p_object_kind)::text
    || ',"object_literal":' || CASE WHEN p_object_kind = 'literal'
      THEN pg_catalog.to_json(p_object_literal #>> '{}')::text ELSE 'null' END
    || ',"predicate":' || pg_catalog.to_json(p_predicate)::text
    || ',"sensitivity":' || pg_catalog.to_json(p_sensitivity)::text
    || ',"subject_entity_key":'
    || pg_catalog.to_json(p_subject_entity_key)::text || '}'
$function$;
REVOKE ALL ON FUNCTION memory_private.correction_source_material(
  text,text,text,text,text,jsonb,text,text
) FROM PUBLIC;

CREATE FUNCTION memory_private.revision_fact_policy_sha256(
  p_subject_entity_type text,
  p_subject_entity_key text,
  p_subject_display_name text,
  p_predicate text,
  p_object_kind text,
  p_object_entity_type text,
  p_object_entity_key text,
  p_object_display_name text,
  p_object_literal jsonb,
  p_epistemic_state text,
  p_sensitivity text,
  p_projectable boolean,
  p_domains text[],
  p_intents text[],
  p_surface text,
  p_requires_explicit boolean,
  p_valid_from timestamptz,
  p_valid_to timestamptz,
  p_selected_sha256 text,
  p_selection_binding_sha256 text,
  p_predicate_catalog_sha256 text,
  p_retrieval_text_sha256 text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.revision_fact_policy.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'subject_entity_type', p_subject_entity_type
         )
      || memory_private.framed_utf8_field(
           'subject_entity_key', p_subject_entity_key
         )
      || memory_private.framed_utf8_field(
           'subject_display_name', p_subject_display_name
         )
      || memory_private.framed_utf8_field('predicate', p_predicate)
      || memory_private.framed_utf8_field('object_kind', p_object_kind)
      || memory_private.framed_utf8_field(
           'object_entity_type', p_object_entity_type
         )
      || memory_private.framed_utf8_field(
           'object_entity_key', p_object_entity_key
         )
      || memory_private.framed_utf8_field(
           'object_display_name', p_object_display_name
         )
      || memory_private.framed_utf8_field(
           'object_literal', CASE WHEN p_object_kind = 'literal'
             AND pg_catalog.jsonb_typeof(p_object_literal) = 'string'
             THEN p_object_literal #>> '{}' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'epistemic_state', p_epistemic_state
         )
      || memory_private.framed_utf8_field('sensitivity', p_sensitivity)
      || memory_private.framed_utf8_field(
           'projectable', CASE WHEN p_projectable THEN 'true'
             WHEN NOT p_projectable THEN 'false' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'domains', CASE WHEN pg_catalog.cardinality(p_domains) = 0
             THEN '[]' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'intents', CASE WHEN pg_catalog.cardinality(p_intents) = 0
             THEN '[]' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field('surface', p_surface)
      || memory_private.framed_utf8_field(
           'requires_explicit', CASE WHEN p_requires_explicit THEN 'true'
             WHEN NOT p_requires_explicit THEN 'false' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'valid_from', memory_private.timestamp_epoch_us(p_valid_from)
         )
      || memory_private.framed_utf8_field(
           'valid_to', memory_private.timestamp_epoch_us(p_valid_to)
         )
      || memory_private.framed_utf8_field(
           'selected_sha256', p_selected_sha256
         )
      || memory_private.framed_utf8_field(
           'selection_binding_sha256', p_selection_binding_sha256
         )
      || memory_private.framed_utf8_field(
           'predicate_catalog_sha256', p_predicate_catalog_sha256
         )
      || memory_private.framed_utf8_field(
           'retrieval_text_sha256', p_retrieval_text_sha256
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.revision_fact_policy_sha256(
  text,text,text,text,text,text,text,text,jsonb,text,text,boolean,text[],text[],
  text,boolean,timestamptz,timestamptz,text,text,text,text
) FROM PUBLIC;

CREATE FUNCTION memory_private.render_relational_fact(
  p_subject_entity_type text,
  p_subject_entity_key text,
  p_subject_display_name text,
  p_predicate text,
  p_object_kind text,
  p_object_entity_type text,
  p_object_entity_key text,
  p_object_display_name text,
  p_object_literal jsonb
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT '{"object":' || CASE
      WHEN p_object_kind = 'literal' THEN
        '{"kind":"literal","literal":'
          || pg_catalog.to_json(p_object_literal #>> '{}')::text || '}'
      WHEN p_object_kind = 'entity' THEN
        '{"display_name":' || pg_catalog.to_json(p_object_display_name)::text
          || ',"entity_key":'
          || pg_catalog.to_json(p_object_entity_key)::text
          || ',"entity_type":'
          || pg_catalog.to_json(p_object_entity_type)::text
          || ',"kind":"entity"}'
      ELSE NULL::text
    END
    || ',"predicate":' || pg_catalog.to_json(p_predicate)::text
    || ',"subject":{"display_name":'
    || COALESCE(pg_catalog.to_json(
         CASE WHEN p_subject_entity_type = 'self'
           THEN NULL::text ELSE p_subject_display_name END
       )::text, 'null')
    || ',"entity_key":' || pg_catalog.to_json(p_subject_entity_key)::text
    || ',"entity_type":' || pg_catalog.to_json(p_subject_entity_type)::text
    || '}}'
$function$;
REVOKE ALL ON FUNCTION memory_private.render_relational_fact(
  text,text,text,text,text,text,text,text,jsonb
) FROM PUBLIC;

CREATE FUNCTION memory_private.claim_revision_sha256(
  p_owner_user_id uuid,
  p_claim_id uuid,
  p_revision_id uuid,
  p_revision_number integer,
  p_source_proposal_id uuid,
  p_proposal_sha256 text,
  p_semantic_key_sha256 text,
  p_claim_identity_sha256 text,
  p_subject_entity_type text,
  p_subject_entity_key text,
  p_subject_display_name text,
  p_predicate text,
  p_object_kind text,
  p_object_entity_type text,
  p_object_entity_key text,
  p_object_display_name text,
  p_object_literal jsonb,
  p_epistemic_state text,
  p_sensitivity text,
  p_projectable boolean,
  p_domains text[],
  p_intents text[],
  p_surface text,
  p_requires_explicit boolean,
  p_valid_from timestamptz,
  p_valid_to timestamptz,
  p_source_sha256 text,
  p_selected_sha256 text,
  p_selection_binding_sha256 text,
  p_predicate_catalog_sha256 text,
  p_retrieval_text_sha256 text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.claim_revision.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'owner_user_id', p_owner_user_id::text
         )
      || memory_private.framed_utf8_field('claim_id', p_claim_id::text)
      || memory_private.framed_utf8_field(
           'revision_id', p_revision_id::text
         )
      || memory_private.framed_utf8_field(
           'revision_number', p_revision_number::text
         )
      || memory_private.framed_utf8_field(
           'proposal_id', p_source_proposal_id::text
         )
      || memory_private.framed_utf8_field(
           'proposal_sha256', p_proposal_sha256
         )
      || memory_private.framed_utf8_field(
           'semantic_key_sha256', p_semantic_key_sha256
         )
      || memory_private.framed_utf8_field(
           'claim_identity_sha256', p_claim_identity_sha256
         )
      || memory_private.framed_utf8_field(
           'subject_entity_type', p_subject_entity_type
         )
      || memory_private.framed_utf8_field(
           'subject_entity_key', p_subject_entity_key
         )
      || memory_private.framed_utf8_field(
           'subject_display_name', p_subject_display_name
         )
      || memory_private.framed_utf8_field('predicate', p_predicate)
      || memory_private.framed_utf8_field('object_kind', p_object_kind)
      || memory_private.framed_utf8_field(
           'object_entity_type', p_object_entity_type
         )
      || memory_private.framed_utf8_field(
           'object_entity_key', p_object_entity_key
         )
      || memory_private.framed_utf8_field(
           'object_display_name', p_object_display_name
         )
      || memory_private.framed_utf8_field(
           'object_literal', CASE WHEN p_object_kind = 'literal'
             AND pg_catalog.jsonb_typeof(p_object_literal) = 'string'
             THEN p_object_literal #>> '{}' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'epistemic_state', p_epistemic_state
         )
      || memory_private.framed_utf8_field('sensitivity', p_sensitivity)
      || memory_private.framed_utf8_field(
           'projectable', CASE WHEN p_projectable THEN 'true'
             WHEN NOT p_projectable THEN 'false' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'domains', CASE WHEN pg_catalog.cardinality(p_domains) = 0
             THEN '[]' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'intents', CASE WHEN pg_catalog.cardinality(p_intents) = 0
             THEN '[]' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field('surface', p_surface)
      || memory_private.framed_utf8_field(
           'requires_explicit', CASE WHEN p_requires_explicit THEN 'true'
             WHEN NOT p_requires_explicit THEN 'false' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'valid_from', memory_private.timestamp_utc_text(p_valid_from)
         )
      || memory_private.framed_utf8_field(
           'valid_to', memory_private.timestamp_utc_text(p_valid_to)
         )
      || memory_private.framed_utf8_field(
           'source_sha256', p_source_sha256
         )
      || memory_private.framed_utf8_field(
           'selected_sha256', p_selected_sha256
         )
      || memory_private.framed_utf8_field(
           'selection_binding_sha256', p_selection_binding_sha256
         )
      || memory_private.framed_utf8_field(
           'predicate_catalog_sha256', p_predicate_catalog_sha256
         )
      || memory_private.framed_utf8_field(
           'retrieval_text_sha256', p_retrieval_text_sha256
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.claim_revision_sha256(
  uuid,uuid,uuid,integer,uuid,text,text,text,text,text,text,text,text,text,text,text,
  jsonb,text,text,boolean,text[],text[],text,boolean,timestamptz,timestamptz,
  text,text,text,text,text
) FROM PUBLIC;

CREATE FUNCTION memory_private.claim_state_sha256(
  p_owner_user_id uuid,
  p_claim_id uuid,
  p_semantic_key_sha256 text,
  p_claim_identity_sha256 text,
  p_lifecycle_state text,
  p_is_current boolean,
  p_current_revision_id uuid,
  p_current_revision_number integer,
  p_current_revision_sha256 text,
  p_projection_sequence integer
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.claim_state.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'owner_user_id', p_owner_user_id::text
         )
      || memory_private.framed_utf8_field('claim_id', p_claim_id::text)
      || memory_private.framed_utf8_field(
           'semantic_key_sha256', p_semantic_key_sha256
         )
      || memory_private.framed_utf8_field(
           'claim_identity_sha256', p_claim_identity_sha256
         )
      || memory_private.framed_utf8_field(
           'lifecycle_state', p_lifecycle_state
         )
      || memory_private.framed_utf8_field(
           'is_current', CASE WHEN p_is_current THEN 'true'
             WHEN NOT p_is_current THEN 'false' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'revision_id', p_current_revision_id::text
         )
      || memory_private.framed_utf8_field(
           'revision_number', p_current_revision_number::text
         )
      || memory_private.framed_utf8_field(
           'revision_sha256', p_current_revision_sha256
         )
      || memory_private.framed_utf8_field(
           'projection_sequence', p_projection_sequence::text
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.claim_state_sha256(
  uuid,uuid,text,text,text,boolean,uuid,integer,text,integer
) FROM PUBLIC;

CREATE FUNCTION memory_private.projection_contract_sha256()
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT '9a54cf123493a25646b29cadf2be068039ac7062ed6d58e69e8b6e7a58dfe59a'::text
$function$;
REVOKE ALL ON FUNCTION memory_private.projection_contract_sha256()
  FROM PUBLIC;

CREATE FUNCTION memory_private.projection_manifest_sha256(
  p_owner_user_id uuid,
  p_claim_id uuid,
  p_revision_id uuid,
  p_operation_id uuid,
  p_operation text,
  p_sequence_number integer,
  p_revision_sha256 text,
  p_selection_binding_sha256 text,
  p_retrieval_text_sha256 text,
  p_embedding_input_sha256 text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.projection_operation.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'projection_contract_sha256',
           memory_private.projection_contract_sha256()
         )
      || memory_private.framed_utf8_field(
           'owner_user_id', p_owner_user_id::text
         )
      || memory_private.framed_utf8_field('claim_id', p_claim_id::text)
      || memory_private.framed_utf8_field(
           'revision_id', p_revision_id::text
         )
      || memory_private.framed_utf8_field(
           'operation_id', p_operation_id::text
         )
      || memory_private.framed_utf8_field('operation', p_operation)
      || memory_private.framed_utf8_field(
           'sequence_number', p_sequence_number::text
         )
      || memory_private.framed_utf8_field(
           'revision_sha256', p_revision_sha256
         )
      || memory_private.framed_utf8_field(
           'selection_binding_sha256', p_selection_binding_sha256
         )
      || memory_private.framed_utf8_field(
           'retrieval_text_sha256', p_retrieval_text_sha256
         )
      || memory_private.framed_utf8_field(
           'embedding_input_sha256', p_embedding_input_sha256
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.projection_manifest_sha256(
  uuid,uuid,uuid,uuid,text,integer,text,text,text,text
) FROM PUBLIC;

CREATE FUNCTION memory_private.framed_text_array(
  p_name text,
  p_values text[]
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT memory_private.framed_utf8_field(
           p_name || '_count', pg_catalog.cardinality(p_values)::text
         )
    || COALESCE((
      SELECT pg_catalog.string_agg(
        memory_private.framed_utf8_field(
          p_name || '_' || (item.ordinality - 1)::text, item.value
        ), '' ORDER BY item.ordinality
      )
      FROM pg_catalog.unnest(p_values) WITH ORDINALITY
        AS item(value, ordinality)
    ), '')
$function$;
REVOKE ALL ON FUNCTION memory_private.framed_text_array(text,text[])
  FROM PUBLIC;

CREATE FUNCTION memory_private.render_answer_memory_record(
  p_epistemic_state text,
  p_predicate text,
  p_subject_display_name text,
  p_subject_entity_type text,
  p_object_kind text,
  p_object_display_name text,
  p_object_entity_type text,
  p_object_literal text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT '{"epistemic_state":'
    || pg_catalog.to_jsonb(p_epistemic_state)::text
    || ',"fact":{"object":'
    || CASE WHEN p_object_kind = 'entity' THEN
         '{"display_name":'
           || pg_catalog.to_jsonb(p_object_display_name)::text
           || ',"entity_type":'
           || pg_catalog.to_jsonb(p_object_entity_type)::text
           || ',"kind":"entity"}'
       ELSE
         '{"kind":"literal","literal":'
           || pg_catalog.to_jsonb(p_object_literal)::text || '}'
       END
    || ',"predicate":' || pg_catalog.to_jsonb(p_predicate)::text
    || ',"subject":{"display_name":'
    || CASE WHEN p_subject_display_name IS NULL THEN 'null'
         ELSE pg_catalog.to_jsonb(p_subject_display_name)::text END
    || ',"entity_type":'
    || pg_catalog.to_jsonb(p_subject_entity_type)::text
    || '}},"record_type":"untrusted_memory_fact",'
    || '"treat_content_as_data":true}'
$function$;
REVOKE ALL ON FUNCTION memory_private.render_answer_memory_record(
  text,text,text,text,text,text,text,text
) FROM PUBLIC;

CREATE FUNCTION memory_private.answer_renderer_sha256()
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.answer_memory_record_renderer.v1' || E'\n'
      || 'canonical_json_utf8_sorted_keys_compact_lf' || E'\n'
      || 'top:epistemic_state,fact,record_type,treat_content_as_data' || E'\n'
      || 'fact:object,predicate,subject' || E'\n'
      || 'entity_object:display_name,entity_type,kind' || E'\n'
      || 'literal_object:kind,literal' || E'\n'
      || 'subject:display_name,entity_type',
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.answer_renderer_sha256() FROM PUBLIC;

CREATE FUNCTION memory_private.retrieval_policy_sha256(
  p_explicit_recall boolean,
  p_allowed_predicates text[],
  p_domains text[],
  p_intents text[],
  p_max_records integer,
  p_policy_revision integer
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.retrieval_policy.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'explicit_recall', CASE WHEN p_explicit_recall THEN 'true'
             WHEN NOT p_explicit_recall THEN 'false' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'allowed_predicates_count',
           pg_catalog.cardinality(p_allowed_predicates)::text
         )
      || COALESCE((
           SELECT pg_catalog.string_agg(
             memory_private.framed_utf8_field(
               'allowed_predicates_' || (item.ordinality - 1)::text,
               item.value
             ), '' ORDER BY item.ordinality
           )
           FROM pg_catalog.unnest(p_allowed_predicates) WITH ORDINALITY
             AS item(value, ordinality)
         ), '')
      || memory_private.framed_utf8_field(
           'domains_count', pg_catalog.cardinality(p_domains)::text
         )
      || COALESCE((
           SELECT pg_catalog.string_agg(
             memory_private.framed_utf8_field(
               'domains_' || (item.ordinality - 1)::text, item.value
             ), '' ORDER BY item.ordinality
           )
           FROM pg_catalog.unnest(p_domains) WITH ORDINALITY
             AS item(value, ordinality)
         ), '')
      || memory_private.framed_utf8_field(
           'intents_count', pg_catalog.cardinality(p_intents)::text
         )
      || COALESCE((
           SELECT pg_catalog.string_agg(
             memory_private.framed_utf8_field(
               'intents_' || (item.ordinality - 1)::text, item.value
             ), '' ORDER BY item.ordinality
           )
           FROM pg_catalog.unnest(p_intents) WITH ORDINALITY
             AS item(value, ordinality)
         ), '')
      || memory_private.framed_utf8_field(
           'max_records', p_max_records::text
         )
      || memory_private.framed_utf8_field(
           'policy_revision', p_policy_revision::text
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.retrieval_policy_sha256(
  boolean,text[],text[],text[],integer,integer
) FROM PUBLIC;

CREATE FUNCTION memory_private.answer_selection_manifest_sha256(
  p_owner_user_id uuid,
  p_response_id uuid,
  p_query_sha256 text,
  p_policy_sha256 text,
  p_renderer_sha256 text,
  p_prompt_sha256 text,
  p_explicit_recall boolean,
  p_selected_claim_ids uuid[],
  p_selected_revision_ids uuid[],
  p_selected_revision_sha256s text[],
  p_outcome text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.answer_selection.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'owner_user_id', p_owner_user_id::text
         )
      || memory_private.framed_utf8_field('response_id', p_response_id::text)
      || memory_private.framed_utf8_field('query_sha256', p_query_sha256)
      || memory_private.framed_utf8_field('policy_sha256', p_policy_sha256)
      || memory_private.framed_utf8_field('renderer_sha256', p_renderer_sha256)
      || memory_private.framed_utf8_field('prompt_sha256', p_prompt_sha256)
      || memory_private.framed_utf8_field(
           'explicit_recall', CASE WHEN p_explicit_recall THEN 'true'
             WHEN NOT p_explicit_recall THEN 'false' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'selected_claim_ids', p_selected_claim_ids::text
         )
      || memory_private.framed_utf8_field(
           'selected_revision_ids', p_selected_revision_ids::text
         )
      || memory_private.framed_utf8_field(
           'selected_revision_sha256s', p_selected_revision_sha256s::text
         )
      || memory_private.framed_utf8_field('outcome', p_outcome),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.answer_selection_manifest_sha256(
  uuid,uuid,text,text,text,text,boolean,uuid[],uuid[],text[],text
) FROM PUBLIC;

CREATE FUNCTION memory_private.answer_injection_manifest_sha256(
  p_selection_manifest_sha256 text,
  p_injected_claim_ids uuid[],
  p_injected_revision_ids uuid[],
  p_injected_revision_sha256s text[],
  p_memory_block_sha256 text,
  p_memory_block_utf8_bytes integer,
  p_escaped_memory_segment_sha256 text,
  p_escaped_segment_start_utf8 integer,
  p_escaped_segment_end_utf8 integer,
  p_escaped_segment_occurrence_count integer,
  p_outbound_request_sha256 text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.answer_injection.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'selection_manifest_sha256', p_selection_manifest_sha256
         )
      || memory_private.framed_utf8_field(
           'injected_claim_ids', p_injected_claim_ids::text
         )
      || memory_private.framed_utf8_field(
           'injected_revision_ids', p_injected_revision_ids::text
         )
      || memory_private.framed_utf8_field(
           'injected_revision_sha256s', p_injected_revision_sha256s::text
         )
      || memory_private.framed_utf8_field(
           'memory_block_sha256', p_memory_block_sha256
         )
      || memory_private.framed_utf8_field(
           'memory_block_utf8_bytes', p_memory_block_utf8_bytes::text
         )
      || memory_private.framed_utf8_field(
           'escaped_memory_segment_sha256',
           p_escaped_memory_segment_sha256
         )
      || memory_private.framed_utf8_field(
           'escaped_segment_start_utf8', p_escaped_segment_start_utf8::text
         )
      || memory_private.framed_utf8_field(
           'escaped_segment_end_utf8', p_escaped_segment_end_utf8::text
         )
      || memory_private.framed_utf8_field(
           'escaped_segment_occurrence_count',
           p_escaped_segment_occurrence_count::text
         )
      || memory_private.framed_utf8_field(
           'outbound_request_sha256', p_outbound_request_sha256
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.answer_injection_manifest_sha256(
  text,uuid[],uuid[],text[],text,integer,text,integer,integer,integer,text
) FROM PUBLIC;

CREATE FUNCTION memory_private.proposal_sha256(
  p_owner_user_id uuid,
  p_job_id uuid,
  p_evidence_id uuid,
  p_provider_call_id uuid,
  p_source_kind text,
  p_source_message_id uuid,
  p_source_thread_id uuid,
  p_source_window_id uuid,
  p_window_sha256 text,
  p_source_sha256 text,
  p_selected_sha256 text,
  p_selection_binding_sha256 text,
  p_context_message_id uuid,
  p_context_sha256 text,
  p_predicate_catalog_sha256 text,
  p_request_sha256 text,
  p_response_sha256 text,
  p_purpose text,
  p_correction_target_claim_id uuid,
  p_correction_target_revision_id uuid,
  p_correction_target_revision_number integer,
  p_correction_target_revision_sha256 text,
  p_correction_target_state_sha256 text,
  p_correction_target_identity_sha256 text,
  p_correction_target_projection_sequence integer,
  p_correction_pending_state_sha256 text,
  p_fact_index smallint,
  p_proposal_id uuid,
  p_operation_id uuid,
  p_subject_entity_type text,
  p_subject_entity_key text,
  p_subject_display_name text,
  p_predicate text,
  p_object_kind text,
  p_object_entity_type text,
  p_object_entity_key text,
  p_object_display_name text,
  p_object_literal jsonb,
  p_epistemic_state text,
  p_sensitivity text,
  p_semantic_key_sha256 text,
  p_projectable boolean,
  p_domains text[],
  p_intents text[],
  p_surface text,
  p_requires_explicit boolean,
  p_valid_from timestamptz,
  p_valid_to timestamptz
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
  SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.proposal.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'owner_user_id', p_owner_user_id::text
         )
      || memory_private.framed_utf8_field('job_id', p_job_id::text)
      || memory_private.framed_utf8_field(
           'evidence_id', p_evidence_id::text
         )
      || memory_private.framed_utf8_field(
           'provider_call_id', p_provider_call_id::text
         )
      || memory_private.framed_utf8_field('source_kind', p_source_kind)
      || memory_private.framed_utf8_field(
           'source_message_id', p_source_message_id::text
         )
      || memory_private.framed_utf8_field(
           'source_thread_id', p_source_thread_id::text
         )
      || memory_private.framed_utf8_field(
           'source_window_id', p_source_window_id::text
         )
      || memory_private.framed_utf8_field('window_sha256', p_window_sha256)
      || memory_private.framed_utf8_field('source_sha256', p_source_sha256)
      || memory_private.framed_utf8_field(
           'selected_sha256', p_selected_sha256
         )
      || memory_private.framed_utf8_field(
           'selection_binding_sha256', p_selection_binding_sha256
         )
      || memory_private.framed_utf8_field(
           'context_message_id', p_context_message_id::text
         )
      || memory_private.framed_utf8_field('context_sha256', p_context_sha256)
      || memory_private.framed_utf8_field(
           'predicate_catalog_sha256', p_predicate_catalog_sha256
         )
      || memory_private.framed_utf8_field('request_sha256', p_request_sha256)
      || memory_private.framed_utf8_field(
           'response_sha256', p_response_sha256
         )
      || memory_private.framed_utf8_field(
           'purpose', p_purpose
         )
      || memory_private.framed_utf8_field(
           'correction_target_claim_id', p_correction_target_claim_id::text
         )
      || memory_private.framed_utf8_field(
           'correction_target_revision_id',
           p_correction_target_revision_id::text
         )
      || memory_private.framed_utf8_field(
           'correction_target_revision_number',
           p_correction_target_revision_number::text
         )
      || memory_private.framed_utf8_field(
           'correction_target_revision_sha256',
           p_correction_target_revision_sha256
         )
      || memory_private.framed_utf8_field(
           'correction_target_state_sha256', p_correction_target_state_sha256
         )
      || memory_private.framed_utf8_field(
           'correction_target_identity_sha256',
           p_correction_target_identity_sha256
         )
      || memory_private.framed_utf8_field(
           'correction_target_projection_sequence',
           p_correction_target_projection_sequence::text
         )
      || memory_private.framed_utf8_field(
           'correction_pending_state_sha256',
           p_correction_pending_state_sha256
         )
      || memory_private.framed_utf8_field('fact_index', p_fact_index::text)
      || memory_private.framed_utf8_field(
           'proposal_id', p_proposal_id::text
         )
      || memory_private.framed_utf8_field(
           'operation_id', p_operation_id::text
         )
      || memory_private.framed_utf8_field(
           'subject_entity_type', p_subject_entity_type
         )
      || memory_private.framed_utf8_field(
           'subject_entity_key', p_subject_entity_key
         )
      || memory_private.framed_utf8_field(
           'subject_display_name', p_subject_display_name
         )
      || memory_private.framed_utf8_field('predicate', p_predicate)
      || memory_private.framed_utf8_field('object_kind', p_object_kind)
      || memory_private.framed_utf8_field(
           'object_entity_type', p_object_entity_type
         )
      || memory_private.framed_utf8_field(
           'object_entity_key', p_object_entity_key
         )
      || memory_private.framed_utf8_field(
           'object_display_name', p_object_display_name
         )
      || memory_private.framed_utf8_field(
           'object_literal', CASE WHEN p_object_kind = 'literal'
             AND pg_catalog.jsonb_typeof(p_object_literal) = 'string'
             THEN p_object_literal #>> '{}' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'epistemic_state', p_epistemic_state
         )
      || memory_private.framed_utf8_field('sensitivity', p_sensitivity)
      || memory_private.framed_utf8_field(
           'semantic_key_sha256', p_semantic_key_sha256
         )
      || memory_private.framed_utf8_field(
           'projectable', CASE WHEN p_projectable THEN 'true'
             WHEN NOT p_projectable THEN 'false' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'domains', CASE WHEN pg_catalog.cardinality(p_domains) = 0
             THEN '[]' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'intents', CASE WHEN pg_catalog.cardinality(p_intents) = 0
             THEN '[]' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field('surface', p_surface)
      || memory_private.framed_utf8_field(
           'requires_explicit', CASE WHEN p_requires_explicit THEN 'true'
             WHEN NOT p_requires_explicit THEN 'false' ELSE NULL::text END
         )
      || memory_private.framed_utf8_field(
           'valid_from', memory_private.timestamp_epoch_us(p_valid_from)
         )
      || memory_private.framed_utf8_field(
           'valid_to', memory_private.timestamp_epoch_us(p_valid_to)
         ),
    'UTF8'
  )), 'hex')
$function$;
REVOKE ALL ON FUNCTION memory_private.proposal_sha256(
  uuid,uuid,uuid,uuid,text,uuid,uuid,uuid,text,text,text,text,uuid,text,text,text,
  text,text,uuid,uuid,integer,text,text,text,integer,text,smallint,uuid,uuid,text,text,text,text,text,
  text,text,text,jsonb,text,text,text,boolean,text[],text[],text,boolean,
  timestamptz,timestamptz
) FROM PUBLIC;

DO $hash_contract_self_check$
BEGIN
  IF memory_private.selection_binding_sha256(
       '00000000-0000-0000-0000-000000000001'::uuid,
       'conversation_message',
       '00000000-0000-0000-0000-000000000002'::uuid,
       '00000000-0000-0000-0000-000000000003'::uuid,
       '00000000-0000-0000-0000-000000000004'::uuid,
       pg_catalog.repeat('1', 64), pg_catalog.repeat('2', 64),
       pg_catalog.repeat('3', 64), 7, 19, NULL::uuid, NULL::text
     ) <> '20057004136f64e54333d53d4e46c85d85eba494c3140596f197572b0d47b619'
     OR memory_private.selection_binding_sha256(
       '00000000-0000-0000-0000-000000000001'::uuid,
       'conversation_message',
       '00000000-0000-0000-0000-000000000002'::uuid,
       '00000000-0000-0000-0000-000000000003'::uuid,
       '00000000-0000-0000-0000-000000000004'::uuid,
       pg_catalog.repeat('1', 64), pg_catalog.repeat('2', 64),
       pg_catalog.repeat('3', 64), 7, 19,
       '00000000-0000-0000-0000-000000000005'::uuid,
       pg_catalog.repeat('4', 64)
     ) <> '88c6ea7d5e4aa020ca5f5acca6586e2b2dfe260c4d0b693ad06f27004ab2d860'
     OR memory_private.ingest_successor_receipt_sha256(
       '00000000-0000-0000-0000-000000000001'::uuid,
       '00000000-0000-0000-0000-000000000002'::uuid,
       pg_catalog.repeat('1', 64), 'send_external',
       '00000000-0000-0000-0000-000000000003'::uuid,
       '00000000-0000-0000-0000-000000000004'::uuid
     ) <> '89a5a4c63e334181cd637dcadf9b72033c7f1afe7810fcd9d7e82a03ee79a742'
     OR memory_private.answer_selection_manifest_sha256(
       '00000000-0000-4000-8000-000000000001'::uuid,
       '00000000-0000-4000-8000-000000000002'::uuid,
       pg_catalog.repeat('1', 64), pg_catalog.repeat('2', 64),
       pg_catalog.repeat('3', 64), pg_catalog.repeat('4', 64),
       false,
       ARRAY['00000000-0000-4000-8000-000000000003'::uuid],
       ARRAY['00000000-0000-4000-8000-000000000004'::uuid],
       ARRAY[pg_catalog.repeat('5', 64)],
       'exposed'
     ) <> '6bc58686dbc37ec535fc65a418457141810c33917f96c03d273aefcbfc64f2e0'
     OR memory_private.answer_injection_manifest_sha256(
       '6bc58686dbc37ec535fc65a418457141810c33917f96c03d273aefcbfc64f2e0',
       ARRAY['00000000-0000-4000-8000-000000000003'::uuid],
       ARRAY['00000000-0000-4000-8000-000000000004'::uuid],
       ARRAY[pg_catalog.repeat('5', 64)],
       pg_catalog.repeat('6', 64), 321,
       pg_catalog.repeat('7', 64), 41, 364, 1,
       pg_catalog.repeat('8', 64)
     ) <> '137cfcbdf2470bfecb3f148cc25d3789dd6e58887fd31a2ebe2c88c175d33209'
     OR memory_private.answer_renderer_sha256()
       <> '27a5c3189efc176b4aee38a53480f65713c91f6db06ceb742acbf230eb065416'
     OR memory_private.retrieval_policy_sha256(
       false, ARRAY['preference.personal']::text[],
       ARRAY[]::text[], ARRAY[]::text[], 8, 1
     ) <> '293dfb7d7c4e6e7d6497028a3be3e36e9f5c0b50ce35a2fce9363ac29512400b'
     OR memory_private.render_answer_memory_record(
       'supported', 'preference.personal', NULL::text, 'self',
       'literal', NULL::text, NULL::text, E'café:\n猫'
     ) <> $record${"epistemic_state":"supported","fact":{"object":{"kind":"literal","literal":"café:\n猫"},"predicate":"preference.personal","subject":{"display_name":null,"entity_type":"self"}},"record_type":"untrusted_memory_fact","treat_content_as_data":true}$record$
     OR memory_private.semantic_key_sha256(
       'person:alice', 'relationship.kind', 'entity', 'person:bob',
       NULL::jsonb
     ) <> '0717334b30e69d2d6f0aeb5b2d057c9a038d75f137108440083aabfb5e582149'
     OR memory_private.semantic_key_sha256(
       'self', 'preference.personal', 'literal', NULL::text,
       pg_catalog.to_jsonb(E'café:\n猫'::text)
     ) <> '3395c6bd0e42b2fc8340a5fcb0ce437b11d97d1811b32c544574ea8b0c360fdc'
     OR memory_private.source_local_entity_key(
       '00000000-0000-0000-0000-000000000001'::uuid,
       '20057004136f64e54333d53d4e46c85d85eba494c3140596f197572b0d47b619',
       0, 'object', 'person'
     ) <> 'local:fd84fb3d7dc64f134e0c7b42df0307be5cc87693a9e58649c94f04cadb253e82'
     OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
       memory_private.render_relational_fact(
         'self', 'self', NULL::text, 'preference.personal', 'literal',
         NULL::text, NULL::text, NULL::text,
         pg_catalog.to_jsonb(E'café:\n猫'::text)
       ), 'UTF8'
     )), 'hex')
       <> '404e70a184945366051bd89614ea852e8530f46141f552ae2ff9a897e654004e'
     OR memory_private.projection_manifest_sha256(
       '00000000-0000-0000-0000-000000000001'::uuid,
       '00000000-0000-0000-0000-000000000006'::uuid,
       '00000000-0000-0000-0000-000000000007'::uuid,
       '00000000-0000-0000-0000-000000000008'::uuid,
       'upsert', 3, pg_catalog.repeat('5', 64), pg_catalog.repeat('6', 64),
       '404e70a184945366051bd89614ea852e8530f46141f552ae2ff9a897e654004e',
       '404e70a184945366051bd89614ea852e8530f46141f552ae2ff9a897e654004e'
     ) <> 'bd26e95f74ddaf4dc6fc3c06da17851e65f50a86629531db56ecd70b5ef885be'
     OR memory_private.proposal_sha256(
       '00000000-0000-0000-0000-000000000001'::uuid,
       '00000000-0000-0000-0000-000000000006'::uuid,
       '00000000-0000-0000-0000-000000000007'::uuid,
       '00000000-0000-0000-0000-000000000008'::uuid,
       'conversation_message',
       '00000000-0000-0000-0000-000000000002'::uuid,
       '00000000-0000-0000-0000-000000000003'::uuid,
       '00000000-0000-0000-0000-000000000004'::uuid,
       pg_catalog.repeat('1', 64), pg_catalog.repeat('2', 64),
       pg_catalog.repeat('3', 64),
       '20057004136f64e54333d53d4e46c85d85eba494c3140596f197572b0d47b619',
       NULL::uuid, NULL::text,
       '5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded',
       pg_catalog.repeat('5', 64), pg_catalog.repeat('6', 64), 'new_claim',
       NULL::uuid, NULL::uuid, NULL::integer, NULL::text, NULL::text,
       NULL::text, NULL::integer, NULL::text, 0::smallint,
       '00000000-0000-0000-0000-000000000009'::uuid,
       '00000000-0000-0000-0000-00000000000a'::uuid,
       'self', 'self', NULL::text, 'preference.personal', 'literal',
       NULL::text, NULL::text, NULL::text,
       pg_catalog.to_jsonb(E'café:\n猫'::text), 'supported', 'ordinary',
       '3395c6bd0e42b2fc8340a5fcb0ce437b11d97d1811b32c544574ea8b0c360fdc',
       true, ARRAY[]::text[], ARRAY[]::text[], 'normal', false,
       NULL::timestamptz, NULL::timestamptz
     ) <> 'a8df61933716bb2809595922b6e24800ebfff064c9d667874ff511300ab4973c'
     OR memory_private.proposal_sha256(
       '00000000-0000-0000-0000-000000000001'::uuid, NULL::uuid,
       '00000000-0000-0000-0000-00000000000b'::uuid, NULL::uuid,
       'owner_correction',
       '00000000-0000-0000-0000-00000000000c'::uuid,
       '00000000-0000-0000-0000-00000000000d'::uuid,
       '00000000-0000-0000-0000-00000000000e'::uuid,
       pg_catalog.repeat('7', 64), pg_catalog.repeat('8', 64),
       pg_catalog.repeat('8', 64),
       '2d7c910118d227a1ad151999b63728fcc7e6c8d53c9ddadbd5bd289a37b11ac9',
       NULL::uuid, NULL::text,
       '5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded',
       NULL::text, NULL::text, 'correction',
       '00000000-0000-0000-0000-000000000006'::uuid,
       '00000000-0000-0000-0000-000000000007'::uuid, 2,
       pg_catalog.repeat('9', 64), pg_catalog.repeat('a', 64),
       'dff3fc2c55fd9bef445e1afa1f57a2bbac12fafd5ca8099b8f6a9b84da60b9a1',
       4, pg_catalog.repeat('b', 64), NULL::smallint,
       '75faff1f-5e2c-5525-9483-fe97f4afae69'::uuid,
       'fe148132-3436-5202-9895-37133aef69d2'::uuid,
       'self', 'self', NULL::text, 'preference.personal', 'literal',
       NULL::text, NULL::text, NULL::text,
       pg_catalog.to_jsonb('tea'::text), 'supported', 'ordinary',
       '7246dc1185cc5b11fd5d825111c4e33e62fc083f5df797a98fd0ebbfb9f90e65',
       true, ARRAY[]::text[], ARRAY[]::text[], 'normal', false,
       NULL::timestamptz, NULL::timestamptz
     ) <> 'f793cbc8773cfc39b11cd4867d277f3b5957037dc9ccd94b8aff431198d5960f'
     OR memory_private.claim_revision_sha256(
       '00000000-0000-0000-0000-000000000001'::uuid,
       '00000000-0000-0000-0000-000000000006'::uuid,
       '00000000-0000-0000-0000-000000000007'::uuid, 1,
       '00000000-0000-0000-0000-000000000009'::uuid,
       'a8df61933716bb2809595922b6e24800ebfff064c9d667874ff511300ab4973c',
       '3395c6bd0e42b2fc8340a5fcb0ce437b11d97d1811b32c544574ea8b0c360fdc',
       'dff3fc2c55fd9bef445e1afa1f57a2bbac12fafd5ca8099b8f6a9b84da60b9a1',
       'self', 'self', NULL::text, 'preference.personal', 'literal',
       NULL::text, NULL::text, NULL::text,
       pg_catalog.to_jsonb(E'café:\n猫'::text), 'supported', 'ordinary', true,
       ARRAY[]::text[], ARRAY[]::text[], 'normal', false,
       NULL::timestamptz, NULL::timestamptz, pg_catalog.repeat('2', 64),
       pg_catalog.repeat('3', 64),
       '20057004136f64e54333d53d4e46c85d85eba494c3140596f197572b0d47b619',
       '5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded',
       '404e70a184945366051bd89614ea852e8530f46141f552ae2ff9a897e654004e'
     ) <> '61f011a52dceb6beec78f788b3638047bfa576d534ecc233bf8b059965ddef54'
     OR memory_private.claim_state_sha256(
       '00000000-0000-0000-0000-000000000001'::uuid,
       '00000000-0000-0000-0000-000000000006'::uuid,
       '3395c6bd0e42b2fc8340a5fcb0ce437b11d97d1811b32c544574ea8b0c360fdc',
       'dff3fc2c55fd9bef445e1afa1f57a2bbac12fafd5ca8099b8f6a9b84da60b9a1',
       'active', true,
       '00000000-0000-0000-0000-000000000007'::uuid, 1,
       '61f011a52dceb6beec78f788b3638047bfa576d534ecc233bf8b059965ddef54',
       1
     ) <> '22444cef2c2c45950246c2d5e209386c0df41fe26793565e2967df85b898af68'
  THEN
    RAISE EXCEPTION 'cross-language framed hash contract self-check failed';
  END IF;
END;
$hash_contract_self_check$;

CREATE TABLE memory.predicate_catalog (
  predicate text PRIMARY KEY,
  catalog_name text NOT NULL,
  schema_revision integer NOT NULL,
  catalog_sha256 text NOT NULL,
  subject_kinds text[] NOT NULL,
  object_kinds text[] NOT NULL,
  sensitivities text[] NOT NULL,
  epistemic_statuses text[] NOT NULL,
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CONSTRAINT predicate_catalog_sha_predicate_unique UNIQUE (
    catalog_sha256, predicate
  ),
  CONSTRAINT predicate_catalog_predicate_format CHECK (
    predicate = pg_catalog.btrim(predicate)
    AND pg_catalog.length(predicate) BETWEEN 3 AND 100
    AND predicate ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'
  ),
  CONSTRAINT predicate_catalog_identity CHECK (
    catalog_name = 'personal-memory'
    AND schema_revision = 1
    AND catalog_sha256 =
      '5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded'
  ),
  CONSTRAINT predicate_catalog_subject_kinds CHECK (
    memory_private.is_sorted_unique_allowlist(
      subject_kinds,
      ARRAY['organization','other','person','pet','place','self']::text[]
    )
  ),
  CONSTRAINT predicate_catalog_object_kinds CHECK (
    memory_private.is_sorted_unique_allowlist(
      object_kinds, ARRAY['entity','literal']::text[]
    )
  ),
  CONSTRAINT predicate_catalog_sensitivities CHECK (
    memory_private.is_sorted_unique_allowlist(
      sensitivities,
      ARRAY[
        'ordinary','sensitive_self','sensitive_third_party'
      ]::text[]
    )
  ),
  CONSTRAINT predicate_catalog_epistemic_statuses CHECK (
    memory_private.is_sorted_unique_allowlist(
      epistemic_statuses,
      ARRAY['disputed','supported','uncertain']::text[]
    )
  )
);

CREATE TABLE memory.evidence (
  evidence_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  source_kind text NOT NULL,
  source_message_id uuid,
  source_thread_id uuid,
  source_window_id uuid,
  source_window_sha256 text,
  bridge_source_binding_sha256 text,
  ingest_receipt_sha256 text,
  source_sha256 text NOT NULL,
  selected_sha256 text NOT NULL,
  selection_binding_sha256 text NOT NULL,
  selected_start_utf8 integer,
  selected_end_utf8 integer,
  context_message_id uuid,
  context_sha256 text,
  source_created_at timestamptz NOT NULL,
  eligibility_decision text NOT NULL,
  eligibility_policy_sha256 text NOT NULL,
  review_excerpt text,
  excerpt_expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CONSTRAINT evidence_owner_id UNIQUE (owner_user_id, evidence_id),
  CONSTRAINT evidence_operation_id UNIQUE (owner_user_id, operation_id),
  CONSTRAINT evidence_source_identity UNIQUE NULLS NOT DISTINCT (
    owner_user_id, source_kind, source_message_id, source_sha256,
    selected_sha256, selected_start_utf8, selected_end_utf8,
    context_message_id, context_sha256, bridge_source_binding_sha256
  ),
  CONSTRAINT evidence_owner_nonzero CHECK (
    owner_user_id <> '00000000-0000-0000-0000-000000000000'::uuid
  ),
  CONSTRAINT evidence_source_kind CHECK (
    source_kind IN ('conversation_message', 'owner_correction')
  ),
  CONSTRAINT evidence_source_shape CHECK (
    (source_kind = 'conversation_message'
      AND source_message_id IS NOT NULL
      AND source_thread_id IS NOT NULL
      AND source_window_id IS NOT NULL
      AND source_window_sha256 IS NOT NULL
      AND bridge_source_binding_sha256 IS NOT NULL
      AND ingest_receipt_sha256 IS NOT NULL
      AND selected_start_utf8 IS NOT NULL
      AND selected_end_utf8 > selected_start_utf8
      AND selected_start_utf8 >= 0
      AND (context_message_id IS NULL) = (context_sha256 IS NULL))
    OR
    (source_kind = 'owner_correction'
      AND source_message_id IS NOT NULL
      AND source_thread_id IS NOT NULL
      AND source_window_id IS NOT NULL
      AND source_window_sha256 IS NOT NULL
      AND bridge_source_binding_sha256 IS NULL
      AND ingest_receipt_sha256 IS NULL
      AND selected_start_utf8 = 0
      AND selected_end_utf8 > 0
      AND context_message_id IS NULL
      AND context_sha256 IS NULL
      AND selected_sha256 = source_sha256)
  ),
  CONSTRAINT evidence_hashes CHECK (
    source_sha256 ~ '^[0-9a-f]{64}$'
    AND selected_sha256 ~ '^[0-9a-f]{64}$'
    AND selection_binding_sha256 ~ '^[0-9a-f]{64}$'
    AND (
      bridge_source_binding_sha256 IS NULL
      OR bridge_source_binding_sha256 ~ '^[0-9a-f]{64}$'
    )
    AND (
      ingest_receipt_sha256 IS NULL
      OR ingest_receipt_sha256 ~ '^[0-9a-f]{64}$'
    )
    AND (
      source_window_sha256 IS NULL
      OR source_window_sha256 ~ '^[0-9a-f]{64}$'
    )
    AND (context_sha256 IS NULL OR context_sha256 ~ '^[0-9a-f]{64}$')
    AND eligibility_policy_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT evidence_route_shape CHECK (
    (source_kind = 'conversation_message'
      AND eligibility_decision = 'send_external')
    OR
    (source_kind = 'owner_correction'
      AND eligibility_decision = 'route_internal')
  ),
  CONSTRAINT evidence_excerpt_bounded CHECK (
    (review_excerpt IS NULL AND excerpt_expires_at IS NULL)
    OR
    (review_excerpt IS NOT NULL
      AND pg_catalog.octet_length(review_excerpt) BETWEEN 1 AND 16000
      AND pg_catalog.octet_length(review_excerpt) =
        selected_end_utf8 - selected_start_utf8
      AND pg_catalog.encode(pg_catalog.sha256(
        pg_catalog.convert_to(review_excerpt, 'UTF8')
      ), 'hex') = selected_sha256
      AND excerpt_expires_at > created_at)
  )
);

CREATE INDEX evidence_owner_source_idx
  ON memory.evidence(owner_user_id, source_created_at DESC, evidence_id);
CREATE INDEX evidence_excerpt_expiry_idx
  ON memory.evidence(excerpt_expires_at)
  WHERE review_excerpt IS NOT NULL;

CREATE TABLE memory.extraction_job (
  job_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  idempotency_sha256 text NOT NULL,
  state text NOT NULL DEFAULT 'pending',
  attempt_count integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 3,
  available_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  lease_token uuid,
  claimed_by text,
  claimed_at timestamptz,
  lease_expires_at timestamptz,
  completed_at timestamptz,
  last_error_code text,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CONSTRAINT extraction_job_owner_id UNIQUE (owner_user_id, job_id),
  CONSTRAINT extraction_job_evidence_unique UNIQUE (owner_user_id, evidence_id),
  CONSTRAINT extraction_job_operation_unique UNIQUE (owner_user_id, operation_id),
  CONSTRAINT extraction_job_idempotency_unique UNIQUE (
    owner_user_id, idempotency_sha256
  ),
  CONSTRAINT extraction_job_evidence_fk FOREIGN KEY (
    owner_user_id, evidence_id
  ) REFERENCES memory.evidence(owner_user_id, evidence_id) ON DELETE RESTRICT,
  CONSTRAINT extraction_job_hash CHECK (
    idempotency_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT extraction_job_state CHECK (
    state IN (
      'pending', 'claimed', 'retryable', 'completed', 'failed_terminal'
    )
  ),
  CONSTRAINT extraction_job_attempts CHECK (
    max_attempts BETWEEN 1 AND 5
    AND attempt_count BETWEEN 0 AND max_attempts
  ),
  CONSTRAINT extraction_job_lease_shape CHECK (
    (state = 'claimed'
      AND lease_token IS NOT NULL
      AND claimed_by IS NOT NULL
      AND claimed_at IS NOT NULL
      AND lease_expires_at > claimed_at)
    OR
    (state <> 'claimed'
      AND lease_token IS NULL
      AND claimed_by IS NULL
      AND claimed_at IS NULL
      AND lease_expires_at IS NULL)
  ),
  CONSTRAINT extraction_job_terminal_shape CHECK (
    (state IN ('completed', 'failed_terminal') AND completed_at IS NOT NULL)
    OR
    (state NOT IN ('completed', 'failed_terminal') AND completed_at IS NULL)
  ),
  CONSTRAINT extraction_job_error_size CHECK (
    last_error_code IS NULL
    OR (
      pg_catalog.octet_length(last_error_code) BETWEEN 1 AND 128
      AND last_error_code ~ '^[a-z][a-z0-9_]{0,127}$'
    )
  ),
  CONSTRAINT extraction_job_error_semantics CHECK (
    (state IN ('pending', 'claimed', 'completed')
      AND last_error_code IS NULL)
    OR
    (state = 'retryable'
      AND last_error_code IN (
        'adapter_rejected_before_send',
        'connection_failed_before_send',
        'evidence_excerpt_expired_before_dispatch',
        'lease_expired_before_dispatch',
        'local_serialization_failed_before_send',
        'provider_proved_not_accepted'
      ))
    OR
    (state = 'failed_terminal'
      AND last_error_code IN (
        'adapter_rejected_before_send',
        'connection_failed_before_send',
        'connection_reset_after_dispatch',
        'dispatch_crash',
        'evidence_excerpt_expired_before_dispatch',
        'invalid_provider_output',
        'lease_expired_after_dispatch',
        'lease_expired_before_dispatch',
        'local_serialization_failed_before_send',
        'provider_auth_rejected',
        'provider_outcome_unknown',
        'provider_proved_not_accepted',
        'provider_request_rejected',
        'provider_timeout_after_dispatch',
        'provider_usage_contract_violation',
        'response_persistence_failed_after_dispatch',
        'response_schema_violation'
      ))
  )
);

CREATE INDEX extraction_job_lease_idx
  ON memory.extraction_job(state, available_at, created_at, job_id)
  WHERE state IN ('pending', 'retryable', 'claimed');

CREATE TABLE memory.provider_call (
  provider_call_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  job_id uuid NOT NULL,
  attempt_number integer NOT NULL,
  operation_id uuid NOT NULL,
  idempotency_sha256 text NOT NULL,
  provider text NOT NULL,
  model text NOT NULL,
  schema_sha256 text NOT NULL,
  selected_sha256 text NOT NULL,
  selection_binding_sha256 text NOT NULL,
  predicate_catalog_sha256 text NOT NULL,
  operation text NOT NULL,
  privacy_manifest_sha256 text NOT NULL,
  max_output_tokens integer NOT NULL,
  timeout_ms integer NOT NULL,
  state text NOT NULL DEFAULT 'reserved',
  request_sha256 text,
  response_sha256 text,
  input_tokens integer,
  output_tokens integer,
  error_code text,
  reserved_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  dispatched_at timestamptz,
  completed_at timestamptz,
  CONSTRAINT provider_call_owner_id UNIQUE (owner_user_id, provider_call_id),
  CONSTRAINT provider_call_attempt_unique UNIQUE (
    owner_user_id, job_id, attempt_number
  ),
  CONSTRAINT provider_call_operation_unique UNIQUE (owner_user_id, operation_id),
  CONSTRAINT provider_call_idempotency_unique UNIQUE (
    owner_user_id, idempotency_sha256
  ),
  CONSTRAINT provider_call_job_fk FOREIGN KEY (owner_user_id, job_id)
    REFERENCES memory.extraction_job(owner_user_id, job_id) ON DELETE RESTRICT,
  CONSTRAINT provider_call_names CHECK (
    pg_catalog.octet_length(provider) BETWEEN 1 AND 64
    AND pg_catalog.octet_length(model) BETWEEN 1 AND 128
    AND pg_catalog.octet_length(operation) BETWEEN 1 AND 64
  ),
  CONSTRAINT provider_call_hashes CHECK (
    idempotency_sha256 ~ '^[0-9a-f]{64}$'
    AND schema_sha256 ~ '^[0-9a-f]{64}$'
    AND selected_sha256 ~ '^[0-9a-f]{64}$'
    AND selection_binding_sha256 ~ '^[0-9a-f]{64}$'
    AND predicate_catalog_sha256 ~ '^[0-9a-f]{64}$'
    AND privacy_manifest_sha256 ~ '^[0-9a-f]{64}$'
    AND (request_sha256 IS NULL OR request_sha256 ~ '^[0-9a-f]{64}$')
    AND (response_sha256 IS NULL OR response_sha256 ~ '^[0-9a-f]{64}$')
  ),
  CONSTRAINT provider_call_bounds CHECK (
    attempt_number BETWEEN 1 AND 5
    AND max_output_tokens BETWEEN 1 AND 8192
    AND timeout_ms BETWEEN 1000 AND 120000
    AND (input_tokens IS NULL OR input_tokens BETWEEN 0 AND 100000)
    AND (
      output_tokens IS NULL
      OR output_tokens BETWEEN 0 AND max_output_tokens
    )
  ),
  CONSTRAINT provider_call_state CHECK (
    state IN (
      'reserved', 'dispatched', 'completed', 'retryable_failure',
      'terminal_failure', 'outcome_unknown'
    )
  ),
  CONSTRAINT provider_call_completion_shape CHECK (
    (state = 'reserved'
      AND request_sha256 IS NULL
      AND response_sha256 IS NULL
      AND dispatched_at IS NULL
      AND completed_at IS NULL
      AND error_code IS NULL)
    OR
    (state = 'dispatched'
      AND request_sha256 IS NOT NULL
      AND response_sha256 IS NULL
      AND dispatched_at IS NOT NULL
      AND completed_at IS NULL
      AND error_code IS NULL)
    OR
    (state = 'completed' AND input_tokens IS NOT NULL
      AND output_tokens IS NOT NULL
      AND request_sha256 IS NOT NULL
      AND response_sha256 IS NOT NULL
      AND dispatched_at IS NOT NULL
      AND completed_at IS NOT NULL
      AND error_code IS NULL)
    OR
    (state = 'retryable_failure'
      AND request_sha256 IS NULL
      AND response_sha256 IS NULL
      AND input_tokens IS NULL
      AND output_tokens IS NULL
      AND dispatched_at IS NULL
      AND completed_at IS NOT NULL
      AND error_code IS NOT NULL)
    OR
    (state = 'terminal_failure'
      AND request_sha256 IS NOT NULL
      AND response_sha256 IS NULL
      AND input_tokens IS NULL
      AND output_tokens IS NULL
      AND dispatched_at IS NOT NULL
      AND completed_at IS NOT NULL
      AND error_code IS NOT NULL
    )
    OR
    (state = 'outcome_unknown'
      AND request_sha256 IS NOT NULL
      AND response_sha256 IS NULL
      AND input_tokens IS NULL
      AND output_tokens IS NULL
      AND dispatched_at IS NOT NULL
      AND completed_at IS NOT NULL
      AND error_code IS NOT NULL)
  ),
  CONSTRAINT provider_call_error_size CHECK (
    error_code IS NULL
    OR (
      pg_catalog.octet_length(error_code) BETWEEN 1 AND 128
      AND error_code ~ '^[a-z][a-z0-9_]{0,127}$'
    )
  ),
  CONSTRAINT provider_call_retry_semantics CHECK (
    state <> 'retryable_failure'
    OR error_code IN (
      'adapter_rejected_before_send',
      'connection_failed_before_send',
      'evidence_excerpt_expired_before_dispatch',
      'local_serialization_failed_before_send',
      'provider_proved_not_accepted',
      'lease_expired_before_dispatch'
    )
  ),
  CONSTRAINT provider_call_terminal_semantics CHECK (
    state <> 'terminal_failure'
    OR error_code IN (
      'invalid_provider_output',
      'provider_auth_rejected',
      'provider_request_rejected',
      'provider_usage_contract_violation',
      'response_schema_violation'
    )
  ),
  CONSTRAINT provider_call_unknown_semantics CHECK (
    state <> 'outcome_unknown'
    OR error_code IN (
      'connection_reset_after_dispatch',
      'dispatch_crash',
      'lease_expired_after_dispatch',
      'provider_timeout_after_dispatch',
      'response_persistence_failed_after_dispatch'
    )
  )
);

CREATE TABLE memory.entity (
  entity_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  entity_key text NOT NULL,
  entity_type text NOT NULL,
  display_name text NOT NULL,
  normalized_name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CONSTRAINT entity_owner_id UNIQUE (owner_user_id, entity_id),
  CONSTRAINT entity_owner_key UNIQUE (owner_user_id, entity_key),
  CONSTRAINT entity_key_format CHECK (
    entity_key = pg_catalog.btrim(entity_key)
    AND entity_key = normalize(entity_key, NFC)
    AND pg_catalog.octet_length(entity_key) BETWEEN 1 AND 200
  ),
  CONSTRAINT entity_type CHECK (
    entity_type IN ('self', 'person', 'pet', 'organization', 'place', 'other')
  ),
  CONSTRAINT entity_names CHECK (
    pg_catalog.octet_length(display_name) BETWEEN 1 AND 256
    AND display_name = normalize(display_name, NFC)
    AND pg_catalog.octet_length(normalized_name) BETWEEN 1 AND 256
    AND normalized_name = normalize(normalized_name, NFC)
  )
);

CREATE TABLE memory.claim (
  claim_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  semantic_key_sha256 text NOT NULL,
  claim_identity_sha256 text NOT NULL,
  current_state_sha256 text NOT NULL,
  lifecycle_state text NOT NULL,
  current_revision_id uuid NOT NULL,
  current_revision_number integer NOT NULL,
  projection_sequence integer NOT NULL DEFAULT 0,
  correction_pending_at timestamptz,
  retracted_at timestamptz,
  deletion_requested_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CONSTRAINT claim_owner_id UNIQUE (owner_user_id, claim_id),
  CONSTRAINT claim_semantic_unique UNIQUE (owner_user_id, semantic_key_sha256),
  CONSTRAINT claim_hash CHECK (
    semantic_key_sha256 ~ '^[0-9a-f]{64}$'
    AND claim_identity_sha256 ~ '^[0-9a-f]{64}$'
    AND current_state_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT claim_revision_number CHECK (current_revision_number > 0),
  CONSTRAINT claim_projection_sequence CHECK (projection_sequence >= 0),
  CONSTRAINT claim_lifecycle CHECK (
    lifecycle_state IN (
      'active', 'correction_pending', 'retracted', 'deletion_pending'
    )
  ),
  CONSTRAINT claim_lifecycle_shape CHECK (
    (lifecycle_state = 'active'
      AND correction_pending_at IS NULL
      AND retracted_at IS NULL
      AND deletion_requested_at IS NULL)
    OR
    (lifecycle_state = 'correction_pending'
      AND correction_pending_at IS NOT NULL
      AND retracted_at IS NULL
      AND deletion_requested_at IS NULL)
    OR
    (lifecycle_state = 'retracted'
      AND retracted_at IS NOT NULL
      AND deletion_requested_at IS NULL)
    OR
    (lifecycle_state = 'deletion_pending'
      AND deletion_requested_at IS NOT NULL)
  )
);

CREATE TABLE memory.proposal (
  proposal_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  provider_call_id uuid,
  operation_id uuid NOT NULL,
  proposal_purpose text NOT NULL,
  fact_index smallint,
  proposal_sha256 text NOT NULL,
  semantic_key_sha256 text NOT NULL,
  selected_sha256 text NOT NULL,
  selection_binding_sha256 text NOT NULL,
  predicate_catalog_sha256 text NOT NULL,
  subject_entity_key text NOT NULL,
  subject_entity_type text NOT NULL,
  subject_display_name text,
  predicate text NOT NULL,
  object_kind text NOT NULL,
  object_entity_key text,
  object_entity_type text,
  object_display_name text,
  object_literal jsonb,
  epistemic_state text NOT NULL,
  projectable boolean NOT NULL,
  sensitivity text NOT NULL,
  domains text[] NOT NULL DEFAULT ARRAY[]::text[],
  intents text[] NOT NULL DEFAULT ARRAY[]::text[],
  surface text NOT NULL,
  requires_explicit boolean NOT NULL DEFAULT false,
  valid_from timestamptz,
  valid_to timestamptz,
  correction_of_claim_id uuid,
  correction_target_revision_id uuid,
  correction_target_revision_number integer,
  expected_revision_sha256 text,
  correction_target_state_sha256 text,
  correction_target_identity_sha256 text,
  correction_target_projection_sequence integer,
  correction_pending_state_sha256 text,
  review_state text NOT NULL DEFAULT 'pending_review',
  reviewer_kind text,
  reviewer_user_id uuid,
  review_reason_codes text[],
  reviewed_at timestamptz,
  expires_at timestamptz NOT NULL,
  purge_after timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CONSTRAINT proposal_owner_id UNIQUE (owner_user_id, proposal_id),
  CONSTRAINT proposal_operation_unique UNIQUE (owner_user_id, operation_id),
  CONSTRAINT proposal_exact_unique UNIQUE (
    owner_user_id, evidence_id, proposal_sha256
  ),
  CONSTRAINT proposal_evidence_fk FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id) ON DELETE RESTRICT,
  CONSTRAINT proposal_provider_call_fk FOREIGN KEY (
    owner_user_id, provider_call_id
  ) REFERENCES memory.provider_call(owner_user_id, provider_call_id)
    ON DELETE RESTRICT,
  CONSTRAINT proposal_correction_claim_fk FOREIGN KEY (
    owner_user_id, correction_of_claim_id
  ) REFERENCES memory.claim(owner_user_id, claim_id) ON DELETE RESTRICT,
  CONSTRAINT proposal_predicate_fk FOREIGN KEY (
    predicate_catalog_sha256, predicate
  ) REFERENCES memory.predicate_catalog(catalog_sha256, predicate)
    ON DELETE RESTRICT,
  CONSTRAINT proposal_hashes CHECK (
    proposal_sha256 ~ '^[0-9a-f]{64}$'
    AND semantic_key_sha256 ~ '^[0-9a-f]{64}$'
    AND selected_sha256 ~ '^[0-9a-f]{64}$'
    AND selection_binding_sha256 ~ '^[0-9a-f]{64}$'
    AND predicate_catalog_sha256 ~ '^[0-9a-f]{64}$'
    AND (
      expected_revision_sha256 IS NULL
      OR expected_revision_sha256 ~ '^[0-9a-f]{64}$'
    )
    AND (
      correction_target_state_sha256 IS NULL
      OR correction_target_state_sha256 ~ '^[0-9a-f]{64}$'
    )
    AND (
      correction_target_identity_sha256 IS NULL
      OR correction_target_identity_sha256 ~ '^[0-9a-f]{64}$'
    )
    AND (
      correction_pending_state_sha256 IS NULL
      OR correction_pending_state_sha256 ~ '^[0-9a-f]{64}$'
    )
  ),
  CONSTRAINT proposal_entity_fields CHECK (
    pg_catalog.octet_length(subject_entity_key) BETWEEN 1 AND 200
    AND subject_entity_key = normalize(subject_entity_key, NFC)
    AND subject_entity_type IN (
      'self', 'person', 'pet', 'organization', 'place', 'other'
    )
    AND (
      (subject_entity_type = 'self'
        AND subject_entity_key = 'self'
        AND subject_display_name IS NULL)
      OR
      (subject_entity_type <> 'self'
        AND subject_entity_key <> 'self'
        AND subject_display_name IS NOT NULL
        AND pg_catalog.octet_length(subject_display_name) BETWEEN 1 AND 256
        AND subject_display_name = normalize(subject_display_name, NFC))
    )
  ),
  CONSTRAINT proposal_object_shape CHECK (
    (object_kind = 'literal'
      AND pg_catalog.jsonb_typeof(object_literal) = 'string'
      AND pg_catalog.octet_length(object_literal #>> '{}') BETWEEN 1 AND 2000
      AND object_literal #>> '{}' = normalize(object_literal #>> '{}', NFC)
      AND object_entity_key IS NULL
      AND object_entity_type IS NULL
      AND object_display_name IS NULL)
    OR
    (object_kind = 'entity'
      AND object_literal IS NULL
      AND object_entity_key IS NOT NULL
      AND object_entity_key = normalize(object_entity_key, NFC)
      AND pg_catalog.octet_length(object_entity_key) BETWEEN 1 AND 200
      AND object_entity_type IN (
        'self', 'person', 'pet', 'organization', 'place', 'other'
      )
      AND object_display_name IS NOT NULL
      AND object_display_name = normalize(object_display_name, NFC)
      AND pg_catalog.octet_length(object_display_name) BETWEEN 1 AND 256)
  ),
  CONSTRAINT proposal_content_bounds CHECK (
    pg_catalog.pg_column_size(object_literal) <= 16384
  ),
  CONSTRAINT proposal_epistemic CHECK (
    epistemic_state IN ('supported', 'uncertain', 'disputed')
  ),
  CONSTRAINT proposal_policy CHECK (
    sensitivity IN (
      'ordinary', 'sensitive_self', 'sensitive_third_party'
    )
    AND surface IN ('normal', 'explicit_only', 'never')
    AND (surface <> 'never' OR NOT projectable)
    AND (NOT requires_explicit OR surface = 'explicit_only')
    AND (
      sensitivity = 'ordinary'
      OR (
        sensitivity IN ('sensitive_self', 'sensitive_third_party')
        AND requires_explicit
        AND surface = 'explicit_only'
      )
    )
    AND pg_catalog.cardinality(domains) BETWEEN 0 AND 16
    AND pg_catalog.cardinality(intents) BETWEEN 0 AND 16
    AND pg_catalog.array_position(domains, NULL) IS NULL
    AND pg_catalog.array_position(intents, NULL) IS NULL
  ),
  CONSTRAINT proposal_temporal CHECK (
    valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from
  ),
  CONSTRAINT proposal_correction_shape CHECK (
    (proposal_purpose = 'new_claim'
      AND correction_of_claim_id IS NULL
      AND correction_target_revision_id IS NULL
      AND correction_target_revision_number IS NULL
      AND expected_revision_sha256 IS NULL
      AND correction_target_state_sha256 IS NULL
      AND correction_target_identity_sha256 IS NULL
      AND correction_target_projection_sequence IS NULL
      AND correction_pending_state_sha256 IS NULL
      AND provider_call_id IS NOT NULL
      AND fact_index BETWEEN 0 AND 7)
    OR
    (proposal_purpose = 'correction'
      AND correction_of_claim_id IS NOT NULL
      AND correction_target_revision_id IS NOT NULL
      AND correction_target_revision_number > 0
      AND expected_revision_sha256 IS NOT NULL
      AND correction_target_state_sha256 IS NOT NULL
      AND correction_target_identity_sha256 IS NOT NULL
      AND correction_target_projection_sequence >= 0
      AND correction_pending_state_sha256 IS NOT NULL
      AND provider_call_id IS NULL
      AND fact_index IS NULL)
  ),
  CONSTRAINT proposal_review_state CHECK (
    review_state IN ('pending_review', 'admitted', 'rejected', 'expired')
  ),
  CONSTRAINT proposal_review_shape CHECK (
    (review_state = 'pending_review'
      AND reviewer_kind IS NULL
      AND reviewer_user_id IS NULL
      AND review_reason_codes IS NULL
      AND reviewed_at IS NULL)
    OR
    (review_state = 'admitted'
      AND reviewer_kind = 'owner'
      AND reviewer_user_id IS NOT NULL
      AND review_reason_codes = ARRAY['explicit_owner_review']::text[]
      AND reviewed_at IS NOT NULL)
    OR
    (review_state = 'rejected'
      AND reviewer_kind = 'owner'
      AND reviewer_user_id IS NOT NULL
      AND memory_private.is_sorted_unique_allowlist(
        review_reason_codes,
        ARRAY[
          'duplicate_existing', 'not_durable', 'proposal_incorrect'
        ]::text[]
      )
      AND reviewed_at IS NOT NULL)
    OR
    (review_state = 'rejected'
      AND reviewer_kind = 'system'
      AND reviewer_user_id IS NULL
      AND review_reason_codes = ARRAY['lifecycle_override']::text[]
      AND reviewed_at IS NOT NULL)
    OR
    (review_state = 'expired'
      AND reviewer_kind = 'system'
      AND reviewer_user_id IS NULL
      AND review_reason_codes = ARRAY['proposal_expired']::text[]
      AND reviewed_at IS NOT NULL)
  ),
  CONSTRAINT proposal_retention CHECK (
    expires_at > created_at
    AND expires_at <= created_at + interval '7 days'
    AND purge_after >= expires_at
    AND purge_after <= created_at + interval '30 days'
  ),
  CONSTRAINT proposal_review_reason_codes CHECK (
    review_reason_codes IS NULL
    OR (
      pg_catalog.cardinality(review_reason_codes) BETWEEN 1 AND 3
      AND memory_private.is_ascii_key_array(review_reason_codes)
      AND memory_private.is_sorted_unique_allowlist(
        review_reason_codes,
        ARRAY[
          'duplicate_existing', 'explicit_owner_review',
          'lifecycle_override', 'not_durable', 'proposal_expired',
          'proposal_incorrect'
        ]::text[]
      )
    )
  )
);

CREATE INDEX proposal_review_idx
  ON memory.proposal(owner_user_id, review_state, expires_at, proposal_id);
CREATE UNIQUE INDEX proposal_provider_fact_unique
  ON memory.proposal(owner_user_id, evidence_id, fact_index)
  WHERE fact_index IS NOT NULL;
CREATE INDEX proposal_provider_call_idx
  ON memory.proposal(owner_user_id, provider_call_id)
  WHERE provider_call_id IS NOT NULL;
CREATE INDEX proposal_correction_revision_idx
  ON memory.proposal(
    owner_user_id, correction_of_claim_id, correction_target_revision_id
  ) WHERE correction_of_claim_id IS NOT NULL;
CREATE INDEX proposal_catalog_predicate_idx
  ON memory.proposal(predicate_catalog_sha256, predicate);

CREATE TABLE memory.claim_revision (
  revision_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  revision_number integer NOT NULL,
  source_proposal_id uuid NOT NULL,
  semantic_key_sha256 text NOT NULL,
  claim_identity_sha256 text NOT NULL,
  subject_entity_id uuid NOT NULL,
  subject_entity_type text NOT NULL,
  subject_entity_key text NOT NULL,
  subject_display_name text,
  predicate text NOT NULL,
  object_kind text NOT NULL,
  object_entity_id uuid,
  object_entity_type text,
  object_entity_key text,
  object_display_name text,
  object_literal jsonb,
  retrieval_text text NOT NULL,
  retrieval_text_sha256 text NOT NULL,
  epistemic_state text NOT NULL,
  projectable boolean NOT NULL,
  sensitivity text NOT NULL,
  domains text[] NOT NULL DEFAULT ARRAY[]::text[],
  intents text[] NOT NULL DEFAULT ARRAY[]::text[],
  surface text NOT NULL,
  requires_explicit boolean NOT NULL DEFAULT false,
  valid_from timestamptz,
  valid_to timestamptz,
  source_sha256 text NOT NULL,
  selected_sha256 text NOT NULL,
  selection_binding_sha256 text NOT NULL,
  predicate_catalog_sha256 text NOT NULL,
  fact_policy_sha256 text NOT NULL,
  revision_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CONSTRAINT claim_revision_owner_id UNIQUE (owner_user_id, revision_id),
  CONSTRAINT claim_revision_owner_claim_id UNIQUE (
    owner_user_id, claim_id, revision_id
  ),
  CONSTRAINT claim_revision_number_unique UNIQUE (
    owner_user_id, claim_id, revision_number
  ),
  CONSTRAINT claim_revision_proposal_unique UNIQUE (
    owner_user_id, source_proposal_id
  ),
  CONSTRAINT claim_revision_claim_fk FOREIGN KEY (owner_user_id, claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id) ON DELETE RESTRICT,
  CONSTRAINT claim_revision_proposal_fk FOREIGN KEY (
    owner_user_id, source_proposal_id
  ) REFERENCES memory.proposal(owner_user_id, proposal_id) ON DELETE NO ACTION
    DEFERRABLE INITIALLY DEFERRED,
  CONSTRAINT claim_revision_subject_fk FOREIGN KEY (
    owner_user_id, subject_entity_id
  ) REFERENCES memory.entity(owner_user_id, entity_id) ON DELETE RESTRICT,
  CONSTRAINT claim_revision_object_fk FOREIGN KEY (
    owner_user_id, object_entity_id
  ) REFERENCES memory.entity(owner_user_id, entity_id) ON DELETE RESTRICT,
  CONSTRAINT claim_revision_predicate_fk FOREIGN KEY (
    predicate_catalog_sha256, predicate
  ) REFERENCES memory.predicate_catalog(catalog_sha256, predicate)
    ON DELETE RESTRICT,
  CONSTRAINT claim_revision_number CHECK (revision_number > 0),
  CONSTRAINT claim_revision_subject_shape CHECK (
    subject_entity_type IN (
      'self', 'person', 'pet', 'organization', 'place', 'other'
    )
    AND subject_entity_key = normalize(subject_entity_key, NFC)
    AND pg_catalog.octet_length(subject_entity_key) BETWEEN 1 AND 200
    AND (
      (subject_entity_type = 'self'
        AND subject_entity_key = 'self'
        AND subject_display_name IS NULL)
      OR
      (subject_entity_type <> 'self'
        AND subject_entity_key <> 'self'
        AND subject_display_name IS NOT NULL
        AND pg_catalog.octet_length(subject_display_name) BETWEEN 1 AND 256
        AND subject_display_name = normalize(subject_display_name, NFC))
    )
  ),
  CONSTRAINT claim_revision_object_shape CHECK (
    (object_kind = 'literal'
      AND object_entity_id IS NULL
      AND object_entity_type IS NULL
      AND object_entity_key IS NULL
      AND object_display_name IS NULL
      AND pg_catalog.jsonb_typeof(object_literal) = 'string'
      AND pg_catalog.octet_length(object_literal #>> '{}') BETWEEN 1 AND 2000
      AND object_literal #>> '{}' = normalize(object_literal #>> '{}', NFC))
    OR
    (object_kind = 'entity'
      AND object_entity_id IS NOT NULL
      AND object_entity_type IN (
        'self', 'person', 'pet', 'organization', 'place', 'other'
      )
      AND object_entity_key IS NOT NULL
      AND object_entity_key = normalize(object_entity_key, NFC)
      AND pg_catalog.octet_length(object_entity_key) BETWEEN 1 AND 200
      AND object_display_name IS NOT NULL
      AND object_display_name = normalize(object_display_name, NFC)
      AND pg_catalog.octet_length(object_display_name) BETWEEN 1 AND 256
      AND object_literal IS NULL)
  ),
  CONSTRAINT claim_revision_content CHECK (
    pg_catalog.pg_column_size(object_literal) <= 16384
    AND pg_catalog.octet_length(retrieval_text) BETWEEN 1 AND 4096
  ),
  CONSTRAINT claim_revision_epistemic CHECK (
    epistemic_state IN ('supported', 'uncertain', 'disputed')
  ),
  CONSTRAINT claim_revision_policy CHECK (
    sensitivity IN (
      'ordinary', 'sensitive_self', 'sensitive_third_party'
    )
    AND surface IN ('normal', 'explicit_only', 'never')
    AND (surface <> 'never' OR NOT projectable)
    AND (NOT requires_explicit OR surface = 'explicit_only')
    AND (
      sensitivity = 'ordinary'
      OR (
        sensitivity IN ('sensitive_self', 'sensitive_third_party')
        AND requires_explicit
        AND surface = 'explicit_only'
      )
    )
    AND pg_catalog.cardinality(domains) BETWEEN 0 AND 16
    AND pg_catalog.cardinality(intents) BETWEEN 0 AND 16
  ),
  CONSTRAINT claim_revision_temporal CHECK (
    valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from
  ),
  CONSTRAINT claim_revision_hashes CHECK (
    semantic_key_sha256 ~ '^[0-9a-f]{64}$'
    AND claim_identity_sha256 ~ '^[0-9a-f]{64}$'
    AND source_sha256 ~ '^[0-9a-f]{64}$'
    AND selected_sha256 ~ '^[0-9a-f]{64}$'
    AND selection_binding_sha256 ~ '^[0-9a-f]{64}$'
    AND predicate_catalog_sha256 ~ '^[0-9a-f]{64}$'
    AND fact_policy_sha256 ~ '^[0-9a-f]{64}$'
    AND retrieval_text_sha256 ~ '^[0-9a-f]{64}$'
    AND revision_sha256 ~ '^[0-9a-f]{64}$'
  )
);

ALTER TABLE memory.claim
  ADD CONSTRAINT claim_current_revision_fk FOREIGN KEY (
    owner_user_id, claim_id, current_revision_id
  ) REFERENCES memory.claim_revision(owner_user_id, claim_id, revision_id)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE memory.proposal
  ADD CONSTRAINT proposal_correction_revision_fk FOREIGN KEY (
    owner_user_id, correction_of_claim_id, correction_target_revision_id
  ) REFERENCES memory.claim_revision(owner_user_id, claim_id, revision_id)
    ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX claim_revision_subject_idx
  ON memory.claim_revision(owner_user_id, subject_entity_id);
CREATE INDEX claim_revision_object_idx
  ON memory.claim_revision(owner_user_id, object_entity_id)
  WHERE object_entity_id IS NOT NULL;
CREATE INDEX claim_revision_catalog_predicate_idx
  ON memory.claim_revision(predicate_catalog_sha256, predicate);

CREATE TABLE memory.claim_evidence (
  owner_user_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  selected_sha256 text NOT NULL,
  selection_binding_sha256 text NOT NULL,
  stance text NOT NULL,
  reason_codes text[] NOT NULL DEFAULT ARRAY[]::text[],
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  PRIMARY KEY (owner_user_id, revision_id, evidence_id),
  CONSTRAINT claim_evidence_revision_fk FOREIGN KEY (
    owner_user_id, claim_id, revision_id
  ) REFERENCES memory.claim_revision(owner_user_id, claim_id, revision_id)
    ON DELETE RESTRICT,
  CONSTRAINT claim_evidence_evidence_fk FOREIGN KEY (
    owner_user_id, evidence_id
  ) REFERENCES memory.evidence(owner_user_id, evidence_id) ON DELETE RESTRICT,
  CONSTRAINT claim_evidence_hashes CHECK (
    selected_sha256 ~ '^[0-9a-f]{64}$'
    AND selection_binding_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT claim_evidence_stance CHECK (
    stance IN ('supports', 'opposes', 'qualifies', 'context')
  ),
  CONSTRAINT claim_evidence_reason_codes CHECK (
    pg_catalog.cardinality(reason_codes) BETWEEN 1 AND 3
    AND memory_private.is_ascii_key_array(reason_codes)
    AND memory_private.is_sorted_unique_allowlist(
      reason_codes,
      ARRAY[
        'duplicate_existing', 'explicit_owner_review', 'not_durable',
        'proposal_incorrect'
      ]::text[]
    )
  )
);

CREATE INDEX claim_evidence_source_idx
  ON memory.claim_evidence(owner_user_id, evidence_id, revision_id);
CREATE INDEX claim_evidence_revision_idx
  ON memory.claim_evidence(owner_user_id, claim_id, revision_id);

CREATE TABLE memory.projection_outbox (
  outbox_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  sequence_number integer NOT NULL,
  operation text NOT NULL,
  point_id uuid NOT NULL,
  revision_sha256 text NOT NULL,
  selection_binding_sha256 text NOT NULL,
  projection_manifest_sha256 text NOT NULL,
  collection_alias text NOT NULL DEFAULT 'governed_memory_active',
  state text NOT NULL DEFAULT 'pending',
  attempt_count integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 8,
  available_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  lease_token uuid,
  claimed_by text,
  claimed_at timestamptz,
  lease_expires_at timestamptz,
  physical_collection_name text,
  vector_sha256 text,
  verification_sha256 text,
  verification_receipt_sha256 text,
  verified_at timestamptz,
  last_error_code text,
  applied_at timestamptz,
  completion_lease_token uuid,
  completion_outcome text,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CONSTRAINT projection_outbox_owner_id UNIQUE (owner_user_id, outbox_id),
  CONSTRAINT projection_outbox_operation_unique UNIQUE (
    owner_user_id, operation_id
  ),
  CONSTRAINT projection_outbox_sequence_unique UNIQUE (
    owner_user_id, claim_id, sequence_number
  ),
  CONSTRAINT projection_outbox_claim_fk FOREIGN KEY (
    owner_user_id, claim_id
  ) REFERENCES memory.claim(owner_user_id, claim_id) ON DELETE RESTRICT,
  CONSTRAINT projection_outbox_revision_fk FOREIGN KEY (
    owner_user_id, claim_id, revision_id
  ) REFERENCES memory.claim_revision(owner_user_id, claim_id, revision_id)
    ON DELETE RESTRICT,
  CONSTRAINT projection_outbox_point CHECK (point_id = claim_id),
  CONSTRAINT projection_outbox_operation CHECK (
    operation IN ('upsert', 'delete')
  ),
  CONSTRAINT projection_outbox_hashes CHECK (
    revision_sha256 ~ '^[0-9a-f]{64}$'
    AND selection_binding_sha256 ~ '^[0-9a-f]{64}$'
    AND projection_manifest_sha256 ~ '^[0-9a-f]{64}$'
    AND (vector_sha256 IS NULL OR vector_sha256 ~ '^[0-9a-f]{64}$')
    AND (
      verification_sha256 IS NULL
      OR verification_sha256 ~ '^[0-9a-f]{64}$'
    )
    AND (
      verification_receipt_sha256 IS NULL
      OR verification_receipt_sha256 ~ '^[0-9a-f]{64}$'
    )
  ),
  CONSTRAINT projection_outbox_alias CHECK (
    collection_alias = 'governed_memory_active'
    AND (
      physical_collection_name IS NULL
      OR physical_collection_name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
    )
  ),
  CONSTRAINT projection_outbox_state CHECK (
    state IN (
      'pending', 'claimed', 'retryable', 'applied', 'failed_terminal',
      'superseded'
    )
  ),
  CONSTRAINT projection_outbox_attempts CHECK (
    max_attempts BETWEEN 1 AND 12
    AND attempt_count BETWEEN 0 AND max_attempts
    AND sequence_number > 0
  ),
  CONSTRAINT projection_outbox_lease_shape CHECK (
    (state = 'claimed'
      AND lease_token IS NOT NULL
      AND claimed_by IS NOT NULL
      AND claimed_at IS NOT NULL
      AND lease_expires_at > claimed_at)
    OR
    (state <> 'claimed'
      AND lease_token IS NULL
      AND claimed_by IS NULL
      AND claimed_at IS NULL
      AND lease_expires_at IS NULL)
  ),
  CONSTRAINT projection_outbox_applied_shape CHECK (
    (state = 'applied'
      AND applied_at IS NOT NULL
      AND (
        (operation = 'upsert'
          AND verification_sha256 IS NULL
          AND verification_receipt_sha256 IS NULL
          AND verified_at IS NULL
          AND vector_sha256 IS NOT NULL
          AND physical_collection_name IS NOT NULL
          AND last_error_code IS NULL)
        OR
        (operation = 'delete'
          AND vector_sha256 IS NULL
          AND verification_sha256 IS NOT NULL
          AND verification_receipt_sha256 IS NOT NULL
          AND verified_at IS NOT NULL
          AND physical_collection_name IS NOT NULL
          AND last_error_code IS NULL)
      ))
    OR
    (state <> 'applied'
      AND applied_at IS NULL
      AND vector_sha256 IS NULL
      AND verification_sha256 IS NULL
      AND verification_receipt_sha256 IS NULL
      AND verified_at IS NULL
      AND physical_collection_name IS NULL)
  ),
  CONSTRAINT projection_outbox_error_key CHECK (
    last_error_code IS NULL
    OR last_error_code ~ '^[a-z][a-z0-9_]{0,127}$'
  ),
  CONSTRAINT projection_outbox_completion_replay_shape CHECK (
    (
      completion_lease_token IS NULL
      AND completion_outcome IS NULL
      AND state <> 'applied'
    )
    OR
    (
      completion_lease_token IS NOT NULL
      AND completion_outcome IN ('applied', 'retryable', 'failed_terminal')
      AND (
        (completion_outcome = 'applied' AND state = 'applied')
        OR
        (completion_outcome = 'retryable'
          AND state IN ('retryable', 'failed_terminal'))
        OR
        (completion_outcome = 'failed_terminal'
          AND state = 'failed_terminal')
      )
    )
  ),
  CONSTRAINT projection_outbox_error_semantics CHECK (
    (state IN ('pending', 'claimed', 'applied')
      AND last_error_code IS NULL)
    OR
    (state = 'retryable'
      AND last_error_code IN (
        'embedding_provider_unavailable', 'embedding_timeout',
        'lease_expired', 'qdrant_rate_limited', 'qdrant_timeout',
        'qdrant_unavailable', 'qdrant_verification_inconclusive'
      ))
    OR
    (state = 'failed_terminal'
      AND last_error_code IN (
        'embedding_contract_violation', 'embedding_provider_unavailable',
        'embedding_timeout', 'lease_expired',
        'projection_contract_violation', 'qdrant_collection_mismatch',
        'qdrant_dimension_mismatch', 'qdrant_rate_limited',
        'qdrant_receipt_contract_violation', 'qdrant_timeout',
        'qdrant_unavailable', 'qdrant_verification_inconclusive'
      ))
    OR
    (state = 'superseded'
      AND last_error_code = 'stale_projection_sequence')
  )
);

CREATE INDEX projection_outbox_lease_idx
  ON memory.projection_outbox(state, available_at, created_at, outbox_id)
  WHERE state IN ('pending', 'retryable', 'claimed');
CREATE INDEX projection_outbox_revision_idx
  ON memory.projection_outbox(owner_user_id, claim_id, revision_id);

CREATE TABLE memory.answer_binding (
  binding_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  response_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  query_sha256 text NOT NULL,
  policy_sha256 text NOT NULL,
  renderer_sha256 text NOT NULL,
  prompt_sha256 text NOT NULL,
  explicit_recall boolean NOT NULL,
  allowed_predicates text[] NOT NULL,
  policy_domains text[] NOT NULL DEFAULT ARRAY[]::text[],
  policy_intents text[] NOT NULL DEFAULT ARRAY[]::text[],
  policy_max_records integer NOT NULL,
  policy_revision integer NOT NULL,
  selection_manifest_sha256 text NOT NULL,
  selected_claim_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],
  selected_revision_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],
  selected_revision_sha256s text[] NOT NULL DEFAULT ARRAY[]::text[],
  injected_claim_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],
  injected_revision_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],
  injected_revision_sha256s text[] NOT NULL DEFAULT ARRAY[]::text[],
  memory_block_sha256 text,
  memory_block_utf8_bytes integer,
  escaped_memory_segment_sha256 text,
  escaped_segment_start_utf8 integer,
  escaped_segment_end_utf8 integer,
  escaped_segment_occurrence_count integer,
  outbound_request_sha256 text,
  injection_manifest_sha256 text,
  dispatched_at timestamptz,
  outcome text NOT NULL,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CONSTRAINT answer_binding_owner_id UNIQUE (owner_user_id, binding_id),
  CONSTRAINT answer_binding_operation_unique UNIQUE (owner_user_id, operation_id),
  CONSTRAINT answer_binding_response_unique UNIQUE (owner_user_id, response_id),
  CONSTRAINT answer_binding_hashes CHECK (
    query_sha256 ~ '^[0-9a-f]{64}$'
    AND policy_sha256 ~ '^[0-9a-f]{64}$'
    AND renderer_sha256 ~ '^[0-9a-f]{64}$'
    AND prompt_sha256 ~ '^[0-9a-f]{64}$'
    AND selection_manifest_sha256 ~ '^[0-9a-f]{64}$'
    AND (
      memory_block_sha256 IS NULL
      OR memory_block_sha256 ~ '^[0-9a-f]{64}$'
    )
    AND (
      escaped_memory_segment_sha256 IS NULL
      OR escaped_memory_segment_sha256 ~ '^[0-9a-f]{64}$'
    )
    AND (
      outbound_request_sha256 IS NULL
      OR outbound_request_sha256 ~ '^[0-9a-f]{64}$'
    )
    AND (
      injection_manifest_sha256 IS NULL
      OR injection_manifest_sha256 ~ '^[0-9a-f]{64}$'
    )
  ),
  CONSTRAINT answer_binding_arrays CHECK (
    pg_catalog.cardinality(selected_claim_ids)
      = pg_catalog.cardinality(selected_revision_ids)
    AND pg_catalog.cardinality(selected_claim_ids)
      = pg_catalog.cardinality(selected_revision_sha256s)
    AND pg_catalog.cardinality(selected_claim_ids) BETWEEN 0 AND 8
    AND pg_catalog.cardinality(selected_claim_ids) <= policy_max_records
    AND pg_catalog.array_position(selected_claim_ids, NULL) IS NULL
    AND pg_catalog.array_position(selected_revision_ids, NULL) IS NULL
    AND pg_catalog.array_position(selected_revision_sha256s, NULL) IS NULL
    AND pg_catalog.cardinality(injected_claim_ids)
      = pg_catalog.cardinality(injected_revision_ids)
    AND pg_catalog.cardinality(injected_claim_ids)
      = pg_catalog.cardinality(injected_revision_sha256s)
    AND pg_catalog.cardinality(injected_claim_ids) BETWEEN 0 AND 8
    AND pg_catalog.array_position(injected_claim_ids, NULL) IS NULL
    AND pg_catalog.array_position(injected_revision_ids, NULL) IS NULL
    AND pg_catalog.array_position(injected_revision_sha256s, NULL) IS NULL
  ),
  CONSTRAINT answer_binding_policy_envelope CHECK (
    memory_private.is_sorted_unique_key_array(
      allowed_predicates, 1, 8
    )
    AND memory_private.is_sorted_unique_key_array(policy_domains, 0, 16)
    AND memory_private.is_sorted_unique_key_array(policy_intents, 0, 16)
    AND policy_max_records BETWEEN 1 AND 8
    AND policy_revision = 1
    AND policy_sha256 = memory_private.retrieval_policy_sha256(
      explicit_recall, allowed_predicates, policy_domains,
      policy_intents, policy_max_records, policy_revision
    )
  ),
  CONSTRAINT answer_binding_outcome CHECK (
    outcome IN ('exposed', 'no_memory_selected')
  ),
  CONSTRAINT answer_binding_outcome_shape CHECK (
    (outcome = 'exposed'
      AND pg_catalog.cardinality(selected_claim_ids) > 0
    )
    OR
    (outcome = 'no_memory_selected'
      AND pg_catalog.cardinality(selected_claim_ids) = 0)
  ),
  CONSTRAINT answer_binding_dispatch CHECK (
    (outcome = 'exposed'
      AND pg_catalog.cardinality(injected_revision_ids) > 0
      AND memory_block_sha256 IS NOT NULL
      AND memory_block_utf8_bytes BETWEEN 1 AND 32768
      AND escaped_memory_segment_sha256 IS NOT NULL
      AND escaped_segment_start_utf8 IS NOT NULL
      AND escaped_segment_start_utf8 >= 0
      AND escaped_segment_end_utf8 IS NOT NULL
      AND escaped_segment_end_utf8 > escaped_segment_start_utf8
      AND escaped_segment_occurrence_count IS NOT NULL
      AND escaped_segment_occurrence_count = 1
      AND outbound_request_sha256 IS NOT NULL
      AND injection_manifest_sha256 IS NOT NULL
      AND dispatched_at IS NOT NULL)
    OR
    (outcome = 'no_memory_selected'
      AND pg_catalog.cardinality(injected_revision_ids) = 0
      AND memory_block_sha256 IS NULL
      AND memory_block_utf8_bytes IS NULL
      AND escaped_memory_segment_sha256 IS NULL
      AND escaped_segment_start_utf8 IS NULL
      AND escaped_segment_end_utf8 IS NULL
      AND escaped_segment_occurrence_count IS NULL
      AND outbound_request_sha256 IS NULL
      AND injection_manifest_sha256 IS NULL
      AND dispatched_at IS NULL)
  ),
  CONSTRAINT answer_binding_retention CHECK (
    expires_at = created_at + interval '90 days'
  )
);

CREATE INDEX answer_binding_owner_time_idx
  ON memory.answer_binding(owner_user_id, created_at DESC, binding_id);
CREATE INDEX answer_binding_expiry_idx
  ON memory.answer_binding(expires_at, binding_id);

CREATE TABLE memory.audit_event (
  event_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  actor_kind text NOT NULL,
  actor_user_id uuid,
  object_type text NOT NULL,
  object_id uuid NOT NULL,
  transition_code text NOT NULL,
  prior_state_sha256 text,
  new_state_sha256 text NOT NULL,
  reason_code text NOT NULL,
  event_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CONSTRAINT audit_event_owner_id UNIQUE (owner_user_id, event_id),
  CONSTRAINT audit_event_operation_unique UNIQUE (
    owner_user_id, operation_id, transition_code
  ),
  CONSTRAINT audit_event_hash_unique UNIQUE (owner_user_id, event_sha256),
  CONSTRAINT audit_event_actor CHECK (
    actor_kind IN ('owner', 'reviewer', 'worker', 'system')
    AND (
      (actor_kind IN ('owner', 'reviewer') AND actor_user_id = owner_user_id)
      OR (actor_kind IN ('worker', 'system') AND actor_user_id IS NULL)
    )
  ),
  CONSTRAINT audit_event_names CHECK (
    pg_catalog.octet_length(object_type) BETWEEN 1 AND 64
    AND pg_catalog.octet_length(transition_code) BETWEEN 1 AND 96
    AND pg_catalog.octet_length(reason_code) BETWEEN 1 AND 128
    AND object_type ~ '^[a-z][a-z0-9_]{0,63}$'
    AND transition_code ~ '^[a-z][a-z0-9_]{0,95}$'
    AND reason_code ~ '^[a-z][a-z0-9_]{0,127}$'
  ),
  CONSTRAINT audit_event_hashes CHECK (
    (prior_state_sha256 IS NULL OR prior_state_sha256 ~ '^[0-9a-f]{64}$')
    AND new_state_sha256 ~ '^[0-9a-f]{64}$'
    AND event_sha256 ~ '^[0-9a-f]{64}$'
  )
);

CREATE INDEX audit_event_operation_idx
  ON memory.audit_event(owner_user_id, operation_id, created_at DESC);

CREATE TABLE memory.claim_deletion_receipt (
  receipt_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
  audit_event_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  prior_state_sha256 text NOT NULL,
  delete_outbox_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  revision_sha256 text NOT NULL,
  sequence_number integer NOT NULL,
  collection_alias text NOT NULL,
  physical_collection_name text NOT NULL,
  point_id uuid NOT NULL,
  projection_manifest_sha256 text NOT NULL,
  applied_at timestamptz NOT NULL,
  verified_at timestamptz NOT NULL,
  absence_verification_sha256 text NOT NULL,
  absence_verification_receipt_sha256 text NOT NULL,
  receipt_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  CONSTRAINT claim_deletion_receipt_owner_id UNIQUE (
    owner_user_id, receipt_id
  ),
  CONSTRAINT claim_deletion_receipt_operation_unique UNIQUE (
    owner_user_id, operation_id
  ),
  CONSTRAINT claim_deletion_receipt_claim_unique UNIQUE (
    owner_user_id, claim_id
  ),
  CONSTRAINT claim_deletion_receipt_outbox_unique UNIQUE (
    owner_user_id, delete_outbox_id
  ),
  CONSTRAINT claim_deletion_receipt_audit_fk FOREIGN KEY (
    owner_user_id, audit_event_id
  ) REFERENCES memory.audit_event(owner_user_id, event_id) ON DELETE RESTRICT,
  CONSTRAINT claim_deletion_receipt_point CHECK (point_id = claim_id),
  CONSTRAINT claim_deletion_receipt_sequence CHECK (sequence_number > 0),
  CONSTRAINT claim_deletion_receipt_collection CHECK (
    collection_alias = 'governed_memory_active'
    AND physical_collection_name
      ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
  ),
  CONSTRAINT claim_deletion_receipt_hashes CHECK (
    prior_state_sha256 ~ '^[0-9a-f]{64}$'
    AND revision_sha256 ~ '^[0-9a-f]{64}$'
    AND projection_manifest_sha256 ~ '^[0-9a-f]{64}$'
    AND absence_verification_sha256 ~ '^[0-9a-f]{64}$'
    AND absence_verification_receipt_sha256 ~ '^[0-9a-f]{64}$'
    AND receipt_sha256 ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT claim_deletion_receipt_times CHECK (
    verified_at >= applied_at
  )
);

CREATE INDEX claim_deletion_receipt_owner_time_idx
  ON memory.claim_deletion_receipt(
    owner_user_id, created_at DESC, receipt_id
  );
CREATE INDEX claim_deletion_receipt_audit_idx
  ON memory.claim_deletion_receipt(owner_user_id, audit_event_id);

INSERT INTO memory.predicate_catalog(
  predicate, catalog_name, schema_revision, catalog_sha256,
  subject_kinds, object_kinds, sensitivities, epistemic_statuses
) VALUES
  (
    'commitment.active', 'personal-memory', 1,
    '5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded',
    ARRAY['self'], ARRAY['literal'],
    ARRAY['ordinary','sensitive_self'],
    ARRAY['disputed','supported','uncertain']
  ),
  (
    'entity.attribute', 'personal-memory', 1,
    '5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded',
    ARRAY['organization','other','person','pet','place','self'],
    ARRAY['entity','literal'],
    ARRAY['ordinary','sensitive_self','sensitive_third_party'],
    ARRAY['disputed','supported','uncertain']
  ),
  (
    'event.occurred', 'personal-memory', 1,
    '5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded',
    ARRAY['organization','other','person','pet','place','self'],
    ARRAY['literal'],
    ARRAY['ordinary','sensitive_self','sensitive_third_party'],
    ARRAY['disputed','supported','uncertain']
  ),
  (
    'identity.alias', 'personal-memory', 1,
    '5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded',
    ARRAY['organization','other','person','pet','place','self'],
    ARRAY['literal'],
    ARRAY['ordinary','sensitive_self','sensitive_third_party'],
    ARRAY['disputed','supported','uncertain']
  ),
  (
    'identity.preferred_name', 'personal-memory', 1,
    '5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded',
    ARRAY['person','self'], ARRAY['literal'],
    ARRAY['ordinary','sensitive_self','sensitive_third_party'],
    ARRAY['disputed','supported','uncertain']
  ),
  (
    'location.association', 'personal-memory', 1,
    '5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded',
    ARRAY['organization','other','person','pet','place','self'],
    ARRAY['entity','literal'],
    ARRAY['ordinary','sensitive_self','sensitive_third_party'],
    ARRAY['disputed','supported','uncertain']
  ),
  (
    'preference.personal', 'personal-memory', 1,
    '5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded',
    ARRAY['self'], ARRAY['entity','literal'],
    ARRAY['ordinary','sensitive_self'],
    ARRAY['disputed','supported','uncertain']
  ),
  (
    'relationship.kind', 'personal-memory', 1,
    '5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded',
    ARRAY['person','self'], ARRAY['entity'],
    ARRAY['sensitive_third_party'],
    ARRAY['disputed','supported','uncertain']
  );

CREATE FUNCTION memory_private.current_owner_id()
RETURNS uuid
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor_text text;
  auth_context_sha256 text;
BEGIN
  actor_text := pg_catalog.current_setting('app.user_id', true);
  auth_context_sha256 := pg_catalog.current_setting(
    'app.auth_context_sha256', true
  );
  IF actor_text IS NULL OR actor_text = ''
     OR auth_context_sha256 IS NULL
     OR auth_context_sha256 !~ '^[0-9a-f]{64}$' THEN
    RETURN NULL;
  END IF;
  RETURN actor_text::uuid;
EXCEPTION WHEN invalid_text_representation THEN
  RETURN NULL;
END;
$function$;

ALTER FUNCTION memory_private.current_owner_id()
  OWNER TO governed_memory_owner;
REVOKE ALL ON FUNCTION memory_private.current_owner_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory_private.current_owner_id()
  TO governed_memory_api;

REVOKE ALL ON ALL TABLES IN SCHEMA memory FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA memory FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA memory_private FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA memory FROM governed_memory_api;
REVOKE ALL ON ALL TABLES IN SCHEMA memory FROM governed_memory_worker;

ALTER TABLE memory.evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.evidence FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.evidence
  TO governed_memory_api
  USING (owner_user_id = (SELECT memory_private.current_owner_id()))
  WITH CHECK (owner_user_id = (SELECT memory_private.current_owner_id()));
CREATE POLICY owner_internal ON memory.evidence
  TO governed_memory_owner USING (true) WITH CHECK (true);

ALTER TABLE memory.extraction_job ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.extraction_job FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.extraction_job
  TO governed_memory_api
  USING (owner_user_id = (SELECT memory_private.current_owner_id()))
  WITH CHECK (owner_user_id = (SELECT memory_private.current_owner_id()));
CREATE POLICY owner_internal ON memory.extraction_job
  TO governed_memory_owner USING (true) WITH CHECK (true);

ALTER TABLE memory.provider_call ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.provider_call FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.provider_call
  TO governed_memory_api
  USING (owner_user_id = (SELECT memory_private.current_owner_id()))
  WITH CHECK (owner_user_id = (SELECT memory_private.current_owner_id()));
CREATE POLICY owner_internal ON memory.provider_call
  TO governed_memory_owner USING (true) WITH CHECK (true);

ALTER TABLE memory.proposal ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.proposal FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.proposal
  TO governed_memory_api
  USING (owner_user_id = (SELECT memory_private.current_owner_id()))
  WITH CHECK (owner_user_id = (SELECT memory_private.current_owner_id()));
CREATE POLICY owner_internal ON memory.proposal
  TO governed_memory_owner USING (true) WITH CHECK (true);

ALTER TABLE memory.entity ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.entity FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.entity
  TO governed_memory_api
  USING (owner_user_id = (SELECT memory_private.current_owner_id()))
  WITH CHECK (owner_user_id = (SELECT memory_private.current_owner_id()));
CREATE POLICY owner_internal ON memory.entity
  TO governed_memory_owner USING (true) WITH CHECK (true);

ALTER TABLE memory.claim ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.claim FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.claim
  TO governed_memory_api
  USING (owner_user_id = (SELECT memory_private.current_owner_id()))
  WITH CHECK (owner_user_id = (SELECT memory_private.current_owner_id()));
CREATE POLICY owner_internal ON memory.claim
  TO governed_memory_owner USING (true) WITH CHECK (true);

ALTER TABLE memory.claim_revision ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.claim_revision FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.claim_revision
  TO governed_memory_api
  USING (owner_user_id = (SELECT memory_private.current_owner_id()))
  WITH CHECK (owner_user_id = (SELECT memory_private.current_owner_id()));
CREATE POLICY owner_internal ON memory.claim_revision
  TO governed_memory_owner USING (true) WITH CHECK (true);

ALTER TABLE memory.claim_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.claim_evidence FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.claim_evidence
  TO governed_memory_api
  USING (owner_user_id = (SELECT memory_private.current_owner_id()))
  WITH CHECK (owner_user_id = (SELECT memory_private.current_owner_id()));
CREATE POLICY owner_internal ON memory.claim_evidence
  TO governed_memory_owner USING (true) WITH CHECK (true);

ALTER TABLE memory.projection_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.projection_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.projection_outbox
  TO governed_memory_api
  USING (owner_user_id = (SELECT memory_private.current_owner_id()))
  WITH CHECK (owner_user_id = (SELECT memory_private.current_owner_id()));
CREATE POLICY owner_internal ON memory.projection_outbox
  TO governed_memory_owner USING (true) WITH CHECK (true);

ALTER TABLE memory.answer_binding ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.answer_binding FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.answer_binding
  TO governed_memory_api
  USING (owner_user_id = (SELECT memory_private.current_owner_id()))
  WITH CHECK (owner_user_id = (SELECT memory_private.current_owner_id()));
CREATE POLICY owner_internal ON memory.answer_binding
  TO governed_memory_owner USING (true) WITH CHECK (true);

ALTER TABLE memory.audit_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.audit_event FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.audit_event
  TO governed_memory_api
  USING (owner_user_id = (SELECT memory_private.current_owner_id()))
  WITH CHECK (owner_user_id = (SELECT memory_private.current_owner_id()));
CREATE POLICY owner_internal ON memory.audit_event
  TO governed_memory_owner USING (true) WITH CHECK (true);

ALTER TABLE memory.claim_deletion_receipt ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.claim_deletion_receipt FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation ON memory.claim_deletion_receipt
  TO governed_memory_api
  USING (owner_user_id = (SELECT memory_private.current_owner_id()))
  WITH CHECK (owner_user_id = (SELECT memory_private.current_owner_id()));
CREATE POLICY owner_internal ON memory.claim_deletion_receipt
  TO governed_memory_owner USING (true) WITH CHECK (true);

CREATE FUNCTION memory_private.guard_immutable_fact()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME USING ERRCODE = '55000';
  END IF;
  IF TG_OP = 'DELETE' AND (
    session_user <> 'governed_memory_worker'
    OR COALESCE(pg_catalog.current_setting(
      'app.memory_hard_delete_operation_id', true
    ), '') !~ '^[0-9a-f-]{36}$'
  ) THEN
    RAISE EXCEPTION '% deletion requires verified hard-delete finalization',
      TG_TABLE_NAME USING ERRCODE = '42501';
  END IF;
  RETURN OLD;
END;
$function$;

CREATE FUNCTION memory_private.guard_answer_binding_mutation()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  hard_delete_operation text;
  retention_purge_operation text;
  expected_receipt_sha256 text;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    RAISE EXCEPTION 'answer_binding is immutable' USING ERRCODE = '55000';
  END IF;
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'answer_binding deletion requires worker authority'
      USING ERRCODE = '42501';
  END IF;
  hard_delete_operation := COALESCE(pg_catalog.current_setting(
    'app.memory_hard_delete_operation_id', true
  ), '');
  IF hard_delete_operation ~
       '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' THEN
    RETURN OLD;
  END IF;
  retention_purge_operation := COALESCE(pg_catalog.current_setting(
    'app.memory_answer_binding_purge_operation_id', true
  ), '');
  IF retention_purge_operation !~
       '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     OR OLD.expires_at > pg_catalog.transaction_timestamp() THEN
    RAISE EXCEPTION
      'answer_binding deletion requires exact retention-purge authority'
      USING ERRCODE = '42501';
  END IF;
  expected_receipt_sha256 :=
    memory_private.answer_binding_retention_receipt_sha256(
      OLD.owner_user_id, retention_purge_operation::uuid,
      OLD.binding_id, OLD.expires_at
    );
  IF NOT EXISTS (
    SELECT 1
    FROM memory.audit_event AS receipt
    WHERE receipt.owner_user_id = OLD.owner_user_id
      AND receipt.operation_id = retention_purge_operation::uuid
      AND receipt.actor_kind = 'system'
      AND receipt.actor_user_id IS NULL
      AND receipt.object_type = 'answer_binding'
      AND receipt.object_id = OLD.binding_id
      AND receipt.transition_code = 'answer_binding_retention_purged'
      AND receipt.prior_state_sha256 IS NULL
      AND receipt.new_state_sha256 = expected_receipt_sha256
      AND receipt.reason_code = 'retention_expired'
      AND receipt.event_sha256 = expected_receipt_sha256
  ) THEN
    RAISE EXCEPTION 'answer_binding retention-purge receipt is absent'
      USING ERRCODE = '42501';
  END IF;
  RETURN OLD;
END;
$function$;

CREATE FUNCTION memory_private.guard_append_only_audit()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION 'audit_event is append-only' USING ERRCODE = '55000';
END;
$function$;

CREATE FUNCTION memory_private.guard_evidence_mutation()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF session_user <> 'governed_memory_worker'
       OR NOT (
         COALESCE(pg_catalog.current_setting(
           'app.memory_hard_delete_operation_id', true
         ), '') ~ '^[0-9a-f-]{36}$'
         OR COALESCE(pg_catalog.current_setting(
           'app.memory_proposal_purge_operation_id', true
         ), '') ~ '^[0-9a-f-]{36}$'
       ) THEN
      RAISE EXCEPTION 'evidence deletion requires verified finalization'
        USING ERRCODE = '42501';
    END IF;
    RETURN OLD;
  END IF;
  IF session_user <> 'governed_memory_worker'
     OR OLD.evidence_id IS DISTINCT FROM NEW.evidence_id
     OR OLD.owner_user_id IS DISTINCT FROM NEW.owner_user_id
     OR OLD.operation_id IS DISTINCT FROM NEW.operation_id
     OR OLD.source_kind IS DISTINCT FROM NEW.source_kind
     OR OLD.source_message_id IS DISTINCT FROM NEW.source_message_id
     OR OLD.source_thread_id IS DISTINCT FROM NEW.source_thread_id
     OR OLD.source_window_id IS DISTINCT FROM NEW.source_window_id
     OR OLD.source_window_sha256 IS DISTINCT FROM NEW.source_window_sha256
     OR OLD.bridge_source_binding_sha256
          IS DISTINCT FROM NEW.bridge_source_binding_sha256
     OR OLD.ingest_receipt_sha256 IS DISTINCT FROM NEW.ingest_receipt_sha256
     OR OLD.source_sha256 IS DISTINCT FROM NEW.source_sha256
     OR OLD.selected_sha256 IS DISTINCT FROM NEW.selected_sha256
     OR OLD.selection_binding_sha256
          IS DISTINCT FROM NEW.selection_binding_sha256
     OR OLD.selected_start_utf8 IS DISTINCT FROM NEW.selected_start_utf8
     OR OLD.selected_end_utf8 IS DISTINCT FROM NEW.selected_end_utf8
     OR OLD.context_message_id IS DISTINCT FROM NEW.context_message_id
     OR OLD.context_sha256 IS DISTINCT FROM NEW.context_sha256
     OR OLD.source_created_at IS DISTINCT FROM NEW.source_created_at
     OR OLD.eligibility_decision IS DISTINCT FROM NEW.eligibility_decision
     OR OLD.eligibility_policy_sha256
          IS DISTINCT FROM NEW.eligibility_policy_sha256
     OR OLD.created_at IS DISTINCT FROM NEW.created_at
     OR OLD.review_excerpt IS NULL
     OR NEW.review_excerpt IS NOT NULL
     OR NEW.excerpt_expires_at IS NOT NULL THEN
    RAISE EXCEPTION 'evidence authority binding is immutable'
      USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END;
$function$;

CREATE FUNCTION memory_private.guard_provider_call_mutation()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF session_user <> 'governed_memory_worker'
       OR NOT (
         COALESCE(pg_catalog.current_setting(
           'app.memory_hard_delete_operation_id', true
         ), '') ~ '^[0-9a-f-]{36}$'
         OR COALESCE(pg_catalog.current_setting(
           'app.memory_proposal_purge_operation_id', true
         ), '') ~ '^[0-9a-f-]{36}$'
       ) THEN
      RAISE EXCEPTION 'provider call deletion requires verified finalization'
        USING ERRCODE = '42501';
    END IF;
    RETURN OLD;
  END IF;
  IF session_user <> 'governed_memory_worker'
     OR OLD.provider_call_id IS DISTINCT FROM NEW.provider_call_id
     OR OLD.owner_user_id IS DISTINCT FROM NEW.owner_user_id
     OR OLD.job_id IS DISTINCT FROM NEW.job_id
     OR OLD.attempt_number IS DISTINCT FROM NEW.attempt_number
     OR OLD.operation_id IS DISTINCT FROM NEW.operation_id
     OR OLD.idempotency_sha256 IS DISTINCT FROM NEW.idempotency_sha256
     OR OLD.provider IS DISTINCT FROM NEW.provider
     OR OLD.model IS DISTINCT FROM NEW.model
     OR OLD.schema_sha256 IS DISTINCT FROM NEW.schema_sha256
     OR OLD.selected_sha256 IS DISTINCT FROM NEW.selected_sha256
     OR OLD.selection_binding_sha256
          IS DISTINCT FROM NEW.selection_binding_sha256
     OR OLD.predicate_catalog_sha256
          IS DISTINCT FROM NEW.predicate_catalog_sha256
     OR OLD.operation IS DISTINCT FROM NEW.operation
     OR OLD.privacy_manifest_sha256
          IS DISTINCT FROM NEW.privacy_manifest_sha256
     OR OLD.max_output_tokens IS DISTINCT FROM NEW.max_output_tokens
     OR OLD.timeout_ms IS DISTINCT FROM NEW.timeout_ms
     OR OLD.reserved_at IS DISTINCT FROM NEW.reserved_at
     OR NOT (
       (OLD.state = 'reserved' AND NEW.state IN (
         'dispatched', 'retryable_failure'
       ))
       OR
       (OLD.state = 'dispatched' AND NEW.state IN (
         'completed', 'terminal_failure', 'outcome_unknown'
       ))
     )
     OR (OLD.request_sha256 IS NOT NULL
         AND OLD.request_sha256 IS DISTINCT FROM NEW.request_sha256)
     OR (OLD.response_sha256 IS NOT NULL
         AND OLD.response_sha256 IS DISTINCT FROM NEW.response_sha256)
     OR (OLD.input_tokens IS NOT NULL
         AND OLD.input_tokens IS DISTINCT FROM NEW.input_tokens)
     OR (OLD.output_tokens IS NOT NULL
         AND OLD.output_tokens IS DISTINCT FROM NEW.output_tokens)
     OR (OLD.dispatched_at IS NOT NULL
         AND OLD.dispatched_at IS DISTINCT FROM NEW.dispatched_at)
     OR (OLD.completed_at IS NOT NULL
         AND OLD.completed_at IS DISTINCT FROM NEW.completed_at) THEN
    RAISE EXCEPTION 'provider call binding or transition is invalid'
      USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END;
$function$;

CREATE FUNCTION memory_private.guard_proposal_mutation()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF session_user <> 'governed_memory_worker'
       OR NOT (
         COALESCE(pg_catalog.current_setting(
           'app.memory_proposal_purge_operation_id', true
         ), '') ~ '^[0-9a-f-]{36}$'
         OR COALESCE(pg_catalog.current_setting(
           'app.memory_hard_delete_operation_id', true
         ), '') ~ '^[0-9a-f-]{36}$'
       ) THEN
      RAISE EXCEPTION 'proposal deletion requires bounded retention purge'
        USING ERRCODE = '42501';
    END IF;
    RETURN OLD;
  END IF;
  IF session_user NOT IN ('governed_memory_api', 'governed_memory_worker')
     OR OLD.review_state <> 'pending_review'
     OR NEW.review_state NOT IN ('admitted', 'rejected', 'expired')
     OR OLD.proposal_id IS DISTINCT FROM NEW.proposal_id
     OR OLD.owner_user_id IS DISTINCT FROM NEW.owner_user_id
     OR OLD.evidence_id IS DISTINCT FROM NEW.evidence_id
     OR OLD.provider_call_id IS DISTINCT FROM NEW.provider_call_id
     OR OLD.operation_id IS DISTINCT FROM NEW.operation_id
     OR OLD.proposal_purpose IS DISTINCT FROM NEW.proposal_purpose
     OR OLD.fact_index IS DISTINCT FROM NEW.fact_index
     OR OLD.proposal_sha256 IS DISTINCT FROM NEW.proposal_sha256
     OR OLD.semantic_key_sha256 IS DISTINCT FROM NEW.semantic_key_sha256
     OR OLD.selected_sha256 IS DISTINCT FROM NEW.selected_sha256
     OR OLD.selection_binding_sha256
          IS DISTINCT FROM NEW.selection_binding_sha256
     OR OLD.predicate_catalog_sha256
          IS DISTINCT FROM NEW.predicate_catalog_sha256
     OR OLD.subject_entity_key IS DISTINCT FROM NEW.subject_entity_key
     OR OLD.subject_entity_type IS DISTINCT FROM NEW.subject_entity_type
     OR OLD.subject_display_name IS DISTINCT FROM NEW.subject_display_name
     OR OLD.predicate IS DISTINCT FROM NEW.predicate
     OR OLD.object_kind IS DISTINCT FROM NEW.object_kind
     OR OLD.object_entity_key IS DISTINCT FROM NEW.object_entity_key
     OR OLD.object_entity_type IS DISTINCT FROM NEW.object_entity_type
     OR OLD.object_display_name IS DISTINCT FROM NEW.object_display_name
     OR OLD.object_literal IS DISTINCT FROM NEW.object_literal
     OR OLD.epistemic_state IS DISTINCT FROM NEW.epistemic_state
     OR OLD.projectable IS DISTINCT FROM NEW.projectable
     OR OLD.sensitivity IS DISTINCT FROM NEW.sensitivity
     OR OLD.domains IS DISTINCT FROM NEW.domains
     OR OLD.intents IS DISTINCT FROM NEW.intents
     OR OLD.surface IS DISTINCT FROM NEW.surface
     OR OLD.requires_explicit IS DISTINCT FROM NEW.requires_explicit
     OR OLD.valid_from IS DISTINCT FROM NEW.valid_from
     OR OLD.valid_to IS DISTINCT FROM NEW.valid_to
     OR OLD.correction_of_claim_id
          IS DISTINCT FROM NEW.correction_of_claim_id
     OR OLD.correction_target_revision_id
          IS DISTINCT FROM NEW.correction_target_revision_id
     OR OLD.correction_target_revision_number
          IS DISTINCT FROM NEW.correction_target_revision_number
     OR OLD.expected_revision_sha256
          IS DISTINCT FROM NEW.expected_revision_sha256
     OR OLD.correction_target_state_sha256
          IS DISTINCT FROM NEW.correction_target_state_sha256
     OR OLD.correction_target_identity_sha256
          IS DISTINCT FROM NEW.correction_target_identity_sha256
     OR OLD.correction_target_projection_sequence
          IS DISTINCT FROM NEW.correction_target_projection_sequence
     OR OLD.correction_pending_state_sha256
          IS DISTINCT FROM NEW.correction_pending_state_sha256
     OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
     OR OLD.purge_after IS DISTINCT FROM NEW.purge_after
     OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
    RAISE EXCEPTION 'proposal authority binding is immutable'
      USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END;
$function$;

CREATE FUNCTION memory_private.guard_projection_outbox_mutation()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  revision memory.claim_revision%ROWTYPE;
  target_claim memory.claim%ROWTYPE;
  expected_manifest text;
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF session_user <> 'governed_memory_worker'
       OR COALESCE(pg_catalog.current_setting(
         'app.memory_hard_delete_operation_id', true
       ), '') !~ '^[0-9a-f-]{36}$' THEN
      RAISE EXCEPTION 'projection outbox deletion requires finalization'
        USING ERRCODE = '42501';
    END IF;
    RETURN OLD;
  END IF;
  IF TG_OP = 'INSERT' THEN
    SELECT value.* INTO STRICT target_claim
    FROM memory.claim AS value
    WHERE value.owner_user_id = NEW.owner_user_id
      AND value.claim_id = NEW.claim_id;
    SELECT value.* INTO STRICT revision
    FROM memory.claim_revision AS value
    WHERE value.owner_user_id = NEW.owner_user_id
      AND value.claim_id = NEW.claim_id
      AND value.revision_id = NEW.revision_id;
    expected_manifest := memory_private.projection_manifest_sha256(
      NEW.owner_user_id, NEW.claim_id, NEW.revision_id, NEW.operation_id,
      NEW.operation, NEW.sequence_number, NEW.revision_sha256,
      NEW.selection_binding_sha256,
      revision.retrieval_text_sha256,
      revision.retrieval_text_sha256
    );
    IF NEW.sequence_number <> target_claim.projection_sequence
       OR NEW.revision_id <> target_claim.current_revision_id
       OR NEW.point_id <> target_claim.claim_id
       OR (
         NEW.operation = 'upsert'
         AND target_claim.lifecycle_state <> 'active'
       )
       OR (
         NEW.operation = 'delete'
         AND target_claim.lifecycle_state NOT IN (
           'correction_pending', 'retracted', 'deletion_pending'
         )
       )
       OR NEW.revision_sha256 <> revision.revision_sha256
       OR NEW.selection_binding_sha256
            <> revision.selection_binding_sha256
       OR NEW.projection_manifest_sha256 <> expected_manifest THEN
      RAISE EXCEPTION 'projection outbox authority binding mismatch'
        USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
  END IF;
  IF session_user <> 'governed_memory_worker'
     OR OLD.outbox_id IS DISTINCT FROM NEW.outbox_id
     OR OLD.owner_user_id IS DISTINCT FROM NEW.owner_user_id
     OR OLD.claim_id IS DISTINCT FROM NEW.claim_id
     OR OLD.revision_id IS DISTINCT FROM NEW.revision_id
     OR OLD.operation_id IS DISTINCT FROM NEW.operation_id
     OR OLD.sequence_number IS DISTINCT FROM NEW.sequence_number
     OR OLD.operation IS DISTINCT FROM NEW.operation
     OR OLD.point_id IS DISTINCT FROM NEW.point_id
     OR OLD.revision_sha256 IS DISTINCT FROM NEW.revision_sha256
     OR OLD.selection_binding_sha256
          IS DISTINCT FROM NEW.selection_binding_sha256
     OR OLD.projection_manifest_sha256
          IS DISTINCT FROM NEW.projection_manifest_sha256
     OR OLD.collection_alias IS DISTINCT FROM NEW.collection_alias
     OR OLD.max_attempts IS DISTINCT FROM NEW.max_attempts
     OR OLD.created_at IS DISTINCT FROM NEW.created_at
     OR NOT (
       (OLD.state IN ('pending', 'retryable')
         AND NEW.state IN ('claimed', 'superseded'))
       OR
       (OLD.state = 'claimed'
         AND NEW.state IN ('retryable', 'applied', 'failed_terminal'))
     )
     OR (OLD.vector_sha256 IS NOT NULL
         AND OLD.vector_sha256 IS DISTINCT FROM NEW.vector_sha256)
     OR (OLD.verification_sha256 IS NOT NULL
         AND OLD.verification_sha256 IS DISTINCT FROM NEW.verification_sha256)
     OR (OLD.verification_receipt_sha256 IS NOT NULL
         AND OLD.verification_receipt_sha256
               IS DISTINCT FROM NEW.verification_receipt_sha256)
     OR (OLD.verified_at IS NOT NULL
         AND OLD.verified_at IS DISTINCT FROM NEW.verified_at)
     OR (OLD.physical_collection_name IS NOT NULL
         AND OLD.physical_collection_name
               IS DISTINCT FROM NEW.physical_collection_name)
     OR (OLD.applied_at IS NOT NULL
         AND OLD.applied_at IS DISTINCT FROM NEW.applied_at)
     OR (
       NEW.state IN ('claimed', 'superseded')
       AND (
         NEW.completion_lease_token IS NOT NULL
         OR NEW.completion_outcome IS NOT NULL
       )
     )
     OR (
       OLD.state = 'claimed'
       AND NEW.state = 'applied'
       AND (
         NEW.completion_lease_token IS DISTINCT FROM OLD.lease_token
         OR NEW.completion_outcome IS DISTINCT FROM 'applied'
       )
     )
     OR (
       OLD.state = 'claimed'
       AND NEW.state IN ('retryable', 'failed_terminal')
       AND (
         (
           NEW.last_error_code = 'lease_expired'
           AND (
             NEW.completion_lease_token IS NOT NULL
             OR NEW.completion_outcome IS NOT NULL
           )
         )
         OR
         (
           NEW.last_error_code <> 'lease_expired'
           AND (
             NEW.completion_lease_token IS DISTINCT FROM OLD.lease_token
             OR NEW.completion_outcome NOT IN (
               'retryable', 'failed_terminal'
             )
           )
         )
       )
     ) THEN
    RAISE EXCEPTION 'projection outbox binding or transition is invalid'
      USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END;
$function$;

ALTER FUNCTION memory_private.guard_immutable_fact()
  OWNER TO governed_memory_owner;
ALTER FUNCTION memory_private.guard_answer_binding_mutation()
  OWNER TO governed_memory_owner;
ALTER FUNCTION memory_private.guard_append_only_audit()
  OWNER TO governed_memory_owner;
ALTER FUNCTION memory_private.guard_evidence_mutation()
  OWNER TO governed_memory_owner;
ALTER FUNCTION memory_private.guard_provider_call_mutation()
  OWNER TO governed_memory_owner;
ALTER FUNCTION memory_private.guard_proposal_mutation()
  OWNER TO governed_memory_owner;
ALTER FUNCTION memory_private.guard_projection_outbox_mutation()
  OWNER TO governed_memory_owner;
REVOKE ALL ON FUNCTION memory_private.guard_immutable_fact() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_private.guard_answer_binding_mutation()
  FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_private.guard_append_only_audit() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_private.guard_evidence_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_private.guard_provider_call_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_private.guard_proposal_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_private.guard_projection_outbox_mutation()
  FROM PUBLIC;

CREATE TRIGGER evidence_guard
  BEFORE UPDATE OR DELETE ON memory.evidence
  FOR EACH ROW EXECUTE FUNCTION memory_private.guard_evidence_mutation();
CREATE TRIGGER provider_call_guard
  BEFORE UPDATE OR DELETE ON memory.provider_call
  FOR EACH ROW EXECUTE FUNCTION memory_private.guard_provider_call_mutation();
CREATE TRIGGER proposal_guard
  BEFORE UPDATE OR DELETE ON memory.proposal
  FOR EACH ROW EXECUTE FUNCTION memory_private.guard_proposal_mutation();
CREATE TRIGGER projection_outbox_guard
  BEFORE INSERT OR UPDATE OR DELETE ON memory.projection_outbox
  FOR EACH ROW EXECUTE FUNCTION
    memory_private.guard_projection_outbox_mutation();

CREATE TRIGGER claim_revision_immutable
  BEFORE UPDATE OR DELETE ON memory.claim_revision
  FOR EACH ROW EXECUTE FUNCTION memory_private.guard_immutable_fact();
CREATE TRIGGER claim_evidence_immutable
  BEFORE UPDATE OR DELETE ON memory.claim_evidence
  FOR EACH ROW EXECUTE FUNCTION memory_private.guard_immutable_fact();
CREATE TRIGGER answer_binding_immutable
  BEFORE UPDATE OR DELETE ON memory.answer_binding
  FOR EACH ROW EXECUTE FUNCTION
    memory_private.guard_answer_binding_mutation();
CREATE TRIGGER audit_event_append_only
  BEFORE UPDATE OR DELETE ON memory.audit_event
  FOR EACH ROW EXECUTE FUNCTION memory_private.guard_append_only_audit();
CREATE TRIGGER claim_deletion_receipt_immutable
  BEFORE UPDATE OR DELETE ON memory.claim_deletion_receipt
  FOR EACH ROW EXECUTE FUNCTION memory_private.guard_append_only_audit();

CREATE FUNCTION memory_private.operation_id_conflicts(
  p_owner_user_id uuid,
  p_operation_id uuid,
  p_allowed_proposal_id uuid,
  p_allowed_outbox_id uuid,
  p_allowed_claim_id uuid,
  p_allowed_transition_codes text[]
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
  SELECT EXISTS (
    SELECT 1
    FROM memory.evidence AS value
    WHERE value.owner_user_id = p_owner_user_id
      AND value.operation_id = p_operation_id
    UNION ALL
    SELECT 1
    FROM memory.extraction_job AS value
    WHERE value.owner_user_id = p_owner_user_id
      AND value.operation_id = p_operation_id
    UNION ALL
    SELECT 1
    FROM memory.provider_call AS value
    WHERE value.owner_user_id = p_owner_user_id
      AND value.operation_id = p_operation_id
    UNION ALL
    SELECT 1
    FROM memory.proposal AS value
    WHERE value.owner_user_id = p_owner_user_id
      AND value.operation_id = p_operation_id
      AND value.proposal_id IS DISTINCT FROM p_allowed_proposal_id
    UNION ALL
    SELECT 1
    FROM memory.projection_outbox AS value
    WHERE value.owner_user_id = p_owner_user_id
      AND value.operation_id = p_operation_id
      AND value.outbox_id IS DISTINCT FROM p_allowed_outbox_id
    UNION ALL
    SELECT 1
    FROM memory.answer_binding AS value
    WHERE value.owner_user_id = p_owner_user_id
      AND value.operation_id = p_operation_id
    UNION ALL
    SELECT 1
    FROM memory.claim_deletion_receipt AS value
    WHERE value.owner_user_id = p_owner_user_id
      AND value.operation_id = p_operation_id
      AND value.claim_id IS DISTINCT FROM p_allowed_claim_id
    UNION ALL
    SELECT 1
    FROM memory.audit_event AS value
    WHERE value.owner_user_id = p_owner_user_id
      AND value.operation_id = p_operation_id
      AND NOT (
        value.transition_code = ANY(
          COALESCE(p_allowed_transition_codes, ARRAY[]::text[])
        )
        AND (
          (value.object_type = 'proposal'
            AND value.object_id = p_allowed_proposal_id)
          OR (
            value.object_type = 'proposal'
            AND value.transition_code
                  = 'correction_rejected_lifecycle_override'
            AND EXISTS (
              SELECT 1 FROM memory.proposal AS proposal
              WHERE proposal.owner_user_id = value.owner_user_id
                AND proposal.proposal_id = value.object_id
                AND proposal.correction_of_claim_id = p_allowed_claim_id
            )
          )
          OR (value.object_type = 'projection_outbox'
            AND value.object_id = p_allowed_outbox_id)
          OR (value.object_type = 'claim'
            AND value.object_id = p_allowed_claim_id)
        )
      )
  )
$function$;
ALTER FUNCTION memory_private.operation_id_conflicts(
  uuid,uuid,uuid,uuid,uuid,text[]
) OWNER TO governed_memory_owner;
REVOKE ALL ON FUNCTION memory_private.operation_id_conflicts(
  uuid,uuid,uuid,uuid,uuid,text[]
) FROM PUBLIC;

CREATE FUNCTION memory_private.record_selected_evidence(
  p_owner_user_id uuid,
  p_operation_id uuid,
  p_bridge_source_binding_sha256 text,
  p_message_id uuid,
  p_thread_id uuid,
  p_window_id uuid,
  p_window_sha256 text,
  p_source_sha256 text,
  p_selected_sha256 text,
  p_selected_start_utf8 integer,
  p_selected_end_utf8 integer,
  p_context_message_id uuid,
  p_context_sha256 text,
  p_source_created_at timestamptz,
  p_decision text,
  p_policy_sha256 text,
  p_review_excerpt text
)
RETURNS TABLE(
  outcome text,
  evidence_id uuid,
  extraction_job_id uuid,
  ingest_receipt_sha256 text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  selected_evidence_id uuid;
  selected_job_id uuid;
  selected_binding_sha256 text;
  job_key text;
  event_hash text;
  context_event_hash text;
  existing_event memory.audit_event%ROWTYPE;
  existing_evidence memory.evidence%ROWTYPE;
  existing_job memory.extraction_job%ROWTYPE;
  is_replay boolean := false;
  job_was_inserted boolean := false;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_owner_user_id IS NULL OR p_operation_id IS NULL
     OR p_message_id IS NULL OR p_thread_id IS NULL OR p_window_id IS NULL
     OR p_window_id = '00000000-0000-0000-0000-000000000000'::uuid
     OR p_bridge_source_binding_sha256 IS NULL
     OR p_bridge_source_binding_sha256 !~ '^[0-9a-f]{64}$'
     OR p_window_sha256 IS NULL
     OR p_window_sha256 !~ '^[0-9a-f]{64}$'
     OR p_source_sha256 IS NULL
     OR p_source_sha256 !~ '^[0-9a-f]{64}$'
     OR p_policy_sha256 IS NULL
     OR p_policy_sha256 !~ '^[0-9a-f]{64}$'
     OR p_source_created_at IS NULL
     OR p_decision IS NULL
     OR p_decision NOT IN (
       'send_external', 'skip_zero_call', 'route_internal',
       'block_local', 'review_context'
     ) THEN
    RAISE EXCEPTION 'invalid evidence decision input' USING ERRCODE = '22023';
  END IF;
  IF p_decision = 'send_external' THEN
    IF p_selected_sha256 IS NULL
       OR p_selected_sha256 !~ '^[0-9a-f]{64}$'
       OR p_selected_start_utf8 IS NULL OR p_selected_start_utf8 < 0
       OR p_selected_end_utf8 IS NULL
       OR p_selected_end_utf8 <= p_selected_start_utf8
       OR (p_context_message_id IS NULL) <> (p_context_sha256 IS NULL)
       OR (
         p_context_sha256 IS NOT NULL
         AND p_context_sha256 !~ '^[0-9a-f]{64}$'
       )
       OR p_review_excerpt IS NULL
       OR pg_catalog.octet_length(p_review_excerpt)
            <> p_selected_end_utf8 - p_selected_start_utf8
       OR pg_catalog.encode(pg_catalog.sha256(
         pg_catalog.convert_to(p_review_excerpt, 'UTF8')
       ), 'hex') <> p_selected_sha256 THEN
      RAISE EXCEPTION 'selected evidence binding is invalid'
        USING ERRCODE = '22023';
    END IF;
  ELSIF p_selected_sha256 IS NOT NULL
        OR p_selected_start_utf8 IS NOT NULL
        OR p_selected_end_utf8 IS NOT NULL
        OR p_context_message_id IS NOT NULL
        OR p_context_sha256 IS NOT NULL
        OR p_review_excerpt IS NOT NULL THEN
    RAISE EXCEPTION 'non-send decision cannot persist selected evidence'
      USING ERRCODE = '22023';
  END IF;

  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      p_owner_user_id::text || '|intake|' || p_operation_id::text, 0
    )
  );

  IF p_decision <> 'send_external' THEN
    event_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
      'governed_memory.eligibility_receipt.v1' || E'\n'
        || memory_private.framed_utf8_field(
             'owner_user_id', p_owner_user_id::text
           )
        || memory_private.framed_utf8_field(
             'operation_id', p_operation_id::text
           )
        || memory_private.framed_utf8_field(
             'bridge_source_binding_sha256',
             p_bridge_source_binding_sha256
           )
        || memory_private.framed_utf8_field('decision', p_decision)
        || memory_private.framed_utf8_field(
             'policy_sha256', p_policy_sha256
           )
        || memory_private.framed_utf8_field(
             'message_id', p_message_id::text
           ),
      'UTF8'
    )), 'hex');
    SELECT event.* INTO existing_event
    FROM memory.audit_event AS event
    WHERE event.owner_user_id = p_owner_user_id
      AND event.operation_id = p_operation_id
      AND event.transition_code = 'eligibility_' || p_decision;
    IF FOUND THEN
      IF existing_event.actor_kind <> 'worker'
         OR existing_event.object_type <> 'conversation_message'
         OR existing_event.object_id <> p_message_id
         OR existing_event.transition_code <> 'eligibility_' || p_decision
         OR existing_event.new_state_sha256 <> event_hash
         OR existing_event.reason_code <> p_decision
         OR existing_event.event_sha256 <> event_hash THEN
        RAISE EXCEPTION 'eligibility decision replay drifted'
          USING ERRCODE = '23514';
      END IF;
      RETURN QUERY SELECT
        CASE WHEN p_decision = 'review_context'
          THEN 'context_required_replayed' ELSE 'terminal_decision_replayed' END,
        NULL::uuid, NULL::uuid, NULL::text;
      RETURN;
    END IF;
    context_event_hash := pg_catalog.encode(pg_catalog.sha256(
      pg_catalog.convert_to(
        'governed_memory.eligibility_receipt.v1' || E'\n'
          || memory_private.framed_utf8_field(
               'owner_user_id', p_owner_user_id::text
             )
          || memory_private.framed_utf8_field(
               'operation_id', p_operation_id::text
             )
          || memory_private.framed_utf8_field(
               'bridge_source_binding_sha256',
               p_bridge_source_binding_sha256
             )
          || memory_private.framed_utf8_field(
               'decision', 'review_context'
             )
          || memory_private.framed_utf8_field(
               'policy_sha256', p_policy_sha256
             )
          || memory_private.framed_utf8_field(
               'message_id', p_message_id::text
             ),
        'UTF8'
      )
    ), 'hex');
    IF EXISTS (
      SELECT 1 FROM memory.audit_event AS prior
      WHERE prior.owner_user_id = p_owner_user_id
        AND prior.operation_id = p_operation_id
        AND pg_catalog.left(prior.transition_code, 12) = 'eligibility_'
        AND (
          p_decision = 'review_context'
          OR prior.transition_code <> 'eligibility_review_context'
          OR prior.actor_kind <> 'worker'
          OR prior.object_type <> 'conversation_message'
          OR prior.object_id <> p_message_id
          OR prior.new_state_sha256 <> context_event_hash
          OR prior.reason_code <> 'review_context'
          OR prior.event_sha256 <> context_event_hash
        )
    ) THEN
      RAISE EXCEPTION 'eligibility decision conflicts with prior receipt'
        USING ERRCODE = '23514';
    END IF;
    INSERT INTO memory.audit_event(
      owner_user_id, operation_id, actor_kind, object_type, object_id,
      transition_code, new_state_sha256, reason_code, event_sha256
    ) VALUES (
      p_owner_user_id, p_operation_id, 'worker', 'conversation_message',
      p_message_id, 'eligibility_' || p_decision, event_hash,
      p_decision, event_hash
    );
    RETURN QUERY SELECT
      CASE WHEN p_decision = 'review_context'
        THEN 'context_required' ELSE 'terminal_decision' END,
      NULL::uuid, NULL::uuid, NULL::text;
    RETURN;
  END IF;

  event_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.eligibility_receipt.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'owner_user_id', p_owner_user_id::text
         )
      || memory_private.framed_utf8_field(
           'operation_id', p_operation_id::text
         )
      || memory_private.framed_utf8_field(
           'bridge_source_binding_sha256', p_bridge_source_binding_sha256
         )
      || memory_private.framed_utf8_field('decision', 'review_context')
      || memory_private.framed_utf8_field('policy_sha256', p_policy_sha256)
      || memory_private.framed_utf8_field('message_id', p_message_id::text),
    'UTF8'
  )), 'hex');
  IF (
    p_context_message_id IS NOT NULL
  ) IS DISTINCT FROM EXISTS (
    SELECT 1
    FROM memory.audit_event AS event
    WHERE event.owner_user_id = p_owner_user_id
      AND event.operation_id = p_operation_id
      AND event.actor_kind = 'worker'
      AND event.object_type = 'conversation_message'
      AND event.object_id = p_message_id
      AND event.transition_code = 'eligibility_review_context'
      AND event.new_state_sha256 = event_hash
      AND event.reason_code = 'review_context'
      AND event.event_sha256 = event_hash
  ) OR EXISTS (
    SELECT 1
    FROM memory.audit_event AS event
    WHERE event.owner_user_id = p_owner_user_id
      AND event.operation_id = p_operation_id
      AND pg_catalog.left(event.transition_code, 12) = 'eligibility_'
      AND event.transition_code <> 'eligibility_review_context'
  ) THEN
    RAISE EXCEPTION 'context-resolved selection receipt drifted'
      USING ERRCODE = '40001';
  END IF;
  selected_binding_sha256 := memory_private.selection_binding_sha256(
    p_owner_user_id, 'conversation_message', p_message_id, p_thread_id,
    p_window_id, p_window_sha256, p_source_sha256, p_selected_sha256,
    p_selected_start_utf8, p_selected_end_utf8,
    p_context_message_id, p_context_sha256
  );
  selected_evidence_id := memory_private.derived_uuid(
    p_operation_id, 'bridge-evidence'
  );
  selected_job_id := memory_private.derived_uuid(
    p_operation_id, 'bridge-extraction-job'
  );
  job_key := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(
      p_owner_user_id::text || '|' || selected_evidence_id::text || '|'
      || selected_binding_sha256 || '|extract', 'UTF8'
    )
  ), 'hex');
  event_hash := memory_private.ingest_successor_receipt_sha256(
    p_owner_user_id, p_operation_id, p_bridge_source_binding_sha256,
    p_decision, selected_evidence_id, selected_job_id
  );

  INSERT INTO memory.evidence(
    evidence_id, owner_user_id, operation_id, source_kind, source_message_id,
    source_thread_id, source_window_id, source_window_sha256,
    bridge_source_binding_sha256, ingest_receipt_sha256,
    source_sha256, selected_sha256, selection_binding_sha256,
    selected_start_utf8, selected_end_utf8,
    context_message_id, context_sha256, source_created_at,
    eligibility_decision, eligibility_policy_sha256, review_excerpt,
    excerpt_expires_at
  ) VALUES (
    selected_evidence_id, p_owner_user_id, p_operation_id,
    'conversation_message', p_message_id,
    p_thread_id, p_window_id, p_window_sha256,
    p_bridge_source_binding_sha256, event_hash,
    p_source_sha256, p_selected_sha256, selected_binding_sha256,
    p_selected_start_utf8, p_selected_end_utf8,
    p_context_message_id, p_context_sha256, p_source_created_at, p_decision,
    p_policy_sha256, p_review_excerpt,
    pg_catalog.transaction_timestamp() + interval '7 days'
  )
  ON CONFLICT ON CONSTRAINT evidence_operation_id DO NOTHING
  RETURNING false INTO is_replay;

  IF NOT FOUND THEN
    is_replay := true;
    SELECT existing.* INTO STRICT existing_evidence
    FROM memory.evidence AS existing
    WHERE existing.owner_user_id = p_owner_user_id
      AND existing.operation_id = p_operation_id;
    IF existing_evidence.source_kind <> 'conversation_message'
       OR existing_evidence.source_message_id <> p_message_id
       OR existing_evidence.source_thread_id <> p_thread_id
       OR existing_evidence.source_window_id <> p_window_id
       OR existing_evidence.source_window_sha256 <> p_window_sha256
       OR existing_evidence.bridge_source_binding_sha256
            <> p_bridge_source_binding_sha256
       OR existing_evidence.ingest_receipt_sha256 <> event_hash
       OR existing_evidence.source_sha256 <> p_source_sha256
       OR existing_evidence.selected_sha256 <> p_selected_sha256
       OR existing_evidence.selection_binding_sha256
            <> selected_binding_sha256
       OR existing_evidence.selected_start_utf8 <> p_selected_start_utf8
       OR existing_evidence.selected_end_utf8 <> p_selected_end_utf8
       OR existing_evidence.context_message_id
            IS DISTINCT FROM p_context_message_id
       OR existing_evidence.context_sha256 IS DISTINCT FROM p_context_sha256
       OR existing_evidence.source_created_at <> p_source_created_at
       OR existing_evidence.eligibility_decision <> p_decision
       OR existing_evidence.eligibility_policy_sha256 <> p_policy_sha256
       OR existing_evidence.review_excerpt IS DISTINCT FROM p_review_excerpt
       OR existing_evidence.evidence_id <> selected_evidence_id THEN
      RAISE EXCEPTION 'selected evidence replay drifted'
        USING ERRCODE = '23514';
    END IF;
  END IF;

  INSERT INTO memory.extraction_job(
    job_id, owner_user_id, evidence_id, operation_id, idempotency_sha256
  ) VALUES (
    selected_job_id, p_owner_user_id, selected_evidence_id,
    p_operation_id, job_key
  )
  ON CONFLICT ON CONSTRAINT extraction_job_evidence_unique DO NOTHING
  RETURNING true INTO job_was_inserted;
  IF NOT FOUND THEN
    SELECT existing.* INTO STRICT existing_job
    FROM memory.extraction_job AS existing
    WHERE existing.owner_user_id = p_owner_user_id
      AND existing.evidence_id = selected_evidence_id;
    IF existing_job.operation_id <> p_operation_id
       OR existing_job.job_id <> selected_job_id
       OR existing_job.idempotency_sha256 <> job_key
       OR existing_job.state NOT IN (
         'pending', 'claimed', 'retryable', 'completed', 'failed_terminal'
       ) THEN
      RAISE EXCEPTION 'extraction job replay drifted'
        USING ERRCODE = '23514';
    END IF;
  END IF;
  IF is_replay = job_was_inserted THEN
    RAISE EXCEPTION 'evidence and extraction job replay state diverged'
      USING ERRCODE = '23514';
  END IF;

  INSERT INTO memory.audit_event(
    owner_user_id, operation_id, actor_kind, object_type, object_id,
    transition_code, new_state_sha256, reason_code, event_sha256
  ) VALUES (
    p_owner_user_id, p_operation_id, 'worker', 'extraction_job',
    selected_job_id, 'evidence_selected', event_hash,
    'send_external', event_hash
  )
  ON CONFLICT (owner_user_id, operation_id, transition_code) DO NOTHING;

  IF NOT EXISTS (
    SELECT 1 FROM memory.audit_event AS receipt
    WHERE receipt.owner_user_id = p_owner_user_id
      AND receipt.operation_id = p_operation_id
      AND receipt.actor_kind = 'worker'
      AND receipt.object_type = 'extraction_job'
      AND receipt.object_id = selected_job_id
      AND receipt.transition_code = 'evidence_selected'
      AND receipt.new_state_sha256 = event_hash
      AND receipt.reason_code = 'send_external'
      AND receipt.event_sha256 = event_hash
  ) THEN
    RAISE EXCEPTION 'ingest receipt replay drifted' USING ERRCODE = '23514';
  END IF;

  RETURN QUERY SELECT CASE WHEN is_replay THEN 'replayed' ELSE 'accepted' END,
    selected_evidence_id, selected_job_id, event_hash;
END;
$function$;

CREATE FUNCTION memory_private.read_ingest_receipt(
  p_owner_user_id uuid,
  p_operation_id uuid,
  p_bridge_source_binding_sha256 text
)
RETURNS TABLE(
  evidence_id uuid,
  extraction_job_id uuid,
  ingest_receipt_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_owner_user_id IS NULL OR p_operation_id IS NULL
     OR p_bridge_source_binding_sha256 IS NULL
     OR p_bridge_source_binding_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid ingest receipt lookup' USING ERRCODE = '22023';
  END IF;
  RETURN QUERY
  SELECT evidence.evidence_id, job.job_id, evidence.ingest_receipt_sha256
  FROM memory.evidence AS evidence
  JOIN memory.extraction_job AS job
    ON job.owner_user_id = evidence.owner_user_id
   AND job.evidence_id = evidence.evidence_id
   AND job.operation_id = evidence.operation_id
  CROSS JOIN LATERAL (
    SELECT memory_private.ingest_successor_receipt_sha256(
      evidence.owner_user_id, evidence.operation_id,
      evidence.bridge_source_binding_sha256,
      evidence.eligibility_decision, evidence.evidence_id, job.job_id
    ) AS receipt_sha256
  ) AS binding
  JOIN memory.audit_event AS receipt
    ON receipt.owner_user_id = evidence.owner_user_id
   AND receipt.operation_id = evidence.operation_id
   AND receipt.actor_kind = 'worker'
   AND receipt.object_type = 'extraction_job'
   AND receipt.object_id = job.job_id
   AND receipt.transition_code = 'evidence_selected'
   AND receipt.new_state_sha256 = binding.receipt_sha256
   AND receipt.reason_code = 'send_external'
   AND receipt.event_sha256 = binding.receipt_sha256
  WHERE evidence.owner_user_id = p_owner_user_id
    AND evidence.operation_id = p_operation_id
    AND evidence.source_kind = 'conversation_message'
    AND evidence.bridge_source_binding_sha256
          = p_bridge_source_binding_sha256
    AND evidence.eligibility_decision = 'send_external'
    AND evidence.ingest_receipt_sha256 = binding.receipt_sha256;
END;
$function$;

CREATE FUNCTION memory_private.lease_extraction_jobs(
  p_worker_id text,
  p_limit integer,
  p_lease_seconds integer,
  p_provider text,
  p_model text,
  p_schema_sha256 text,
  p_predicate_catalog_sha256 text,
  p_operation text,
  p_privacy_manifest_sha256 text,
  p_max_output_tokens integer,
  p_timeout_ms integer
)
RETURNS TABLE(
  owner_user_id uuid,
  job_id uuid,
  evidence_id uuid,
  source_kind text,
  source_message_id uuid,
  source_thread_id uuid,
  source_window_id uuid,
  source_window_sha256 text,
  source_sha256 text,
  selected_sha256 text,
  selection_binding_sha256 text,
  selected_start_utf8 integer,
  selected_end_utf8 integer,
  context_message_id uuid,
  context_sha256 text,
  review_excerpt text,
  predicate_catalog_sha256 text,
  attempt_number integer,
  lease_token uuid,
  lease_expires_at timestamptz,
  provider_call_id uuid,
  provider_operation_id uuid
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  candidate record;
  new_lease_token uuid;
  new_provider_call_id uuid;
  new_operation_id uuid;
  new_lease_expires_at timestamptz;
  call_key text;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF COALESCE(p_worker_id, '') !~ '^[a-z][a-z0-9_:/.-]{0,127}$'
     OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 50
     OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 5 AND 300
     OR pg_catalog.octet_length(COALESCE(p_provider, '')) NOT BETWEEN 1 AND 64
     OR pg_catalog.octet_length(COALESCE(p_model, '')) NOT BETWEEN 1 AND 128
     OR p_schema_sha256 IS NULL
     OR p_schema_sha256 !~ '^[0-9a-f]{64}$'
     OR p_predicate_catalog_sha256 IS NULL
     OR p_predicate_catalog_sha256 !~ '^[0-9a-f]{64}$'
     OR pg_catalog.octet_length(COALESCE(p_operation, '')) NOT BETWEEN 1 AND 64
     OR p_privacy_manifest_sha256 IS NULL
     OR p_privacy_manifest_sha256 !~ '^[0-9a-f]{64}$'
     OR p_max_output_tokens IS NULL
     OR p_max_output_tokens NOT BETWEEN 1 AND 8192
     OR p_timeout_ms IS NULL
     OR p_timeout_ms NOT BETWEEN 1000 AND 120000 THEN
    RAISE EXCEPTION 'invalid extraction lease input' USING ERRCODE = '22023';
  END IF;
  IF (
    SELECT pg_catalog.count(*)
    FROM memory.predicate_catalog AS predicate
    WHERE predicate.active
      AND predicate.catalog_sha256 = p_predicate_catalog_sha256
  ) <> 8 THEN
    RAISE EXCEPTION 'requested predicate catalog is not the exact active catalog'
      USING ERRCODE = '40001';
  END IF;

  UPDATE memory.provider_call AS provider
  SET state = CASE WHEN provider.state = 'reserved'
        THEN 'retryable_failure' ELSE 'outcome_unknown' END,
      completed_at = pg_catalog.clock_timestamp(),
      error_code = CASE WHEN provider.state = 'reserved'
        THEN 'lease_expired_before_dispatch'
        ELSE 'lease_expired_after_dispatch' END
  FROM memory.extraction_job AS job
  WHERE job.owner_user_id = provider.owner_user_id
    AND job.job_id = provider.job_id
    AND job.state = 'claimed'
    AND job.lease_expires_at <= pg_catalog.clock_timestamp()
    AND provider.attempt_number = job.attempt_count
    AND provider.state IN ('reserved', 'dispatched');

  UPDATE memory.extraction_job AS expired_job
  SET state = CASE
        WHEN EXISTS (
          SELECT 1 FROM memory.provider_call AS provider
          WHERE provider.owner_user_id = expired_job.owner_user_id
            AND provider.job_id = expired_job.job_id
            AND provider.attempt_number = expired_job.attempt_count
            AND provider.state = 'outcome_unknown'
        ) OR expired_job.attempt_count >= expired_job.max_attempts
          THEN 'failed_terminal'
        ELSE 'retryable'
      END,
      available_at = pg_catalog.clock_timestamp(),
      lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
      lease_expires_at = NULL,
      completed_at = CASE
        WHEN EXISTS (
          SELECT 1 FROM memory.provider_call AS provider
          WHERE provider.owner_user_id = expired_job.owner_user_id
            AND provider.job_id = expired_job.job_id
            AND provider.attempt_number = expired_job.attempt_count
            AND provider.state = 'outcome_unknown'
        ) OR expired_job.attempt_count >= expired_job.max_attempts
        THEN pg_catalog.clock_timestamp() ELSE NULL END,
      last_error_code = CASE WHEN EXISTS (
        SELECT 1 FROM memory.provider_call AS provider
        WHERE provider.owner_user_id = expired_job.owner_user_id
          AND provider.job_id = expired_job.job_id
          AND provider.attempt_number = expired_job.attempt_count
          AND provider.state = 'outcome_unknown'
      ) THEN 'provider_outcome_unknown' ELSE 'lease_expired_before_dispatch' END,
      updated_at = pg_catalog.clock_timestamp()
  WHERE expired_job.state = 'claimed'
    AND expired_job.lease_expires_at <= pg_catalog.clock_timestamp();

  UPDATE memory.evidence AS evidence
  SET review_excerpt = NULL, excerpt_expires_at = NULL
  FROM memory.extraction_job AS terminal_job
  WHERE terminal_job.owner_user_id = evidence.owner_user_id
    AND terminal_job.evidence_id = evidence.evidence_id
    AND terminal_job.state = 'failed_terminal'
    AND evidence.review_excerpt IS NOT NULL;

  FOR candidate IN
    SELECT job.owner_user_id, job.job_id, job.evidence_id,
           job.attempt_count, evidence.source_kind,
           evidence.source_message_id,
           evidence.source_thread_id, evidence.source_window_id,
           evidence.source_window_sha256, evidence.source_sha256,
           evidence.selected_sha256,
           evidence.selection_binding_sha256,
           evidence.selected_start_utf8, evidence.selected_end_utf8,
           evidence.context_message_id, evidence.context_sha256,
           evidence.review_excerpt
    FROM memory.extraction_job AS job
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id = job.owner_user_id
     AND evidence.evidence_id = job.evidence_id
    WHERE job.state IN ('pending', 'retryable')
      AND job.available_at <= pg_catalog.clock_timestamp()
      AND job.attempt_count < job.max_attempts
      AND evidence.source_kind = 'conversation_message'
      AND evidence.review_excerpt IS NOT NULL
    ORDER BY job.available_at, job.created_at, job.job_id
    FOR UPDATE OF job SKIP LOCKED
    LIMIT p_limit
  LOOP
    new_lease_token := pg_catalog.gen_random_uuid();
    new_provider_call_id := pg_catalog.gen_random_uuid();
    new_operation_id := pg_catalog.gen_random_uuid();
    new_lease_expires_at := pg_catalog.clock_timestamp()
      + pg_catalog.make_interval(secs => p_lease_seconds);
    call_key := pg_catalog.encode(pg_catalog.sha256(
      pg_catalog.convert_to(
        candidate.owner_user_id::text || '|' || candidate.job_id::text
        || '|' || (candidate.attempt_count + 1)::text || '|'
        || p_provider || '|' || p_model || '|' || p_schema_sha256 || '|'
        || p_predicate_catalog_sha256 || '|'
        || candidate.selection_binding_sha256,
        'UTF8'
      )
    ), 'hex');

    UPDATE memory.extraction_job
    SET state = 'claimed', attempt_count = attempt_count + 1,
        lease_token = new_lease_token, claimed_by = p_worker_id,
        claimed_at = pg_catalog.clock_timestamp(),
        lease_expires_at = new_lease_expires_at,
        last_error_code = NULL, updated_at = pg_catalog.clock_timestamp()
    WHERE extraction_job.owner_user_id = candidate.owner_user_id
      AND extraction_job.job_id = candidate.job_id;

    INSERT INTO memory.provider_call(
      provider_call_id, owner_user_id, job_id, attempt_number,
      operation_id, idempotency_sha256, provider, model, schema_sha256,
      selected_sha256, selection_binding_sha256,
      predicate_catalog_sha256, operation, privacy_manifest_sha256,
      max_output_tokens, timeout_ms
    ) VALUES (
      new_provider_call_id, candidate.owner_user_id, candidate.job_id,
      candidate.attempt_count + 1, new_operation_id, call_key,
      p_provider, p_model, p_schema_sha256, candidate.selected_sha256,
      candidate.selection_binding_sha256, p_predicate_catalog_sha256, p_operation,
      p_privacy_manifest_sha256, p_max_output_tokens, p_timeout_ms
    );

    RETURN QUERY SELECT candidate.owner_user_id, candidate.job_id,
      candidate.evidence_id, candidate.source_kind,
      candidate.source_message_id,
      candidate.source_thread_id, candidate.source_window_id,
      candidate.source_window_sha256, candidate.source_sha256,
      candidate.selected_sha256,
      candidate.selection_binding_sha256, candidate.selected_start_utf8,
      candidate.selected_end_utf8, candidate.context_message_id,
      candidate.context_sha256, candidate.review_excerpt,
      p_predicate_catalog_sha256,
      candidate.attempt_count + 1, new_lease_token,
      new_lease_expires_at,
      new_provider_call_id, new_operation_id;
  END LOOP;
END;
$function$;

CREATE FUNCTION memory_private.mark_provider_call_dispatched(
  p_job_id uuid,
  p_lease_token uuid,
  p_provider_call_id uuid,
  p_request_sha256 text,
  p_expected_selected_sha256 text,
  p_expected_selection_binding_sha256 text,
  p_expected_predicate_catalog_sha256 text
)
RETURNS TABLE(outcome text, dispatched_at timestamptz)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  job memory.extraction_job%ROWTYPE;
  call memory.provider_call%ROWTYPE;
  captured_at timestamptz;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_job_id IS NULL OR p_lease_token IS NULL OR p_provider_call_id IS NULL
     OR p_request_sha256 IS NULL
     OR p_request_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_selected_sha256 IS NULL
     OR p_expected_selected_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_selection_binding_sha256 IS NULL
     OR p_expected_selection_binding_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_predicate_catalog_sha256 IS NULL
     OR p_expected_predicate_catalog_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid provider dispatch input' USING ERRCODE = '22023';
  END IF;
  SELECT value.* INTO STRICT job
  FROM memory.extraction_job AS value
  WHERE value.job_id = p_job_id
  FOR UPDATE;
  IF job.state <> 'claimed' OR job.lease_token <> p_lease_token
     OR job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
    RAISE EXCEPTION 'stale extraction lease' USING ERRCODE = '40001';
  END IF;
  SELECT value.* INTO STRICT call
  FROM memory.provider_call AS value
  WHERE value.owner_user_id = job.owner_user_id
    AND value.job_id = job.job_id
    AND value.provider_call_id = p_provider_call_id
    AND value.attempt_number = job.attempt_count
  FOR UPDATE;
  IF call.selected_sha256 <> p_expected_selected_sha256
     OR call.selection_binding_sha256
          <> p_expected_selection_binding_sha256
     OR call.predicate_catalog_sha256
          <> p_expected_predicate_catalog_sha256 THEN
    RAISE EXCEPTION 'provider dispatch authority binding drifted'
      USING ERRCODE = '40001';
  END IF;
  IF call.state = 'dispatched' THEN
    IF call.request_sha256 <> p_request_sha256 THEN
      RAISE EXCEPTION 'provider dispatch replay request drifted'
        USING ERRCODE = '23514';
    END IF;
    RAISE EXCEPTION
      'provider dispatch was already authorized; external retry is forbidden'
      USING ERRCODE = '55000';
  END IF;
  IF call.state <> 'reserved' THEN
    RAISE EXCEPTION 'provider call cannot enter dispatched state'
      USING ERRCODE = '40001';
  END IF;
  captured_at := pg_catalog.clock_timestamp();
  UPDATE memory.provider_call
  SET state = 'dispatched', request_sha256 = p_request_sha256,
      dispatched_at = captured_at
  WHERE provider_call.owner_user_id = call.owner_user_id
    AND provider_call.provider_call_id = call.provider_call_id;
  RETURN QUERY SELECT 'dispatched'::text, captured_at;
END;
$function$;

CREATE FUNCTION memory_private.complete_extraction(
  p_job_id uuid,
  p_lease_token uuid,
  p_provider_call_id uuid,
  p_outcome text,
  p_request_sha256 text,
  p_response_sha256 text,
  p_input_tokens integer,
  p_output_tokens integer,
  p_error_code text,
  p_proposals jsonb
)
RETURNS TABLE(outcome text, proposal_count integer)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  job memory.extraction_job%ROWTYPE;
  call memory.provider_call%ROWTYPE;
  evidence memory.evidence%ROWTYPE;
  stored_proposal memory.proposal%ROWTYPE;
  item jsonb;
  stored_item jsonb;
  item_count integer := 0;
  stored_item_count integer;
  replay_seen_proposal_ids uuid[] := ARRAY[]::uuid[];
  proposal_operation_id uuid;
  item_proposal_id uuid;
  item_fact_index smallint;
  computed_semantic_sha text;
  computed_proposal_sha text;
  validated_fact_sha text;
  derived_surface text;
  derived_requires_explicit boolean;
  replacement_object_entity_key text;
  completion_event_hash text;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_job_id IS NULL OR p_lease_token IS NULL OR p_provider_call_id IS NULL
     OR p_outcome IS NULL OR p_outcome NOT IN (
       'completed', 'retryable_failure', 'terminal_failure', 'outcome_unknown'
     )
     OR (
       p_request_sha256 IS NOT NULL
       AND p_request_sha256 !~ '^[0-9a-f]{64}$'
     )
     OR (
       p_response_sha256 IS NOT NULL
       AND p_response_sha256 !~ '^[0-9a-f]{64}$'
     )
     OR (
       p_error_code IS NOT NULL
       AND p_error_code !~ '^[a-z][a-z0-9_]{0,127}$'
     )
     OR (p_input_tokens IS NOT NULL
         AND p_input_tokens NOT BETWEEN 0 AND 100000)
     OR (p_output_tokens IS NOT NULL AND p_output_tokens < 0)
     OR (
       p_outcome IN ('completed', 'terminal_failure', 'outcome_unknown')
       AND p_request_sha256 IS NULL
     )
     OR (p_outcome = 'retryable_failure' AND (
       p_request_sha256 IS NOT NULL
       OR p_error_code NOT IN (
         'adapter_rejected_before_send',
         'connection_failed_before_send',
         'evidence_excerpt_expired_before_dispatch',
         'local_serialization_failed_before_send',
         'provider_proved_not_accepted',
         'lease_expired_before_dispatch'
       )
     ))
     OR (p_outcome = 'terminal_failure' AND p_error_code NOT IN (
       'invalid_provider_output',
       'provider_auth_rejected',
       'provider_request_rejected',
       'provider_usage_contract_violation',
       'response_schema_violation'
     ))
     OR (
       p_outcome = 'outcome_unknown'
       AND (
         p_request_sha256 IS NULL
         OR p_error_code NOT IN (
           'connection_reset_after_dispatch',
           'dispatch_crash',
           'provider_timeout_after_dispatch',
           'response_persistence_failed_after_dispatch',
           'lease_expired_after_dispatch'
         )
       )
     ) THEN
    RAISE EXCEPTION 'invalid extraction completion input'
      USING ERRCODE = '22023';
  END IF;

  SELECT value.* INTO STRICT job
  FROM memory.extraction_job AS value
  WHERE value.job_id = p_job_id
  FOR UPDATE;
  SELECT value.* INTO STRICT call
  FROM memory.provider_call AS value
  WHERE value.owner_user_id = job.owner_user_id
    AND value.job_id = job.job_id
    AND value.provider_call_id = p_provider_call_id
  FOR UPDATE;
  SELECT value.* INTO STRICT evidence
  FROM memory.evidence AS value
  WHERE value.owner_user_id = job.owner_user_id
    AND value.evidence_id = job.evidence_id;

  IF call.state IN (
    'completed', 'retryable_failure', 'terminal_failure', 'outcome_unknown'
  ) THEN
    IF call.state <> p_outcome
       OR call.request_sha256 IS DISTINCT FROM p_request_sha256
       OR call.response_sha256 IS DISTINCT FROM p_response_sha256
       OR call.input_tokens IS DISTINCT FROM p_input_tokens
       OR call.output_tokens IS DISTINCT FROM p_output_tokens
       OR call.error_code IS DISTINCT FROM p_error_code
       OR p_proposals IS NULL
       OR pg_catalog.jsonb_typeof(p_proposals) <> 'array'
       OR NOT EXISTS (
         SELECT 1 FROM memory.audit_event AS receipt
         WHERE receipt.owner_user_id = call.owner_user_id
           AND receipt.operation_id = call.operation_id
           AND receipt.object_type = 'provider_call'
           AND receipt.object_id = call.provider_call_id
           AND receipt.transition_code = 'provider_call_' || call.state
       ) THEN
      RAISE EXCEPTION 'provider completion replay drifted'
        USING ERRCODE = '23514';
    END IF;
    IF call.state = 'completed' THEN
      IF pg_catalog.jsonb_array_length(p_proposals) NOT BETWEEN 0 AND 8
         OR pg_catalog.pg_column_size(p_proposals) > 131072 THEN
        RAISE EXCEPTION 'provider completion proposal replay drifted'
          USING ERRCODE = '23514';
      END IF;
      FOR item IN
        SELECT value FROM pg_catalog.jsonb_array_elements(p_proposals)
          AS replay_item(value)
      LOOP
        IF pg_catalog.jsonb_typeof(item) <> 'object'
           OR ARRAY(
             SELECT key
             FROM pg_catalog.jsonb_object_keys(item) AS object_key(key)
             ORDER BY key
           ) <> ARRAY[
             'epistemic_state', 'fact_index', 'object_display_name',
             'object_entity_key', 'object_entity_type', 'object_kind',
             'object_literal', 'operation_id', 'predicate', 'proposal_id',
             'proposal_sha256', 'semantic_key_sha256', 'sensitivity',
             'subject_display_name', 'subject_entity_key',
             'subject_entity_type'
           ]::text[]
           OR item->>'proposal_id' !~
                '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
           OR item->>'operation_id' !~
                '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
           OR item->>'proposal_sha256' !~ '^[0-9a-f]{64}$'
           OR item->>'semantic_key_sha256' !~ '^[0-9a-f]{64}$'
           OR item->>'fact_index' !~ '^[0-7]$'
           OR item->>'subject_entity_key'
                IS DISTINCT FROM memory_private.source_local_entity_key(
                  job.owner_user_id, call.selection_binding_sha256,
                  CASE WHEN item->>'fact_index' ~ '^[0-7]$'
                    THEN (item->>'fact_index')::integer
                    ELSE NULL::integer END,
                  'subject', item->>'subject_entity_type'
                )
           OR (
             item->>'object_kind' = 'entity'
             AND item->>'object_entity_key'
                  IS DISTINCT FROM memory_private.source_local_entity_key(
                    job.owner_user_id, call.selection_binding_sha256,
                    CASE WHEN item->>'fact_index' ~ '^[0-7]$'
                      THEN (item->>'fact_index')::integer
                      ELSE NULL::integer END,
                    'object', item->>'object_entity_type'
                  )
           ) THEN
          RAISE EXCEPTION 'provider completion proposal replay drifted'
            USING ERRCODE = '23514';
        END IF;
        item_proposal_id := (item->>'proposal_id')::uuid;
        IF item_proposal_id = ANY(replay_seen_proposal_ids) THEN
          RAISE EXCEPTION 'provider completion proposal replay drifted'
            USING ERRCODE = '23514';
        END IF;
        replay_seen_proposal_ids := pg_catalog.array_append(
          replay_seen_proposal_ids, item_proposal_id
        );
        SELECT value.* INTO stored_proposal
        FROM memory.proposal AS value
        WHERE value.owner_user_id = call.owner_user_id
          AND value.provider_call_id = call.provider_call_id
          AND value.proposal_id = item_proposal_id;
        IF NOT FOUND THEN
          RAISE EXCEPTION 'provider completion proposal replay drifted'
            USING ERRCODE = '23514';
        END IF;
        stored_item := pg_catalog.jsonb_build_object(
          'epistemic_state', stored_proposal.epistemic_state,
          'fact_index', stored_proposal.fact_index,
          'object_display_name', stored_proposal.object_display_name,
          'object_entity_key', stored_proposal.object_entity_key,
          'object_entity_type', stored_proposal.object_entity_type,
          'object_kind', stored_proposal.object_kind,
          'object_literal', stored_proposal.object_literal,
          'operation_id', stored_proposal.operation_id,
          'predicate', stored_proposal.predicate,
          'proposal_id', stored_proposal.proposal_id,
          'proposal_sha256', stored_proposal.proposal_sha256,
          'semantic_key_sha256', stored_proposal.semantic_key_sha256,
          'sensitivity', stored_proposal.sensitivity,
          'subject_display_name', stored_proposal.subject_display_name,
          'subject_entity_key', stored_proposal.subject_entity_key,
          'subject_entity_type', stored_proposal.subject_entity_type
        );
        IF item IS DISTINCT FROM stored_item THEN
          RAISE EXCEPTION 'provider completion proposal replay drifted'
            USING ERRCODE = '23514';
        END IF;
        item_fact_index := stored_proposal.fact_index;
        computed_semantic_sha := memory_private.semantic_key_sha256(
          stored_proposal.subject_entity_key, stored_proposal.predicate,
          stored_proposal.object_kind, stored_proposal.object_entity_key,
          stored_proposal.object_literal
        );
        validated_fact_sha := memory_private.validated_fact_sha256(
          call.selection_binding_sha256, item_fact_index,
          stored_proposal.subject_entity_type,
          stored_proposal.subject_entity_key,
          stored_proposal.subject_display_name, stored_proposal.predicate,
          stored_proposal.object_kind, stored_proposal.object_entity_type,
          stored_proposal.object_entity_key,
          stored_proposal.object_display_name,
          stored_proposal.object_literal, stored_proposal.epistemic_state,
          stored_proposal.sensitivity, computed_semantic_sha
        );
        proposal_operation_id := memory_private.uuid5(
          call.provider_call_id,
          'admission:' || item_fact_index::text || ':' || validated_fact_sha
        );
        derived_surface := CASE
          WHEN stored_proposal.sensitivity = 'ordinary'
            THEN 'normal' ELSE 'explicit_only' END;
        derived_requires_explicit :=
          stored_proposal.sensitivity <> 'ordinary';
        computed_proposal_sha := memory_private.proposal_sha256(
          job.owner_user_id, job.job_id, job.evidence_id,
          call.provider_call_id, evidence.source_kind,
          evidence.source_message_id, evidence.source_thread_id,
          evidence.source_window_id, evidence.source_window_sha256,
          evidence.source_sha256, call.selected_sha256,
          call.selection_binding_sha256, evidence.context_message_id,
          evidence.context_sha256, call.predicate_catalog_sha256,
          call.request_sha256, p_response_sha256, 'new_claim',
          NULL::uuid, NULL::uuid, NULL::integer, NULL::text, NULL::text,
          NULL::text, NULL::integer, NULL::text, item_fact_index,
          stored_proposal.proposal_id, proposal_operation_id,
          stored_proposal.subject_entity_type,
          stored_proposal.subject_entity_key,
          stored_proposal.subject_display_name, stored_proposal.predicate,
          stored_proposal.object_kind, stored_proposal.object_entity_type,
          stored_proposal.object_entity_key,
          stored_proposal.object_display_name,
          stored_proposal.object_literal, stored_proposal.epistemic_state,
          stored_proposal.sensitivity, computed_semantic_sha,
          true, ARRAY[]::text[], ARRAY[]::text[], derived_surface,
          derived_requires_explicit, NULL::timestamptz, NULL::timestamptz
        );
        IF stored_proposal.evidence_id <> job.evidence_id
           OR computed_semantic_sha <> item->>'semantic_key_sha256'
           OR computed_proposal_sha <> item->>'proposal_sha256'
           OR stored_proposal.operation_id <> proposal_operation_id
           OR stored_proposal.proposal_id <> memory_private.uuid5(
             call.provider_call_id,
             'proposal:' || item_fact_index::text || ':'
               || validated_fact_sha
           )
           OR stored_proposal.proposal_purpose <> 'new_claim'
           OR stored_proposal.selected_sha256 <> call.selected_sha256
           OR stored_proposal.selection_binding_sha256
                <> call.selection_binding_sha256
           OR stored_proposal.predicate_catalog_sha256
                <> call.predicate_catalog_sha256
           OR stored_proposal.semantic_key_sha256 <> computed_semantic_sha
           OR stored_proposal.proposal_sha256 <> computed_proposal_sha
           OR NOT stored_proposal.projectable
           OR stored_proposal.domains <> ARRAY[]::text[]
           OR stored_proposal.intents <> ARRAY[]::text[]
           OR stored_proposal.surface <> derived_surface
           OR stored_proposal.requires_explicit
                <> derived_requires_explicit
           OR stored_proposal.valid_from IS NOT NULL
           OR stored_proposal.valid_to IS NOT NULL
           OR NOT EXISTS (
             SELECT 1 FROM memory.predicate_catalog AS predicate
             WHERE predicate.active
               AND predicate.predicate = stored_proposal.predicate
               AND predicate.catalog_sha256 = call.predicate_catalog_sha256
               AND stored_proposal.subject_entity_type
                     = ANY(predicate.subject_kinds)
               AND stored_proposal.object_kind = ANY(predicate.object_kinds)
               AND stored_proposal.sensitivity = ANY(predicate.sensitivities)
               AND stored_proposal.epistemic_state
                     = ANY(predicate.epistemic_statuses)
           ) THEN
          RAISE EXCEPTION 'provider completion proposal replay drifted'
            USING ERRCODE = '23514';
        END IF;
        item_count := item_count + 1;
      END LOOP;
      SELECT pg_catalog.count(*)::integer INTO stored_item_count
      FROM memory.proposal AS proposal
      WHERE proposal.owner_user_id = call.owner_user_id
        AND proposal.provider_call_id = call.provider_call_id;
      IF stored_item_count <> item_count THEN
        RAISE EXCEPTION 'provider completion proposal replay drifted'
          USING ERRCODE = '23514';
      END IF;
    ELSIF p_proposals IS DISTINCT FROM '[]'::jsonb THEN
      RAISE EXCEPTION 'provider failure replay contains proposals'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT 'replayed'::text, item_count;
    RETURN;
  END IF;

  IF job.state <> 'claimed' OR job.lease_token <> p_lease_token
     OR job.lease_expires_at <= pg_catalog.clock_timestamp()
     OR call.attempt_number <> job.attempt_count THEN
    RAISE EXCEPTION 'stale extraction lease' USING ERRCODE = '40001';
  END IF;
  IF call.selected_sha256 <> evidence.selected_sha256
     OR call.selection_binding_sha256 <> evidence.selection_binding_sha256 THEN
    RAISE EXCEPTION 'provider call is not the exact dispatched request'
      USING ERRCODE = '40001';
  END IF;
  IF (p_outcome = 'retryable_failure' AND call.state <> 'reserved')
     OR (p_outcome IN ('completed', 'outcome_unknown') AND (
       call.state <> 'dispatched'
       OR call.request_sha256 <> p_request_sha256
     ))
     OR (p_outcome = 'terminal_failure' AND (
       call.state <> 'dispatched'
       OR call.request_sha256 <> p_request_sha256
     )) THEN
    RAISE EXCEPTION 'provider outcome does not match dispatch boundary'
      USING ERRCODE = '40001';
  END IF;

  IF p_outcome = 'completed' THEN
    IF p_response_sha256 IS NULL OR p_error_code IS NOT NULL
       OR p_input_tokens IS NULL OR p_output_tokens IS NULL
       OR p_output_tokens NOT BETWEEN 0 AND call.max_output_tokens
       OR p_proposals IS NULL OR pg_catalog.jsonb_typeof(p_proposals) <> 'array'
       OR pg_catalog.jsonb_array_length(p_proposals) NOT BETWEEN 0 AND 8
       OR pg_catalog.pg_column_size(p_proposals) > 131072 THEN
      RAISE EXCEPTION 'completed extraction payload is invalid'
        USING ERRCODE = '22023';
    END IF;

    FOR item IN SELECT value FROM pg_catalog.jsonb_array_elements(p_proposals)
      AS array_item(value)
    LOOP
      IF pg_catalog.jsonb_typeof(item) <> 'object'
         OR ARRAY(
           SELECT key
           FROM pg_catalog.jsonb_object_keys(item) AS object_key(key)
           ORDER BY key
         ) <> ARRAY[
           'epistemic_state', 'fact_index', 'object_display_name',
           'object_entity_key', 'object_entity_type', 'object_kind',
           'object_literal', 'operation_id', 'predicate', 'proposal_id',
           'proposal_sha256', 'semantic_key_sha256', 'sensitivity',
           'subject_display_name', 'subject_entity_key',
           'subject_entity_type'
         ]::text[]
         OR item->>'proposal_sha256' !~ '^[0-9a-f]{64}$'
         OR item->>'semantic_key_sha256' !~ '^[0-9a-f]{64}$'
         OR pg_catalog.jsonb_typeof(item->'fact_index') <> 'number'
         OR item->>'fact_index' !~ '^[0-7]$'
         OR item->>'proposal_id' !~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
         OR item->>'operation_id' !~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
         OR pg_catalog.jsonb_typeof(item->'subject_entity_key') <> 'string'
         OR pg_catalog.jsonb_typeof(item->'subject_entity_type') <> 'string'
         OR (
           item->>'subject_entity_type' = 'self'
           AND item->'subject_display_name' <> 'null'::jsonb
         )
         OR (
           item->>'subject_entity_type' <> 'self'
           AND pg_catalog.jsonb_typeof(item->'subject_display_name') <> 'string'
         )
         OR pg_catalog.jsonb_typeof(item->'predicate') <> 'string'
         OR pg_catalog.octet_length(item->>'subject_entity_key')
              NOT BETWEEN 1 AND 200
         OR (
           item->>'subject_entity_type' <> 'self'
           AND pg_catalog.octet_length(item->>'subject_display_name')
                 NOT BETWEEN 1 AND 256
         )
         OR COALESCE(item->>'subject_entity_type', '') NOT IN (
           'self', 'person', 'pet', 'organization', 'place', 'other'
         )
         OR item->>'subject_entity_key'
              IS DISTINCT FROM memory_private.source_local_entity_key(
                job.owner_user_id, call.selection_binding_sha256,
                CASE WHEN item->>'fact_index' ~ '^[0-7]$'
                  THEN (item->>'fact_index')::integer ELSE NULL::integer END,
                'subject',
                item->>'subject_entity_type'
              )
         OR COALESCE(item->>'object_kind', '') NOT IN ('literal', 'entity')
         OR COALESCE(item->>'epistemic_state', '') NOT IN (
           'supported', 'uncertain', 'disputed'
         )
         OR COALESCE(item->>'sensitivity', '') NOT IN (
           'ordinary', 'sensitive_self', 'sensitive_third_party'
         )
         OR (
           item->>'object_kind' = 'literal'
           AND (
             pg_catalog.jsonb_typeof(item->'object_literal') <> 'string'
             OR pg_catalog.octet_length(item->>'object_literal')
                  NOT BETWEEN 1 AND 2000
             OR item->'object_entity_key' <> 'null'::jsonb
             OR item->'object_entity_type' <> 'null'::jsonb
             OR item->'object_display_name' <> 'null'::jsonb
           )
         )
         OR (
           item->>'object_kind' = 'entity'
           AND (
             item->'object_literal' <> 'null'::jsonb
             OR pg_catalog.jsonb_typeof(item->'object_entity_key') <> 'string'
             OR pg_catalog.jsonb_typeof(item->'object_entity_type') <> 'string'
             OR pg_catalog.jsonb_typeof(item->'object_display_name') <> 'string'
             OR pg_catalog.octet_length(item->>'object_entity_key')
                  NOT BETWEEN 1 AND 200
             OR pg_catalog.octet_length(item->>'object_display_name')
                  NOT BETWEEN 1 AND 256
             OR COALESCE(item->>'object_entity_type', '') NOT IN (
               'self', 'person', 'pet', 'organization', 'place', 'other'
             )
             OR item->>'object_entity_key'
                  IS DISTINCT FROM memory_private.source_local_entity_key(
                    job.owner_user_id, call.selection_binding_sha256,
                    CASE WHEN item->>'fact_index' ~ '^[0-7]$'
                      THEN (item->>'fact_index')::integer ELSE NULL::integer END,
                    'object',
                    item->>'object_entity_type'
                  )
           )
         )
         OR NOT EXISTS (
           SELECT 1 FROM memory.predicate_catalog AS predicate
           WHERE predicate.predicate = item->>'predicate'
             AND predicate.active
             AND predicate.catalog_sha256 = call.predicate_catalog_sha256
             AND item->>'subject_entity_type' = ANY(predicate.subject_kinds)
             AND item->>'object_kind' = ANY(predicate.object_kinds)
             AND item->>'sensitivity' = ANY(predicate.sensitivities)
             AND item->>'epistemic_state' = ANY(predicate.epistemic_statuses)
         ) THEN
        RAISE EXCEPTION 'proposal item is invalid' USING ERRCODE = '22023';
      END IF;

      proposal_operation_id := (item->>'operation_id')::uuid;
      item_proposal_id := (item->>'proposal_id')::uuid;
      item_fact_index := (item->>'fact_index')::smallint;
      IF item_fact_index NOT BETWEEN 0 AND 7 THEN
        RAISE EXCEPTION 'proposal fact index is invalid'
          USING ERRCODE = '22023';
      END IF;
      computed_semantic_sha := memory_private.semantic_key_sha256(
        item->>'subject_entity_key', item->>'predicate', item->>'object_kind',
        NULLIF(item->>'object_entity_key', ''),
        CASE WHEN item->>'object_kind' = 'literal'
          THEN item->'object_literal' ELSE NULL::jsonb END
      );
      validated_fact_sha := memory_private.validated_fact_sha256(
        call.selection_binding_sha256, item_fact_index,
        item->>'subject_entity_type', item->>'subject_entity_key',
        NULLIF(item->>'subject_display_name', ''), item->>'predicate',
        item->>'object_kind', NULLIF(item->>'object_entity_type', ''),
        NULLIF(item->>'object_entity_key', ''),
        NULLIF(item->>'object_display_name', ''),
        CASE WHEN item->>'object_kind' = 'literal'
          THEN item->'object_literal' ELSE NULL::jsonb END,
        item->>'epistemic_state', item->>'sensitivity', computed_semantic_sha
      );
      IF item_proposal_id <> memory_private.uuid5(
           call.provider_call_id,
           'proposal:' || item_fact_index::text || ':' || validated_fact_sha
         )
         OR proposal_operation_id <> memory_private.uuid5(
           call.provider_call_id,
           'admission:' || item_fact_index::text || ':' || validated_fact_sha
         ) THEN
        RAISE EXCEPTION 'proposal identifiers are not deterministic'
          USING ERRCODE = '22023';
      END IF;
      derived_surface := CASE WHEN item->>'sensitivity' = 'ordinary'
        THEN 'normal' ELSE 'explicit_only' END;
      derived_requires_explicit := item->>'sensitivity' <> 'ordinary';
      computed_proposal_sha := memory_private.proposal_sha256(
        job.owner_user_id, job.job_id, job.evidence_id,
        call.provider_call_id, evidence.source_kind,
        evidence.source_message_id, evidence.source_thread_id,
        evidence.source_window_id, evidence.source_window_sha256,
        evidence.source_sha256, call.selected_sha256,
        call.selection_binding_sha256, evidence.context_message_id,
        evidence.context_sha256, call.predicate_catalog_sha256,
        call.request_sha256, p_response_sha256, 'new_claim',
        NULL::uuid, NULL::uuid, NULL::integer, NULL::text, NULL::text,
        NULL::text, NULL::integer, NULL::text, item_fact_index,
        item_proposal_id,
        proposal_operation_id,
        item->>'subject_entity_type', item->>'subject_entity_key',
        item->>'subject_display_name', item->>'predicate',
        item->>'object_kind', NULLIF(item->>'object_entity_type', ''),
        NULLIF(item->>'object_entity_key', ''),
        NULLIF(item->>'object_display_name', ''),
        CASE WHEN item->>'object_kind' = 'literal'
          THEN item->'object_literal' ELSE NULL::jsonb END,
        item->>'epistemic_state', item->>'sensitivity',
        computed_semantic_sha, true, ARRAY[]::text[],
        ARRAY[]::text[], derived_surface, derived_requires_explicit,
        NULL::timestamptz, NULL::timestamptz
      );
      IF item->>'semantic_key_sha256' <> computed_semantic_sha
         OR item->>'proposal_sha256' <> computed_proposal_sha THEN
        RAISE EXCEPTION 'proposal framed hash mismatch'
          USING ERRCODE = '22023';
      END IF;

      INSERT INTO memory.proposal(
        proposal_id, owner_user_id, evidence_id, provider_call_id,
        operation_id, proposal_purpose, fact_index, proposal_sha256,
        semantic_key_sha256, selected_sha256, selection_binding_sha256,
        predicate_catalog_sha256, subject_entity_key, subject_entity_type,
        subject_display_name, predicate, object_kind, object_entity_key,
        object_entity_type, object_display_name, object_literal,
        epistemic_state, projectable, sensitivity, domains, intents,
        surface, requires_explicit, valid_from, valid_to, expires_at,
        purge_after
      ) VALUES (
        item_proposal_id, job.owner_user_id, job.evidence_id,
        call.provider_call_id, proposal_operation_id, 'new_claim',
        item_fact_index, computed_proposal_sha, computed_semantic_sha,
        call.selected_sha256, call.selection_binding_sha256,
        call.predicate_catalog_sha256, item->>'subject_entity_key',
        item->>'subject_entity_type', item->>'subject_display_name',
        item->>'predicate', item->>'object_kind',
        NULLIF(item->>'object_entity_key', ''),
        NULLIF(item->>'object_entity_type', ''),
        NULLIF(item->>'object_display_name', ''),
        CASE WHEN item->>'object_kind' = 'literal'
          THEN item->'object_literal' ELSE NULL::jsonb END,
        item->>'epistemic_state', true, item->>'sensitivity',
        ARRAY[]::text[], ARRAY[]::text[], derived_surface,
        derived_requires_explicit, NULL, NULL,
        pg_catalog.clock_timestamp() + interval '7 days',
        pg_catalog.clock_timestamp() + interval '30 days'
      );
      item_count := item_count + 1;
    END LOOP;

    UPDATE memory.provider_call
    SET state = 'completed', response_sha256 = p_response_sha256,
        input_tokens = p_input_tokens, output_tokens = p_output_tokens,
        completed_at = pg_catalog.clock_timestamp()
    WHERE provider_call.owner_user_id = job.owner_user_id
      AND provider_call.provider_call_id = call.provider_call_id;
    UPDATE memory.extraction_job
    SET state = 'completed', completed_at = pg_catalog.clock_timestamp(),
        lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
        lease_expires_at = NULL, last_error_code = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE extraction_job.owner_user_id = job.owner_user_id
      AND extraction_job.job_id = job.job_id;
  ELSE
    IF p_response_sha256 IS NOT NULL OR p_error_code IS NULL
       OR p_input_tokens IS NOT NULL OR p_output_tokens IS NOT NULL
       OR p_proposals IS DISTINCT FROM '[]'::jsonb THEN
      RAISE EXCEPTION 'failed extraction payload is invalid'
        USING ERRCODE = '22023';
    END IF;
    UPDATE memory.provider_call
    SET state = p_outcome, error_code = p_error_code,
        input_tokens = p_input_tokens, output_tokens = p_output_tokens,
        completed_at = pg_catalog.clock_timestamp()
    WHERE provider_call.owner_user_id = job.owner_user_id
      AND provider_call.provider_call_id = call.provider_call_id;
    UPDATE memory.extraction_job
    SET state = CASE
          WHEN p_outcome = 'retryable_failure'
               AND attempt_count < max_attempts THEN 'retryable'
          ELSE 'failed_terminal'
        END,
        available_at = CASE
          WHEN p_outcome = 'retryable_failure'
               AND attempt_count < max_attempts
          THEN pg_catalog.clock_timestamp() + interval '30 seconds'
          ELSE available_at
        END,
        lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
        lease_expires_at = NULL, last_error_code = p_error_code,
        completed_at = CASE
          WHEN p_outcome = 'retryable_failure'
               AND attempt_count < max_attempts THEN NULL
          ELSE pg_catalog.clock_timestamp() END,
        updated_at = pg_catalog.clock_timestamp()
    WHERE extraction_job.owner_user_id = job.owner_user_id
      AND extraction_job.job_id = job.job_id;
  END IF;

  completion_event_hash := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(
      job.owner_user_id::text || '|' || call.provider_call_id::text || '|'
      || p_outcome || '|' || COALESCE(p_request_sha256, '-') || '|'
      || COALESCE(p_response_sha256, '-') || '|'
      || COALESCE(p_input_tokens::text, '-') || '|'
      || COALESCE(p_output_tokens::text, '-') || '|'
      || COALESCE(p_error_code, '-') || '|'
      || call.selection_binding_sha256, 'UTF8'
    )
  ), 'hex');
  INSERT INTO memory.audit_event(
    owner_user_id, operation_id, actor_kind, object_type, object_id,
    transition_code, prior_state_sha256, new_state_sha256,
    reason_code, event_sha256
  ) VALUES (
    job.owner_user_id, call.operation_id, 'worker', 'provider_call',
    call.provider_call_id, 'provider_call_' || p_outcome,
    call.idempotency_sha256, completion_event_hash,
    COALESCE(p_error_code, p_outcome), completion_event_hash
  );

  IF p_outcome IN ('terminal_failure', 'outcome_unknown')
     OR (p_outcome = 'completed' AND item_count = 0)
     OR (
       p_outcome = 'retryable_failure'
       AND job.attempt_count >= job.max_attempts
     ) THEN
    UPDATE memory.evidence AS stored_evidence
    SET review_excerpt = NULL, excerpt_expires_at = NULL
    WHERE stored_evidence.owner_user_id = evidence.owner_user_id
      AND stored_evidence.evidence_id = evidence.evidence_id;
  END IF;

  RETURN QUERY SELECT p_outcome, item_count;
END;
$function$;

CREATE FUNCTION memory_private.list_proposals(
  p_limit integer,
  p_before timestamptz,
  p_before_id uuid
)
RETURNS TABLE(
  proposal_id uuid,
  operation_id uuid,
  proposal_sha256 text,
  source_sha256 text,
  selected_sha256 text,
  selection_binding_sha256 text,
  predicate_catalog_sha256 text,
  source_excerpt text,
  subject_entity_type text,
  subject_entity_key text,
  subject_display_name text,
  predicate text,
  object_kind text,
  object_entity_type text,
  object_entity_key text,
  object_display_name text,
  object_literal jsonb,
  epistemic_state text,
  sensitivity text,
  projectable boolean,
  domains text[],
  intents text[],
  surface text,
  requires_explicit boolean,
  valid_from timestamptz,
  valid_to timestamptz,
  correction_of_claim_id uuid,
  expires_at timestamptz,
  created_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'governed_memory_api' THEN
    RAISE EXCEPTION 'api role required' USING ERRCODE = '42501';
  END IF;
  actor := memory_private.current_owner_id();
  IF actor IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
     OR (p_before IS NULL) <> (p_before_id IS NULL) THEN
    RAISE EXCEPTION 'invalid proposal-list input' USING ERRCODE = '22023';
  END IF;
  RETURN QUERY
  SELECT proposal.proposal_id, proposal.operation_id,
         proposal.proposal_sha256,
         evidence.source_sha256,
         proposal.selected_sha256, proposal.selection_binding_sha256,
         proposal.predicate_catalog_sha256,
         evidence.review_excerpt,
         proposal.subject_entity_type, proposal.subject_entity_key,
         proposal.subject_display_name, proposal.predicate,
         proposal.object_kind, proposal.object_entity_type,
         proposal.object_entity_key, proposal.object_display_name,
         proposal.object_literal, proposal.epistemic_state,
         proposal.sensitivity, proposal.projectable,
         proposal.domains, proposal.intents, proposal.surface,
         proposal.requires_explicit, proposal.valid_from,
         proposal.valid_to, proposal.correction_of_claim_id,
         proposal.expires_at, proposal.created_at
  FROM memory.proposal AS proposal
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = proposal.owner_user_id
   AND evidence.evidence_id = proposal.evidence_id
  WHERE proposal.owner_user_id = actor
    AND proposal.review_state = 'pending_review'
    AND proposal.expires_at > pg_catalog.clock_timestamp()
    AND (
      p_before IS NULL
      OR (proposal.created_at, proposal.proposal_id) < (p_before, p_before_id)
    )
  ORDER BY proposal.created_at DESC, proposal.proposal_id DESC
  LIMIT p_limit;
END;
$function$;

CREATE FUNCTION memory_private.read_claim_candidates(p_claim_ids uuid[])
RETURNS TABLE(
  owner_user_id uuid,
  claim_id uuid,
  revision_id uuid,
  revision_number integer,
  revision_sha256 text,
  projection_sequence integer,
  lifecycle_state text,
  state_sha256 text,
  semantic_key_sha256 text,
  claim_identity_sha256 text,
  source_sha256 text,
  is_current boolean,
  projectable boolean,
  predicate_catalog_sha256 text,
  selection_binding_sha256 text,
  subject_entity_key text,
  subject_entity_type text,
  subject_display_name text,
  retrieval_text text,
  retrieval_text_sha256 text,
  predicate text,
  object_kind text,
  object_entity_key text,
  object_entity_type text,
  object_display_name text,
  object_literal jsonb,
  epistemic_state text,
  sensitivity text,
  domains text[],
  intents text[],
  surface text,
  requires_explicit boolean,
  valid_from timestamptz,
  valid_to timestamptz,
  updated_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'governed_memory_api' THEN
    RAISE EXCEPTION 'api role required' USING ERRCODE = '42501';
  END IF;
  actor := memory_private.current_owner_id();
  IF actor IS NULL OR p_claim_ids IS NULL
     OR pg_catalog.cardinality(p_claim_ids) NOT BETWEEN 1 AND 100
     OR pg_catalog.array_position(p_claim_ids, NULL) IS NOT NULL
     OR (
       SELECT pg_catalog.count(*)
       FROM (SELECT DISTINCT value FROM pg_catalog.unnest(p_claim_ids) AS value)
       AS distinct_ids
     ) <> pg_catalog.cardinality(p_claim_ids) THEN
    RAISE EXCEPTION 'invalid candidate IDs' USING ERRCODE = '22023';
  END IF;
  RETURN QUERY
  SELECT claim.owner_user_id, claim.claim_id, revision.revision_id,
         revision.revision_number, revision.revision_sha256,
         claim.projection_sequence, claim.lifecycle_state,
         claim.current_state_sha256, claim.semantic_key_sha256,
         claim.claim_identity_sha256, revision.source_sha256,
         revision.revision_id = claim.current_revision_id,
         revision.projectable, revision.predicate_catalog_sha256,
         revision.selection_binding_sha256,
         revision.subject_entity_key, revision.subject_entity_type,
         revision.subject_display_name,
         revision.retrieval_text, revision.retrieval_text_sha256,
         revision.predicate, revision.object_kind,
         revision.object_entity_key, revision.object_entity_type,
         revision.object_display_name, revision.object_literal,
         revision.epistemic_state,
         revision.sensitivity, revision.domains, revision.intents,
         revision.surface, revision.requires_explicit,
         revision.valid_from, revision.valid_to, claim.updated_at
  FROM memory.claim AS claim
  JOIN memory.claim_revision AS revision
    ON revision.owner_user_id = claim.owner_user_id
   AND revision.claim_id = claim.claim_id
   AND revision.revision_id = claim.current_revision_id
   AND revision.revision_number = claim.current_revision_number
  JOIN memory.entity AS subject
    ON subject.owner_user_id = revision.owner_user_id
   AND subject.entity_id = revision.subject_entity_id
  LEFT JOIN memory.entity AS object_entity
    ON object_entity.owner_user_id = revision.owner_user_id
   AND object_entity.entity_id = revision.object_entity_id
  WHERE claim.owner_user_id = actor
    AND claim.claim_id = ANY(p_claim_ids)
    AND claim.lifecycle_state = 'active'
    AND revision.projectable
    AND claim.current_state_sha256 = memory_private.claim_state_sha256(
      claim.owner_user_id, claim.claim_id, claim.semantic_key_sha256,
      claim.claim_identity_sha256, claim.lifecycle_state, true,
      claim.current_revision_id,
      claim.current_revision_number, revision.revision_sha256,
      claim.projection_sequence
    )
    AND revision.surface <> 'never'
    AND (revision.valid_from IS NULL OR revision.valid_from <= pg_catalog.clock_timestamp())
    AND (revision.valid_to IS NULL OR revision.valid_to > pg_catalog.clock_timestamp())
  ORDER BY pg_catalog.array_position(p_claim_ids, claim.claim_id);
END;
$function$;

CREATE FUNCTION memory_private.read_projection_rebuild_batch(
  p_after_owner_user_id uuid,
  p_after_claim_id uuid,
  p_limit integer
)
RETURNS TABLE(
  owner_user_id uuid,
  claim_id uuid,
  revision_id uuid,
  revision_number integer,
  revision_sha256 text,
  projection_sequence integer,
  lifecycle_state text,
  state_sha256 text,
  semantic_key_sha256 text,
  claim_identity_sha256 text,
  source_sha256 text,
  is_current boolean,
  projectable boolean,
  predicate_catalog_sha256 text,
  selection_binding_sha256 text,
  subject_entity_key text,
  subject_entity_type text,
  subject_display_name text,
  retrieval_text text,
  retrieval_text_sha256 text,
  predicate text,
  object_kind text,
  object_entity_key text,
  object_entity_type text,
  object_display_name text,
  object_literal jsonb,
  epistemic_state text,
  sensitivity text,
  domains text[],
  intents text[],
  surface text,
  requires_explicit boolean,
  valid_from timestamptz,
  valid_to timestamptz,
  updated_at timestamptz,
  outbox_id uuid,
  projection_operation_id uuid,
  projection_operation text,
  outbox_sequence_number integer,
  point_id uuid,
  collection_alias text,
  projection_contract_sha256 text,
  dimensions integer,
  embedding_model text,
  renderer_sha256 text,
  projection_manifest_sha256 text,
  embedding_input_sha256 text,
  applied_vector_sha256 text,
  applied_physical_collection text,
  applied_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
     OR (p_after_owner_user_id IS NULL) <> (p_after_claim_id IS NULL) THEN
    RAISE EXCEPTION 'invalid projection rebuild cursor'
      USING ERRCODE = '22023';
  END IF;
  RETURN QUERY
  SELECT claim.owner_user_id, claim.claim_id, revision.revision_id,
         revision.revision_number, revision.revision_sha256,
         claim.projection_sequence, claim.lifecycle_state,
         claim.current_state_sha256, claim.semantic_key_sha256,
         claim.claim_identity_sha256, revision.source_sha256,
         true, revision.projectable, revision.predicate_catalog_sha256,
         revision.selection_binding_sha256,
         revision.subject_entity_key, revision.subject_entity_type,
         revision.subject_display_name,
         revision.retrieval_text, revision.retrieval_text_sha256,
         revision.predicate, revision.object_kind,
         revision.object_entity_key, revision.object_entity_type,
         revision.object_display_name, revision.object_literal,
         revision.epistemic_state, revision.sensitivity,
         revision.domains, revision.intents, revision.surface,
         revision.requires_explicit, revision.valid_from,
         revision.valid_to, claim.updated_at,
         outbox.outbox_id, outbox.operation_id, outbox.operation,
         outbox.sequence_number, outbox.point_id, outbox.collection_alias,
         memory_private.projection_contract_sha256(), 3072,
         'text-embedding-3-large'::text,
         'f77b782b3e549b30a46b4beb7e25b248018f6c0f3597b36d0020103570e66442',
         outbox.projection_manifest_sha256,
         revision.retrieval_text_sha256, outbox.vector_sha256,
         outbox.physical_collection_name, outbox.applied_at
  FROM memory.claim AS claim
  JOIN memory.claim_revision AS revision
    ON revision.owner_user_id = claim.owner_user_id
   AND revision.claim_id = claim.claim_id
   AND revision.revision_id = claim.current_revision_id
   AND revision.revision_number = claim.current_revision_number
  JOIN memory.projection_outbox AS outbox
    ON outbox.owner_user_id = claim.owner_user_id
   AND outbox.claim_id = claim.claim_id
   AND outbox.revision_id = claim.current_revision_id
   AND outbox.sequence_number = claim.projection_sequence
  WHERE claim.lifecycle_state = 'active'
    AND revision.projectable
    AND outbox.operation = 'upsert'
    AND outbox.state = 'applied'
    AND outbox.vector_sha256 IS NOT NULL
    AND outbox.physical_collection_name IS NOT NULL
    AND claim.current_state_sha256 = memory_private.claim_state_sha256(
      claim.owner_user_id, claim.claim_id, claim.semantic_key_sha256,
      claim.claim_identity_sha256, claim.lifecycle_state, true,
      claim.current_revision_id, claim.current_revision_number,
      revision.revision_sha256, claim.projection_sequence
    )
    AND outbox.projection_manifest_sha256
          = memory_private.projection_manifest_sha256(
              outbox.owner_user_id, outbox.claim_id, outbox.revision_id,
              outbox.operation_id, outbox.operation,
              outbox.sequence_number, outbox.revision_sha256,
              outbox.selection_binding_sha256,
              revision.retrieval_text_sha256,
              revision.retrieval_text_sha256
            )
    AND (
      p_after_owner_user_id IS NULL
      OR (claim.owner_user_id, claim.claim_id)
           > (p_after_owner_user_id, p_after_claim_id)
    )
  ORDER BY claim.owner_user_id, claim.claim_id
  LIMIT p_limit;
END;
$function$;

CREATE FUNCTION memory_private.list_claims(
  p_claim_id uuid,
  p_limit integer,
  p_before timestamptz,
  p_before_id uuid
)
RETURNS TABLE(
  claim_id uuid,
  lifecycle_state text,
  revision_id uuid,
  revision_number integer,
  revision_sha256 text,
  current_state_sha256 text,
  revision_fact_policy_sha256 text,
  predicate_catalog_sha256 text,
  selected_sha256 text,
  selection_binding_sha256 text,
  object_kind text,
  predicate text,
  epistemic_state text,
  sensitivity text,
  updated_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'governed_memory_api' THEN
    RAISE EXCEPTION 'api role required' USING ERRCODE = '42501';
  END IF;
  actor := memory_private.current_owner_id();
  IF actor IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
     OR (p_before IS NULL) <> (p_before_id IS NULL) THEN
    RAISE EXCEPTION 'invalid claim-list input' USING ERRCODE = '22023';
  END IF;
  RETURN QUERY
  SELECT claim.claim_id, claim.lifecycle_state, revision.revision_id,
         revision.revision_number, revision.revision_sha256,
         claim.current_state_sha256, revision.fact_policy_sha256,
         revision.predicate_catalog_sha256, revision.selected_sha256,
         revision.selection_binding_sha256, revision.object_kind,
         revision.predicate, revision.epistemic_state,
         revision.sensitivity, claim.updated_at
  FROM memory.claim AS claim
  JOIN memory.claim_revision AS revision
    ON revision.owner_user_id = claim.owner_user_id
   AND revision.claim_id = claim.claim_id
   AND revision.revision_id = claim.current_revision_id
  WHERE claim.owner_user_id = actor
    AND (p_claim_id IS NULL OR claim.claim_id = p_claim_id)
    AND (
      p_before IS NULL
      OR (claim.updated_at, claim.claim_id) < (p_before, p_before_id)
    )
  ORDER BY claim.updated_at DESC, claim.claim_id DESC
  LIMIT p_limit;
END;
$function$;

CREATE FUNCTION memory_private.read_operation(p_operation_id uuid)
RETURNS TABLE(
  operation_id uuid,
  transition_code text,
  object_type text,
  object_id uuid,
  reason_code text,
  new_state_sha256 text,
  created_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'governed_memory_api' THEN
    RAISE EXCEPTION 'api role required' USING ERRCODE = '42501';
  END IF;
  actor := memory_private.current_owner_id();
  IF actor IS NULL OR p_operation_id IS NULL THEN
    RAISE EXCEPTION 'invalid operation read' USING ERRCODE = '22023';
  END IF;
  RETURN QUERY
  SELECT event.operation_id, event.transition_code, event.object_type,
         event.object_id, event.reason_code, event.new_state_sha256,
         event.created_at
  FROM memory.audit_event AS event
  WHERE event.owner_user_id = actor
    AND event.operation_id = p_operation_id
  ORDER BY event.created_at, event.transition_code;
END;
$function$;

CREATE FUNCTION memory_private.read_status()
RETURNS TABLE(
  active_claims bigint,
  pending_proposals bigint,
  pending_projection bigint,
  failed_projection bigint,
  last_transition_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user <> 'governed_memory_api' THEN
    RAISE EXCEPTION 'api role required' USING ERRCODE = '42501';
  END IF;
  actor := memory_private.current_owner_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'authenticated owner context required'
      USING ERRCODE = '42501';
  END IF;
  RETURN QUERY
  SELECT
    (SELECT pg_catalog.count(*) FROM memory.claim
      WHERE owner_user_id = actor AND lifecycle_state = 'active'),
    (SELECT pg_catalog.count(*) FROM memory.proposal
      WHERE owner_user_id = actor AND review_state = 'pending_review'
        AND expires_at > pg_catalog.clock_timestamp()),
    (SELECT pg_catalog.count(*) FROM memory.projection_outbox
      WHERE owner_user_id = actor AND state IN ('pending', 'retryable', 'claimed')),
    (SELECT pg_catalog.count(*) FROM memory.projection_outbox
      WHERE owner_user_id = actor AND state = 'failed_terminal'),
    (SELECT pg_catalog.max(created_at) FROM memory.audit_event
      WHERE owner_user_id = actor);
END;
$function$;

CREATE FUNCTION memory_private.review_proposal(
  p_operation_id uuid,
  p_proposal_id uuid,
  p_decision text,
  p_expected_proposal_sha256 text,
  p_expected_source_sha256 text,
  p_expected_selected_sha256 text,
  p_expected_selection_binding_sha256 text,
  p_expected_predicate_catalog_sha256 text,
  p_reason_codes text[]
)
RETURNS TABLE(
  outcome text,
  claim_id uuid,
  revision_id uuid,
  outbox_id uuid
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  preview_correction_claim_id uuid;
  proposal memory.proposal%ROWTYPE;
  proposal_evidence memory.evidence%ROWTYPE;
  proposal_call memory.provider_call%ROWTYPE;
  target_claim memory.claim%ROWTYPE;
  current_revision memory.claim_revision%ROWTYPE;
  subject_id uuid;
  object_id uuid;
  resulting_claim_id uuid;
  resulting_revision_id uuid;
  resulting_outbox_id uuid;
  resulting_revision_number integer;
  resulting_sequence integer;
  retrieval_text text;
  retrieval_hash text;
  fact_policy_hash text;
  revision_hash text;
  claim_identity_hash text;
  prior_state_hash text;
  new_state_hash text;
  transition_at timestamptz;
  projection_hash text;
  projection_operation_id uuid;
  existing_projection memory.projection_outbox%ROWTYPE;
  event_hash text;
  restore_event_hash text;
  recomputed_proposal_hash text;
BEGIN
  IF session_user <> 'governed_memory_api' THEN
    RAISE EXCEPTION 'api role required' USING ERRCODE = '42501';
  END IF;
  actor := memory_private.current_owner_id();
  IF actor IS NULL OR p_operation_id IS NULL OR p_proposal_id IS NULL
     OR p_decision IS NULL OR p_decision NOT IN ('admitted', 'rejected')
     OR p_expected_proposal_sha256 IS NULL
     OR p_expected_proposal_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_source_sha256 IS NULL
     OR p_expected_source_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_selected_sha256 IS NULL
     OR p_expected_selected_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_selection_binding_sha256 IS NULL
     OR p_expected_selection_binding_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_predicate_catalog_sha256 IS NULL
     OR p_expected_predicate_catalog_sha256 !~ '^[0-9a-f]{64}$'
     OR p_reason_codes IS NULL
     OR (
       p_decision = 'admitted'
       AND p_reason_codes IS DISTINCT FROM
         ARRAY['explicit_owner_review']::text[]
     )
     OR (
       p_decision = 'rejected'
       AND NOT memory_private.is_sorted_unique_allowlist(
         p_reason_codes,
         ARRAY[
           'duplicate_existing', 'not_durable', 'proposal_incorrect'
         ]::text[]
       )
     ) THEN
    RAISE EXCEPTION 'invalid proposal review input' USING ERRCODE = '22023';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      actor::text || '|operation|' || p_operation_id::text, 0
    )
  );
  SELECT value.correction_of_claim_id INTO preview_correction_claim_id
  FROM memory.proposal AS value
  WHERE value.owner_user_id = actor AND value.proposal_id = p_proposal_id;
  IF NOT FOUND THEN
    IF EXISTS (
      SELECT 1
      FROM memory.audit_event AS receipt
      WHERE receipt.owner_user_id = actor
        AND receipt.object_type = 'proposal'
        AND receipt.object_id = p_proposal_id
        AND receipt.transition_code = 'proposal_retention_purged'
        AND receipt.reason_code = 'retention_expired'
    ) THEN
      RAISE EXCEPTION 'proposal_retention_purged'
        USING ERRCODE = 'P0002';
    END IF;
    RAISE EXCEPTION 'proposal not found' USING ERRCODE = 'P0002';
  END IF;
  IF preview_correction_claim_id IS NOT NULL THEN
    PERFORM pg_catalog.pg_advisory_xact_lock(
      pg_catalog.hashtextextended(
        actor::text || '|claim|'
          || preview_correction_claim_id::text,
        0
      )
    );
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      actor::text || '|proposal|' || p_proposal_id::text, 0
    )
  );
  SELECT value.* INTO STRICT proposal
  FROM memory.proposal AS value
  WHERE value.owner_user_id = actor AND value.proposal_id = p_proposal_id
  FOR UPDATE;
  IF proposal.operation_id IS DISTINCT FROM p_operation_id
     OR proposal.correction_of_claim_id
          IS DISTINCT FROM preview_correction_claim_id THEN
    RAISE EXCEPTION 'proposal lock preview changed'
      USING ERRCODE = '40001';
  END IF;
  SELECT value.* INTO STRICT proposal_evidence
  FROM memory.evidence AS value
  WHERE value.owner_user_id = actor
    AND value.evidence_id = proposal.evidence_id;
  IF proposal.provider_call_id IS NOT NULL THEN
    SELECT value.* INTO STRICT proposal_call
    FROM memory.provider_call AS value
    WHERE value.owner_user_id = actor
      AND value.provider_call_id = proposal.provider_call_id;
  END IF;
  recomputed_proposal_hash := memory_private.proposal_sha256(
    actor,
    CASE WHEN proposal.provider_call_id IS NOT NULL
      THEN proposal_call.job_id ELSE NULL::uuid END,
    proposal.evidence_id, proposal.provider_call_id,
    proposal_evidence.source_kind, proposal_evidence.source_message_id,
    proposal_evidence.source_thread_id, proposal_evidence.source_window_id,
    proposal_evidence.source_window_sha256, proposal_evidence.source_sha256,
    proposal.selected_sha256, proposal.selection_binding_sha256,
    proposal_evidence.context_message_id, proposal_evidence.context_sha256,
    proposal.predicate_catalog_sha256,
    CASE WHEN proposal.provider_call_id IS NOT NULL
      THEN proposal_call.request_sha256 ELSE NULL::text END,
    CASE WHEN proposal.provider_call_id IS NOT NULL
      THEN proposal_call.response_sha256 ELSE NULL::text END,
    proposal.proposal_purpose, proposal.correction_of_claim_id,
    proposal.correction_target_revision_id,
    proposal.correction_target_revision_number,
    proposal.expected_revision_sha256,
    proposal.correction_target_state_sha256,
    proposal.correction_target_identity_sha256,
    proposal.correction_target_projection_sequence,
    proposal.correction_pending_state_sha256, proposal.fact_index,
    proposal.proposal_id, proposal.operation_id,
    proposal.subject_entity_type, proposal.subject_entity_key,
    proposal.subject_display_name, proposal.predicate, proposal.object_kind,
    proposal.object_entity_type, proposal.object_entity_key,
    proposal.object_display_name, proposal.object_literal,
    proposal.epistemic_state, proposal.sensitivity,
    proposal.semantic_key_sha256,
    proposal.projectable, proposal.domains, proposal.intents,
    proposal.surface, proposal.requires_explicit, proposal.valid_from,
    proposal.valid_to
  );
  IF proposal.proposal_sha256 <> p_expected_proposal_sha256
     OR proposal.proposal_sha256 <> recomputed_proposal_hash
     OR proposal_evidence.source_sha256 <> p_expected_source_sha256
     OR proposal.selected_sha256 <> p_expected_selected_sha256
     OR proposal.selection_binding_sha256
          <> p_expected_selection_binding_sha256
     OR proposal.predicate_catalog_sha256
          <> p_expected_predicate_catalog_sha256
     OR proposal_evidence.selected_sha256 <> p_expected_selected_sha256
     OR proposal_evidence.selection_binding_sha256
          <> p_expected_selection_binding_sha256
     OR (
       proposal.provider_call_id IS NOT NULL
       AND (
         proposal_call.state <> 'completed'
         OR proposal_call.selected_sha256 <> proposal.selected_sha256
         OR proposal_call.selection_binding_sha256
              <> proposal.selection_binding_sha256
         OR proposal_call.predicate_catalog_sha256
              <> proposal.predicate_catalog_sha256
         OR NOT EXISTS (
           SELECT 1 FROM memory.extraction_job AS job
           WHERE job.owner_user_id = actor
             AND job.job_id = proposal_call.job_id
             AND job.evidence_id = proposal.evidence_id
             AND job.state = 'completed'
         )
       )
     )
     OR proposal.semantic_key_sha256 <> memory_private.semantic_key_sha256(
          proposal.subject_entity_key, proposal.predicate,
          proposal.object_kind, proposal.object_entity_key,
          proposal.object_literal
        )
     OR (
       proposal.proposal_purpose = 'correction'
       AND proposal.correction_target_identity_sha256
             <> memory_private.claim_identity_sha256(
                  proposal.subject_entity_key, proposal.predicate
                )
     )
     OR NOT proposal.projectable
     OR pg_catalog.cardinality(proposal.domains) <> 0
     OR pg_catalog.cardinality(proposal.intents) <> 0
     OR proposal.valid_from IS NOT NULL
     OR proposal.valid_to IS NOT NULL
     OR proposal.surface <> (
       CASE WHEN proposal.sensitivity = 'ordinary'
         THEN 'normal' ELSE 'explicit_only' END
     )
     OR proposal.requires_explicit
          <> (proposal.sensitivity <> 'ordinary')
     OR NOT EXISTS (
       SELECT 1 FROM memory.predicate_catalog AS predicate
       WHERE predicate.active
         AND predicate.predicate = proposal.predicate
         AND predicate.catalog_sha256 = p_expected_predicate_catalog_sha256
         AND proposal.subject_entity_type = ANY(predicate.subject_kinds)
         AND proposal.object_kind = ANY(predicate.object_kinds)
         AND proposal.sensitivity = ANY(predicate.sensitivities)
         AND proposal.epistemic_state = ANY(predicate.epistemic_statuses)
     ) THEN
    RAISE EXCEPTION 'proposal evidence or catalog CAS mismatch'
      USING ERRCODE = '40001';
  END IF;
  event_hash := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(
      actor::text || '|' || p_operation_id::text || '|'
      || proposal.proposal_id::text || '|' || p_decision || '|'
      || proposal.proposal_sha256 || '|' || proposal_evidence.source_sha256
      || '|' || proposal.selected_sha256 || '|'
      || proposal.selection_binding_sha256 || '|'
      || proposal.predicate_catalog_sha256 || '|'
      || memory_private.framed_text_array(
           'reason_codes', p_reason_codes
         ), 'UTF8'
    )
  ), 'hex');

  IF proposal.review_state <> 'pending_review' THEN
    IF proposal.review_state <> p_decision
       OR proposal.reviewer_kind <> 'owner'
       OR proposal.reviewer_user_id <> actor
       OR proposal.review_reason_codes IS DISTINCT FROM p_reason_codes
       OR NOT EXISTS (
         SELECT 1 FROM memory.audit_event AS receipt
         WHERE receipt.owner_user_id = actor
           AND receipt.operation_id = p_operation_id
           AND receipt.transition_code = CASE
             WHEN p_decision = 'admitted' THEN 'proposal_admitted'
             ELSE 'proposal_rejected'
           END
           AND receipt.reason_code = CASE
             WHEN p_decision = 'admitted' THEN 'proposal_admitted'
             ELSE 'proposal_rejected'
           END
           AND receipt.event_sha256 = event_hash
       ) THEN
      RAISE EXCEPTION 'proposal already resolved differently'
        USING ERRCODE = '23514';
    END IF;
    IF p_decision = 'admitted' THEN
      SELECT revision.claim_id, revision.revision_id
      INTO STRICT resulting_claim_id, resulting_revision_id
      FROM memory.claim_revision AS revision
      WHERE revision.owner_user_id = actor
        AND revision.source_proposal_id = proposal.proposal_id;
      projection_operation_id := memory_private.derived_uuid(
        p_operation_id, 'admission-projection'
      );
      SELECT value.* INTO STRICT existing_projection
      FROM memory.projection_outbox AS value
      WHERE value.owner_user_id = actor
        AND value.operation_id = projection_operation_id;
      IF existing_projection.claim_id <> resulting_claim_id
         OR existing_projection.revision_id <> resulting_revision_id
         OR existing_projection.operation <> 'upsert'
         OR existing_projection.point_id <> resulting_claim_id THEN
        RAISE EXCEPTION 'proposal admission replay lineage drifted'
          USING ERRCODE = '23514';
      END IF;
      resulting_outbox_id := existing_projection.outbox_id;
    ELSIF proposal.correction_of_claim_id IS NOT NULL THEN
      resulting_claim_id := proposal.correction_of_claim_id;
      resulting_revision_id := NULL::uuid;
      projection_operation_id := memory_private.derived_uuid(
        p_operation_id, 'correction-rejection-projection'
      );
      SELECT value.* INTO STRICT existing_projection
      FROM memory.projection_outbox AS value
      WHERE value.owner_user_id = actor
        AND value.operation_id = projection_operation_id;
      IF existing_projection.claim_id <> proposal.correction_of_claim_id
         OR existing_projection.operation <> 'upsert'
         OR existing_projection.point_id <> proposal.correction_of_claim_id
         OR NOT EXISTS (
           SELECT 1 FROM memory.audit_event AS restored
           WHERE restored.owner_user_id = actor
             AND restored.operation_id = p_operation_id
             AND restored.transition_code = 'correction_rejected_restored'
             AND restored.object_type = 'claim'
             AND restored.object_id = proposal.correction_of_claim_id
             AND restored.reason_code = 'proposal_rejected'
         ) THEN
        RAISE EXCEPTION 'proposal rejection replay lineage drifted'
          USING ERRCODE = '23514';
      END IF;
      resulting_outbox_id := existing_projection.outbox_id;
    ELSE
      resulting_claim_id := NULL::uuid;
      resulting_revision_id := NULL::uuid;
      resulting_outbox_id := NULL::uuid;
    END IF;
    RETURN QUERY SELECT 'replayed'::text, resulting_claim_id,
      resulting_revision_id, resulting_outbox_id;
    RETURN;
  END IF;
  IF memory_private.operation_id_conflicts(
       actor, p_operation_id, p_proposal_id, NULL::uuid,
       proposal.correction_of_claim_id, ARRAY[]::text[]
     ) THEN
    RAISE EXCEPTION 'operation id is reserved by another mutation'
      USING ERRCODE = '23514';
  END IF;
  IF proposal.expires_at <= pg_catalog.transaction_timestamp() THEN
    RAISE EXCEPTION 'proposal expired' USING ERRCODE = '40001';
  END IF;

  IF p_decision = 'rejected' THEN
    UPDATE memory.proposal AS stored_proposal
    SET review_state = 'rejected', reviewer_kind = 'owner',
        reviewer_user_id = actor,
        review_reason_codes = p_reason_codes,
        reviewed_at = pg_catalog.transaction_timestamp()
    WHERE stored_proposal.owner_user_id = actor
      AND stored_proposal.proposal_id = proposal.proposal_id;
    INSERT INTO memory.audit_event(
      owner_user_id, operation_id, actor_kind, actor_user_id,
      object_type, object_id, transition_code, prior_state_sha256,
      new_state_sha256, reason_code, event_sha256
    ) VALUES (
      actor, p_operation_id, 'reviewer', actor, 'proposal',
      proposal.proposal_id, 'proposal_rejected', proposal.proposal_sha256,
      event_hash, 'proposal_rejected', event_hash
    );
    IF proposal.correction_of_claim_id IS NOT NULL THEN
      SELECT value.* INTO STRICT target_claim
      FROM memory.claim AS value
      WHERE value.owner_user_id = actor
        AND value.claim_id = proposal.correction_of_claim_id
      FOR UPDATE;
      SELECT value.* INTO STRICT current_revision
      FROM memory.claim_revision AS value
      WHERE value.owner_user_id = actor
        AND value.claim_id = target_claim.claim_id
        AND value.revision_id = target_claim.current_revision_id;
      IF target_claim.lifecycle_state <> 'correction_pending'
         OR target_claim.claim_identity_sha256
              <> proposal.correction_target_identity_sha256
         OR target_claim.projection_sequence
              <> proposal.correction_target_projection_sequence + 1
         OR target_claim.current_state_sha256
              <> proposal.correction_pending_state_sha256
         OR current_revision.revision_id
              <> proposal.correction_target_revision_id
         OR current_revision.revision_number
              <> proposal.correction_target_revision_number
         OR current_revision.revision_sha256
              <> proposal.expected_revision_sha256 THEN
        RAISE EXCEPTION 'correction rejection target state changed'
          USING ERRCODE = '40001';
      END IF;
      prior_state_hash := memory_private.claim_state_sha256(
        target_claim.owner_user_id, target_claim.claim_id,
        target_claim.semantic_key_sha256, target_claim.claim_identity_sha256,
        target_claim.lifecycle_state, true,
        target_claim.current_revision_id, target_claim.current_revision_number,
        current_revision.revision_sha256, target_claim.projection_sequence
      );
      IF prior_state_hash <> target_claim.current_state_sha256
         OR NOT EXISTS (
           SELECT 1 FROM memory.audit_event AS transition
           WHERE transition.owner_user_id = actor
             AND transition.operation_id = proposal_evidence.operation_id
             AND transition.object_type = 'claim'
             AND transition.object_id = target_claim.claim_id
             AND transition.transition_code = 'correction_requested'
             AND transition.prior_state_sha256
                   = proposal.correction_target_state_sha256
             AND transition.new_state_sha256 = target_claim.current_state_sha256
         ) THEN
        RAISE EXCEPTION 'correction rejection state hash changed'
          USING ERRCODE = '40001';
      END IF;
      resulting_sequence := target_claim.projection_sequence + 1;
      resulting_outbox_id := pg_catalog.gen_random_uuid();
      projection_operation_id := memory_private.derived_uuid(
        p_operation_id, 'correction-rejection-projection'
      );
      transition_at := pg_catalog.transaction_timestamp();
      new_state_hash := memory_private.claim_state_sha256(
        target_claim.owner_user_id, target_claim.claim_id,
        target_claim.semantic_key_sha256, target_claim.claim_identity_sha256,
        'active', true,
        target_claim.current_revision_id, target_claim.current_revision_number,
        current_revision.revision_sha256, resulting_sequence
      );
      projection_hash := memory_private.projection_manifest_sha256(
        actor, target_claim.claim_id, current_revision.revision_id,
        projection_operation_id, 'upsert', resulting_sequence,
        current_revision.revision_sha256,
        current_revision.selection_binding_sha256,
        current_revision.retrieval_text_sha256,
        current_revision.retrieval_text_sha256
      );
      UPDATE memory.claim AS target_row
      SET lifecycle_state = 'active', current_state_sha256 = new_state_hash,
          correction_pending_at = NULL,
          projection_sequence = resulting_sequence,
          updated_at = transition_at
      WHERE target_row.owner_user_id = actor
        AND target_row.claim_id = target_claim.claim_id;
      INSERT INTO memory.projection_outbox(
        outbox_id, owner_user_id, claim_id, revision_id, operation_id,
        sequence_number, operation, point_id, revision_sha256,
        selection_binding_sha256, projection_manifest_sha256
      ) VALUES (
        resulting_outbox_id, actor, target_claim.claim_id,
        current_revision.revision_id, projection_operation_id,
        resulting_sequence, 'upsert', target_claim.claim_id,
        current_revision.revision_sha256,
        current_revision.selection_binding_sha256, projection_hash
      );
      restore_event_hash := pg_catalog.encode(pg_catalog.sha256(
        pg_catalog.convert_to(
          actor::text || '|' || p_operation_id::text
          || '|correction_rejected_restored|'
          || target_claim.claim_id::text || '|'
          || current_revision.revision_sha256, 'UTF8'
        )
      ), 'hex');
      INSERT INTO memory.audit_event(
        owner_user_id, operation_id, actor_kind, actor_user_id,
        object_type, object_id, transition_code, prior_state_sha256,
        new_state_sha256, reason_code, event_sha256
      ) VALUES (
        actor, p_operation_id, 'reviewer', actor, 'claim',
        target_claim.claim_id, 'correction_rejected_restored',
        prior_state_hash, new_state_hash,
        'proposal_rejected', restore_event_hash
      );
    END IF;
    RETURN QUERY SELECT 'rejected'::text,
      proposal.correction_of_claim_id, NULL::uuid, resulting_outbox_id;
    RETURN;
  END IF;

  INSERT INTO memory.entity(
    owner_user_id, entity_key, entity_type, display_name, normalized_name
  ) VALUES (
    actor, proposal.subject_entity_key, proposal.subject_entity_type,
    COALESCE(proposal.subject_display_name, proposal.subject_entity_key),
    pg_catalog.lower(
      COALESCE(proposal.subject_display_name, proposal.subject_entity_key)
    )
  ) ON CONFLICT (owner_user_id, entity_key) DO NOTHING;
  SELECT entity.entity_id INTO STRICT subject_id
  FROM memory.entity AS entity
  WHERE entity.owner_user_id = actor
    AND entity.entity_key = proposal.subject_entity_key
    AND entity.entity_type = proposal.subject_entity_type;

  IF proposal.object_kind = 'entity' THEN
    INSERT INTO memory.entity(
      owner_user_id, entity_key, entity_type, display_name, normalized_name
    ) VALUES (
      actor, proposal.object_entity_key, proposal.object_entity_type,
      proposal.object_display_name, pg_catalog.lower(proposal.object_display_name)
    ) ON CONFLICT (owner_user_id, entity_key) DO NOTHING;
    SELECT entity.entity_id INTO STRICT object_id
    FROM memory.entity AS entity
    WHERE entity.owner_user_id = actor
      AND entity.entity_key = proposal.object_entity_key
      AND entity.entity_type = proposal.object_entity_type;
  END IF;

  retrieval_text := memory_private.render_relational_fact(
    proposal.subject_entity_type, proposal.subject_entity_key,
    proposal.subject_display_name, proposal.predicate,
    proposal.object_kind, proposal.object_entity_type,
    proposal.object_entity_key, proposal.object_display_name,
    proposal.object_literal
  );
  IF pg_catalog.octet_length(retrieval_text) NOT BETWEEN 1 AND 4096 THEN
    RAISE EXCEPTION 'deterministic retrieval rendering exceeds bound'
      USING ERRCODE = '22001';
  END IF;
  retrieval_hash := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(retrieval_text, 'UTF8')
  ), 'hex');
  fact_policy_hash := memory_private.revision_fact_policy_sha256(
    proposal.subject_entity_type, proposal.subject_entity_key,
    proposal.subject_display_name, proposal.predicate,
    proposal.object_kind, proposal.object_entity_type,
    proposal.object_entity_key, proposal.object_display_name,
    proposal.object_literal, proposal.epistemic_state,
    proposal.sensitivity, proposal.projectable, proposal.domains,
    proposal.intents, proposal.surface, proposal.requires_explicit,
    proposal.valid_from, proposal.valid_to, proposal.selected_sha256,
    proposal.selection_binding_sha256, proposal.predicate_catalog_sha256,
    retrieval_hash
  );
  claim_identity_hash := memory_private.claim_identity_sha256(
    proposal.subject_entity_key, proposal.predicate
  );

  resulting_revision_id := pg_catalog.gen_random_uuid();
  transition_at := pg_catalog.transaction_timestamp();
  IF proposal.correction_of_claim_id IS NULL THEN
    IF EXISTS (
      SELECT 1 FROM memory.claim AS existing_claim
      WHERE existing_claim.owner_user_id = actor
        AND existing_claim.semantic_key_sha256 = proposal.semantic_key_sha256
    ) THEN
      RAISE EXCEPTION 'exact semantic identity already exists'
        USING ERRCODE = '23505';
    END IF;
    resulting_claim_id := pg_catalog.gen_random_uuid();
    resulting_revision_number := 1;
    resulting_sequence := 1;
  ELSE
    SELECT value.* INTO STRICT target_claim
    FROM memory.claim AS value
    WHERE value.owner_user_id = actor
      AND value.claim_id = proposal.correction_of_claim_id
    FOR UPDATE;
    IF target_claim.lifecycle_state <> 'correction_pending'
       OR target_claim.claim_identity_sha256
            <> proposal.correction_target_identity_sha256
       OR target_claim.projection_sequence
            <> proposal.correction_target_projection_sequence + 1
       OR target_claim.current_state_sha256
            <> proposal.correction_pending_state_sha256
       OR NOT EXISTS (
         SELECT 1 FROM memory.claim_revision AS expected_revision
         WHERE expected_revision.owner_user_id = actor
           AND expected_revision.claim_id = target_claim.claim_id
           AND expected_revision.revision_id = target_claim.current_revision_id
           AND expected_revision.revision_id
                 = proposal.correction_target_revision_id
           AND expected_revision.revision_number
                 = proposal.correction_target_revision_number
           AND expected_revision.revision_sha256 = proposal.expected_revision_sha256
       ) THEN
      RAISE EXCEPTION 'correction target state changed' USING ERRCODE = '40001';
    END IF;
    prior_state_hash := memory_private.claim_state_sha256(
      target_claim.owner_user_id, target_claim.claim_id,
      target_claim.semantic_key_sha256, target_claim.claim_identity_sha256,
      target_claim.lifecycle_state, true,
      target_claim.current_revision_id, target_claim.current_revision_number,
      (SELECT expected_revision.revision_sha256
       FROM memory.claim_revision AS expected_revision
       WHERE expected_revision.owner_user_id = target_claim.owner_user_id
         AND expected_revision.revision_id = target_claim.current_revision_id),
      target_claim.projection_sequence
    );
    IF prior_state_hash <> target_claim.current_state_sha256
       OR NOT EXISTS (
         SELECT 1 FROM memory.audit_event AS transition
         WHERE transition.owner_user_id = actor
           AND transition.operation_id = proposal_evidence.operation_id
           AND transition.object_type = 'claim'
           AND transition.object_id = target_claim.claim_id
           AND transition.transition_code = 'correction_requested'
           AND transition.prior_state_sha256
                 = proposal.correction_target_state_sha256
           AND transition.new_state_sha256 = target_claim.current_state_sha256
       ) THEN
      RAISE EXCEPTION 'correction target state hash changed'
        USING ERRCODE = '40001';
    END IF;
    resulting_claim_id := target_claim.claim_id;
    IF EXISTS (
      SELECT 1 FROM memory.claim AS existing_claim
      WHERE existing_claim.owner_user_id = actor
        AND existing_claim.semantic_key_sha256 = proposal.semantic_key_sha256
        AND existing_claim.claim_id <> target_claim.claim_id
    ) THEN
      RAISE EXCEPTION 'corrected semantic identity collides with another claim'
        USING ERRCODE = '23505';
    END IF;
    resulting_revision_number := target_claim.current_revision_number + 1;
    resulting_sequence := target_claim.projection_sequence + 1;
  END IF;

  revision_hash := memory_private.claim_revision_sha256(
    actor, resulting_claim_id, resulting_revision_id,
    resulting_revision_number, proposal.proposal_id,
    proposal.proposal_sha256, proposal.semantic_key_sha256,
    claim_identity_hash,
    proposal.subject_entity_type, proposal.subject_entity_key,
    proposal.subject_display_name, proposal.predicate,
    proposal.object_kind, proposal.object_entity_type,
    proposal.object_entity_key, proposal.object_display_name,
    proposal.object_literal, proposal.epistemic_state,
    proposal.sensitivity, proposal.projectable, proposal.domains,
    proposal.intents, proposal.surface, proposal.requires_explicit,
    proposal.valid_from, proposal.valid_to, proposal_evidence.source_sha256,
    proposal.selected_sha256,
    proposal.selection_binding_sha256, proposal.predicate_catalog_sha256,
    retrieval_hash
  );

  new_state_hash := memory_private.claim_state_sha256(
    actor, resulting_claim_id, proposal.semantic_key_sha256,
    claim_identity_hash, 'active', true,
    resulting_revision_id, resulting_revision_number, revision_hash,
    resulting_sequence
  );

  IF proposal.correction_of_claim_id IS NULL THEN
    INSERT INTO memory.claim(
      claim_id, owner_user_id, semantic_key_sha256, claim_identity_sha256,
      current_state_sha256,
      lifecycle_state, current_revision_id, current_revision_number,
      projection_sequence, created_at, updated_at
    ) VALUES (
      resulting_claim_id, actor, proposal.semantic_key_sha256,
      claim_identity_hash, new_state_hash, 'active', resulting_revision_id,
      resulting_revision_number, resulting_sequence,
      transition_at, transition_at
    );
  END IF;

  INSERT INTO memory.claim_revision(
    revision_id, owner_user_id, claim_id, revision_number,
    source_proposal_id, semantic_key_sha256, claim_identity_sha256,
    subject_entity_id, subject_entity_type, subject_entity_key,
    subject_display_name, predicate, object_kind, object_entity_id,
    object_entity_type, object_entity_key, object_display_name,
    object_literal, retrieval_text, retrieval_text_sha256,
    epistemic_state, projectable,
    sensitivity, domains, intents, surface, requires_explicit,
    valid_from, valid_to, source_sha256, selected_sha256,
    selection_binding_sha256,
    predicate_catalog_sha256, fact_policy_sha256, revision_sha256
  ) VALUES (
    resulting_revision_id, actor, resulting_claim_id,
    resulting_revision_number, proposal.proposal_id,
    proposal.semantic_key_sha256, claim_identity_hash, subject_id,
    proposal.subject_entity_type, proposal.subject_entity_key,
    proposal.subject_display_name, proposal.predicate,
    proposal.object_kind, object_id, proposal.object_entity_type,
    proposal.object_entity_key, proposal.object_display_name,
    proposal.object_literal, retrieval_text, retrieval_hash,
    proposal.epistemic_state, proposal.projectable, proposal.sensitivity,
    proposal.domains, proposal.intents, proposal.surface,
    proposal.requires_explicit, proposal.valid_from, proposal.valid_to,
    proposal_evidence.source_sha256,
    proposal.selected_sha256, proposal.selection_binding_sha256,
    proposal.predicate_catalog_sha256, fact_policy_hash, revision_hash
  );

  INSERT INTO memory.claim_evidence(
    owner_user_id, claim_id, revision_id, evidence_id,
    selected_sha256, selection_binding_sha256, stance, reason_codes
  ) SELECT actor, resulting_claim_id, resulting_revision_id,
      evidence.evidence_id, evidence.selected_sha256,
      evidence.selection_binding_sha256, 'supports', p_reason_codes
    FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id = actor
      AND evidence.evidence_id = proposal.evidence_id;

  IF proposal.correction_of_claim_id IS NOT NULL THEN
    UPDATE memory.claim AS target_row
    SET semantic_key_sha256 = proposal.semantic_key_sha256,
        claim_identity_sha256 = claim_identity_hash,
        current_state_sha256 = new_state_hash,
        lifecycle_state = 'active', current_revision_id = resulting_revision_id,
        current_revision_number = resulting_revision_number,
        projection_sequence = resulting_sequence,
        correction_pending_at = NULL, retracted_at = NULL,
        deletion_requested_at = NULL,
        updated_at = transition_at
    WHERE target_row.owner_user_id = actor
      AND target_row.claim_id = resulting_claim_id;
  END IF;

  UPDATE memory.proposal AS stored_proposal
  SET review_state = 'admitted', reviewer_kind = 'owner',
      reviewer_user_id = actor,
      review_reason_codes = p_reason_codes,
      reviewed_at = pg_catalog.transaction_timestamp()
  WHERE stored_proposal.owner_user_id = actor
    AND stored_proposal.proposal_id = proposal.proposal_id;

  IF proposal.projectable THEN
    resulting_outbox_id := pg_catalog.gen_random_uuid();
    projection_operation_id := memory_private.derived_uuid(
      p_operation_id, 'admission-projection'
    );
    projection_hash := memory_private.projection_manifest_sha256(
      actor, resulting_claim_id, resulting_revision_id,
      projection_operation_id, 'upsert', resulting_sequence,
      revision_hash, proposal.selection_binding_sha256,
      retrieval_hash, retrieval_hash
    );
    INSERT INTO memory.projection_outbox(
      outbox_id, owner_user_id, claim_id, revision_id, operation_id,
      sequence_number, operation, point_id, revision_sha256,
      selection_binding_sha256, projection_manifest_sha256
    ) VALUES (
      resulting_outbox_id, actor, resulting_claim_id,
      resulting_revision_id, projection_operation_id,
      resulting_sequence, 'upsert', resulting_claim_id,
      revision_hash, proposal.selection_binding_sha256, projection_hash
    );
  END IF;

  INSERT INTO memory.audit_event(
    owner_user_id, operation_id, actor_kind, actor_user_id,
    object_type, object_id, transition_code, prior_state_sha256,
    new_state_sha256, reason_code, event_sha256
  ) VALUES (
    actor, p_operation_id, 'reviewer', actor, 'claim',
    resulting_claim_id, 'proposal_admitted', prior_state_hash,
    new_state_hash, 'proposal_admitted', event_hash
  );
  RETURN QUERY SELECT 'admitted'::text, resulting_claim_id,
    resulting_revision_id, resulting_outbox_id;
END;
$function$;

CREATE FUNCTION memory_private.expire_proposals(p_limit integer)
RETURNS TABLE(expired_count integer, restored_correction_count integer)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  candidate_key record;
  candidate_preview memory.proposal%ROWTYPE;
  candidate memory.proposal%ROWTYPE;
  target_claim memory.claim%ROWTYPE;
  current_revision memory.claim_revision%ROWTYPE;
  expiry_operation_id uuid;
  new_outbox_id uuid;
  captured_at timestamptz;
  new_sequence integer;
  proposal_state_hash text;
  projection_hash text;
  prior_state_hash text;
  new_state_hash text;
  event_hash text;
  expired_total integer := 0;
  restored_total integer := 0;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
    RAISE EXCEPTION 'invalid proposal expiry limit' USING ERRCODE = '22023';
  END IF;

  FOR candidate_key IN
    SELECT proposal.owner_user_id, proposal.proposal_id
    FROM memory.proposal AS proposal
    WHERE proposal.review_state = 'pending_review'
      AND proposal.expires_at <= pg_catalog.transaction_timestamp()
    ORDER BY proposal.expires_at, proposal.proposal_id
    LIMIT p_limit
  LOOP
    SELECT proposal.* INTO candidate_preview
    FROM memory.proposal AS proposal
    WHERE proposal.owner_user_id = candidate_key.owner_user_id
      AND proposal.proposal_id = candidate_key.proposal_id;
    IF NOT FOUND THEN
      CONTINUE;
    END IF;
    expiry_operation_id := pg_catalog.gen_random_uuid();
    PERFORM pg_catalog.pg_advisory_xact_lock(
      pg_catalog.hashtextextended(
        candidate_preview.owner_user_id::text || '|operation|'
          || expiry_operation_id::text,
        0
      )
    );
    IF candidate_preview.correction_of_claim_id IS NOT NULL THEN
      PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
          candidate_preview.owner_user_id::text || '|claim|'
            || candidate_preview.correction_of_claim_id::text,
          0
        )
      );
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
      pg_catalog.hashtextextended(
        candidate_preview.owner_user_id::text || '|proposal|'
          || candidate_preview.proposal_id::text,
        0
      )
    );
    SELECT proposal.* INTO candidate
    FROM memory.proposal AS proposal
    WHERE proposal.owner_user_id = candidate_preview.owner_user_id
      AND proposal.proposal_id = candidate_preview.proposal_id
    FOR UPDATE;
    IF NOT FOUND
       OR candidate.correction_of_claim_id
            IS DISTINCT FROM candidate_preview.correction_of_claim_id
       OR candidate.review_state <> 'pending_review'
       OR candidate.expires_at > pg_catalog.transaction_timestamp() THEN
      CONTINUE;
    END IF;
    captured_at := pg_catalog.transaction_timestamp();
    proposal_state_hash := pg_catalog.encode(pg_catalog.sha256(
      pg_catalog.convert_to(
        candidate.owner_user_id::text || '|' || candidate.proposal_id::text
        || '|' || candidate.proposal_sha256 || '|expired', 'UTF8'
      )
    ), 'hex');

    IF candidate.correction_of_claim_id IS NOT NULL THEN
      SELECT claim.* INTO STRICT target_claim
      FROM memory.claim AS claim
      WHERE claim.owner_user_id = candidate.owner_user_id
        AND claim.claim_id = candidate.correction_of_claim_id
      FOR UPDATE;
      SELECT revision.* INTO STRICT current_revision
      FROM memory.claim_revision AS revision
      WHERE revision.owner_user_id = target_claim.owner_user_id
        AND revision.claim_id = target_claim.claim_id
        AND revision.revision_id = target_claim.current_revision_id;

      IF target_claim.lifecycle_state = 'correction_pending' THEN
        prior_state_hash := memory_private.claim_state_sha256(
          target_claim.owner_user_id, target_claim.claim_id,
          target_claim.semantic_key_sha256,
          target_claim.claim_identity_sha256, target_claim.lifecycle_state,
          true, target_claim.current_revision_id,
          target_claim.current_revision_number,
          current_revision.revision_sha256, target_claim.projection_sequence
        );
        IF current_revision.revision_id
             <> candidate.correction_target_revision_id
           OR current_revision.revision_number
             <> candidate.correction_target_revision_number
           OR current_revision.revision_sha256
             <> candidate.expected_revision_sha256
           OR target_claim.claim_identity_sha256
             <> candidate.correction_target_identity_sha256
           OR target_claim.projection_sequence
             <> candidate.correction_target_projection_sequence + 1
           OR target_claim.current_state_sha256
             <> candidate.correction_pending_state_sha256
           OR prior_state_hash <> target_claim.current_state_sha256
           OR NOT EXISTS (
             SELECT 1 FROM memory.audit_event AS transition
             WHERE transition.owner_user_id = candidate.owner_user_id
               AND transition.operation_id = (
                 SELECT evidence.operation_id
                 FROM memory.evidence AS evidence
                 WHERE evidence.owner_user_id = candidate.owner_user_id
                   AND evidence.evidence_id = candidate.evidence_id
               )
               AND transition.object_type = 'claim'
               AND transition.object_id = target_claim.claim_id
               AND transition.transition_code = 'correction_requested'
               AND transition.prior_state_sha256
                     = candidate.correction_target_state_sha256
               AND transition.new_state_sha256
                     = candidate.correction_pending_state_sha256
           ) THEN
          RAISE EXCEPTION 'expired correction pending state changed'
            USING ERRCODE = '40001';
        END IF;
        new_sequence := target_claim.projection_sequence + 1;
        new_outbox_id := pg_catalog.gen_random_uuid();
        new_state_hash := memory_private.claim_state_sha256(
          target_claim.owner_user_id, target_claim.claim_id,
          target_claim.semantic_key_sha256,
          target_claim.claim_identity_sha256, 'active', true,
          target_claim.current_revision_id,
          target_claim.current_revision_number,
          current_revision.revision_sha256, new_sequence
        );
        projection_hash := memory_private.projection_manifest_sha256(
          candidate.owner_user_id, target_claim.claim_id,
          current_revision.revision_id, expiry_operation_id, 'upsert',
          new_sequence, current_revision.revision_sha256,
          current_revision.selection_binding_sha256,
          current_revision.retrieval_text_sha256,
          current_revision.retrieval_text_sha256
        );
        UPDATE memory.claim
        SET lifecycle_state = 'active', current_state_sha256 = new_state_hash,
            correction_pending_at = NULL,
            projection_sequence = new_sequence,
            updated_at = captured_at
        WHERE owner_user_id = target_claim.owner_user_id
          AND claim_id = target_claim.claim_id;
        INSERT INTO memory.projection_outbox(
          outbox_id, owner_user_id, claim_id, revision_id, operation_id,
          sequence_number, operation, point_id, revision_sha256,
          selection_binding_sha256, projection_manifest_sha256
        ) VALUES (
          new_outbox_id, candidate.owner_user_id, target_claim.claim_id,
          current_revision.revision_id, expiry_operation_id, new_sequence,
          'upsert', target_claim.claim_id, current_revision.revision_sha256,
          current_revision.selection_binding_sha256, projection_hash
        );
        event_hash := pg_catalog.encode(pg_catalog.sha256(
          pg_catalog.convert_to(
            candidate.owner_user_id::text || '|' || expiry_operation_id::text
            || '|correction_expired_restored|'
            || target_claim.claim_id::text || '|'
            || current_revision.revision_sha256, 'UTF8'
          )
        ), 'hex');
        INSERT INTO memory.audit_event(
          owner_user_id, operation_id, actor_kind, object_type, object_id,
          transition_code, prior_state_sha256, new_state_sha256,
          reason_code, event_sha256
        ) VALUES (
          candidate.owner_user_id, expiry_operation_id, 'system', 'claim',
          target_claim.claim_id, 'correction_expired_restored',
          prior_state_hash, new_state_hash,
          'proposal_expired', event_hash
        );
        restored_total := restored_total + 1;
      END IF;
    END IF;

    UPDATE memory.proposal
    SET review_state = 'expired', reviewer_kind = 'system',
        reviewer_user_id = NULL,
        review_reason_codes = ARRAY['proposal_expired']::text[],
        reviewed_at = captured_at
    WHERE owner_user_id = candidate.owner_user_id
      AND proposal_id = candidate.proposal_id;
    event_hash := pg_catalog.encode(pg_catalog.sha256(
      pg_catalog.convert_to(
        candidate.owner_user_id::text || '|' || expiry_operation_id::text
        || '|proposal_expired|' || candidate.proposal_id::text || '|'
        || proposal_state_hash, 'UTF8'
      )
    ), 'hex');
    INSERT INTO memory.audit_event(
      owner_user_id, operation_id, actor_kind, object_type, object_id,
      transition_code, prior_state_sha256, new_state_sha256,
      reason_code, event_sha256
    ) VALUES (
      candidate.owner_user_id, expiry_operation_id, 'system', 'proposal',
      candidate.proposal_id, 'proposal_expired', candidate.proposal_sha256,
      proposal_state_hash, 'proposal_expired', event_hash
    );
    expired_total := expired_total + 1;
  END LOOP;

  RETURN QUERY SELECT expired_total, restored_total;
END;
$function$;

CREATE FUNCTION memory_private.purge_terminal_proposals(p_limit integer)
RETURNS TABLE(purged_count integer)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  candidate record;
  purge_operation_id uuid;
  receipt_hash text;
  purged_total integer := 0;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION 'invalid proposal purge limit' USING ERRCODE = '22023';
  END IF;
  FOR candidate IN
    SELECT proposal.owner_user_id, proposal.proposal_id,
           proposal.proposal_sha256, proposal.review_state,
           proposal.evidence_id, proposal.provider_call_id,
           provider_call.job_id
    FROM memory.proposal AS proposal
    LEFT JOIN memory.provider_call AS provider_call
      ON provider_call.owner_user_id = proposal.owner_user_id
     AND provider_call.provider_call_id = proposal.provider_call_id
    WHERE proposal.review_state IN ('rejected', 'expired')
      AND proposal.purge_after <= pg_catalog.transaction_timestamp()
    ORDER BY proposal.purge_after, proposal.proposal_id
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  LOOP
    purge_operation_id := pg_catalog.gen_random_uuid();
    receipt_hash := pg_catalog.encode(pg_catalog.sha256(
      pg_catalog.convert_to(
        candidate.owner_user_id::text || '|' || candidate.proposal_id::text
        || '|' || candidate.proposal_sha256 || '|'
        || candidate.review_state || '|retention_purged', 'UTF8'
      )
    ), 'hex');
    INSERT INTO memory.audit_event(
      owner_user_id, operation_id, actor_kind, object_type, object_id,
      transition_code, prior_state_sha256, new_state_sha256,
      reason_code, event_sha256
    ) VALUES (
      candidate.owner_user_id, purge_operation_id, 'system', 'proposal',
      candidate.proposal_id, 'proposal_retention_purged',
      candidate.proposal_sha256, receipt_hash,
      'retention_expired', receipt_hash
    );
    PERFORM pg_catalog.set_config(
      'app.memory_proposal_purge_operation_id',
      purge_operation_id::text, true
    );
    DELETE FROM memory.proposal AS proposal
    WHERE proposal.owner_user_id = candidate.owner_user_id
      AND proposal.proposal_id = candidate.proposal_id;
    DELETE FROM memory.provider_call AS provider_call
    WHERE provider_call.owner_user_id = candidate.owner_user_id
      AND (
        provider_call.provider_call_id = candidate.provider_call_id
        OR provider_call.job_id = candidate.job_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.proposal AS proposal
        WHERE proposal.owner_user_id = provider_call.owner_user_id
          AND proposal.provider_call_id = provider_call.provider_call_id
      );
    DELETE FROM memory.extraction_job AS job
    WHERE job.owner_user_id = candidate.owner_user_id
      AND job.job_id = candidate.job_id
      AND NOT EXISTS (
        SELECT 1 FROM memory.provider_call AS provider_call
        WHERE provider_call.owner_user_id = job.owner_user_id
          AND provider_call.job_id = job.job_id
      );
    DELETE FROM memory.evidence AS evidence
    WHERE evidence.owner_user_id = candidate.owner_user_id
      AND evidence.evidence_id = candidate.evidence_id
      AND NOT EXISTS (
        SELECT 1 FROM memory.proposal AS proposal
        WHERE proposal.owner_user_id = evidence.owner_user_id
          AND proposal.evidence_id = evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.claim_evidence AS claim_evidence
        WHERE claim_evidence.owner_user_id = evidence.owner_user_id
          AND claim_evidence.evidence_id = evidence.evidence_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.extraction_job AS job
        WHERE job.owner_user_id = evidence.owner_user_id
          AND job.evidence_id = evidence.evidence_id
      );
    purged_total := purged_total + 1;
  END LOOP;
  RETURN QUERY SELECT purged_total;
END;
$function$;

CREATE FUNCTION memory_private.purge_expired_answer_bindings(p_limit integer)
RETURNS TABLE(purged_count integer)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  candidate record;
  purge_operation_id uuid;
  receipt_sha256 text;
  purged_total integer := 0;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION 'invalid answer-binding purge limit'
      USING ERRCODE = '22023';
  END IF;
  FOR candidate IN
    SELECT binding.owner_user_id, binding.binding_id, binding.expires_at
    FROM memory.answer_binding AS binding
    WHERE binding.expires_at <= pg_catalog.transaction_timestamp()
    ORDER BY binding.expires_at, binding.binding_id
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  LOOP
    purge_operation_id := pg_catalog.gen_random_uuid();
    receipt_sha256 :=
      memory_private.answer_binding_retention_receipt_sha256(
        candidate.owner_user_id, purge_operation_id,
        candidate.binding_id, candidate.expires_at
      );
    INSERT INTO memory.audit_event(
      owner_user_id, operation_id, actor_kind, object_type, object_id,
      transition_code, prior_state_sha256, new_state_sha256,
      reason_code, event_sha256
    ) VALUES (
      candidate.owner_user_id, purge_operation_id, 'system',
      'answer_binding', candidate.binding_id,
      'answer_binding_retention_purged', NULL, receipt_sha256,
      'retention_expired', receipt_sha256
    );
    PERFORM pg_catalog.set_config(
      'app.memory_answer_binding_purge_operation_id',
      purge_operation_id::text, true
    );
    DELETE FROM memory.answer_binding AS binding
    WHERE binding.owner_user_id = candidate.owner_user_id
      AND binding.binding_id = candidate.binding_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'answer-binding retention purge lost its locked row'
        USING ERRCODE = '40001';
    END IF;
    PERFORM pg_catalog.set_config(
      'app.memory_answer_binding_purge_operation_id', '', true
    );
    purged_total := purged_total + 1;
  END LOOP;
  RETURN QUERY SELECT purged_total;
END;
$function$;

CREATE FUNCTION memory_private.redact_expired_evidence_excerpts(
  p_limit integer
)
RETURNS TABLE(redacted_count integer)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  candidate record;
  redaction_operation_id uuid;
  redacted_state_hash text;
  event_hash text;
  redacted_total integer := 0;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION 'invalid evidence redaction limit' USING ERRCODE = '22023';
  END IF;

  FOR candidate IN
    SELECT evidence.owner_user_id, evidence.evidence_id,
           evidence.selected_sha256, evidence.selection_binding_sha256
    FROM memory.evidence AS evidence
    WHERE evidence.review_excerpt IS NOT NULL
      AND (
        evidence.excerpt_expires_at <= pg_catalog.clock_timestamp()
        OR (
          NOT EXISTS (
            SELECT 1
            FROM memory.extraction_job AS job
            WHERE job.owner_user_id = evidence.owner_user_id
              AND job.evidence_id = evidence.evidence_id
              AND job.state IN ('pending', 'claimed', 'retryable')
          )
          AND NOT EXISTS (
            SELECT 1
            FROM memory.proposal AS proposal
            WHERE proposal.owner_user_id = evidence.owner_user_id
              AND proposal.evidence_id = evidence.evidence_id
              AND proposal.review_state = 'pending_review'
          )
        )
      )
    ORDER BY evidence.excerpt_expires_at, evidence.evidence_id
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  LOOP
    redaction_operation_id := pg_catalog.gen_random_uuid();
    UPDATE memory.provider_call AS provider
    SET state = 'retryable_failure',
        completed_at = pg_catalog.clock_timestamp(),
        error_code = 'evidence_excerpt_expired_before_dispatch'
    FROM memory.extraction_job AS job
    WHERE job.owner_user_id = candidate.owner_user_id
      AND job.evidence_id = candidate.evidence_id
      AND job.state = 'claimed'
      AND provider.owner_user_id = job.owner_user_id
      AND provider.job_id = job.job_id
      AND provider.attempt_number = job.attempt_count
      AND provider.state = 'reserved';
    UPDATE memory.extraction_job AS job
    SET state = 'failed_terminal',
        lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
        lease_expires_at = NULL,
        completed_at = pg_catalog.clock_timestamp(),
        last_error_code = 'evidence_excerpt_expired_before_dispatch',
        updated_at = pg_catalog.clock_timestamp()
    WHERE job.owner_user_id = candidate.owner_user_id
      AND job.evidence_id = candidate.evidence_id
      AND (
        job.state IN ('pending', 'retryable')
        OR (
          job.state = 'claimed'
          AND EXISTS (
            SELECT 1 FROM memory.provider_call AS provider
            WHERE provider.owner_user_id = job.owner_user_id
              AND provider.job_id = job.job_id
              AND provider.attempt_number = job.attempt_count
              AND provider.state = 'retryable_failure'
              AND provider.error_code
                    = 'evidence_excerpt_expired_before_dispatch'
          )
        )
      );
    redacted_state_hash := pg_catalog.encode(pg_catalog.sha256(
      pg_catalog.convert_to(
        candidate.owner_user_id::text || '|' || candidate.evidence_id::text
        || '|' || candidate.selected_sha256 || '|'
        || candidate.selection_binding_sha256 || '|excerpt_redacted',
        'UTF8'
      )
    ), 'hex');
    UPDATE memory.evidence
    SET review_excerpt = NULL, excerpt_expires_at = NULL
    WHERE owner_user_id = candidate.owner_user_id
      AND evidence_id = candidate.evidence_id;
    event_hash := pg_catalog.encode(pg_catalog.sha256(
      pg_catalog.convert_to(
        candidate.owner_user_id::text || '|' || redaction_operation_id::text
        || '|evidence_excerpt_redacted|' || candidate.evidence_id::text
        || '|' || redacted_state_hash, 'UTF8'
      )
    ), 'hex');
    INSERT INTO memory.audit_event(
      owner_user_id, operation_id, actor_kind, object_type, object_id,
      transition_code, prior_state_sha256, new_state_sha256,
      reason_code, event_sha256
    ) VALUES (
      candidate.owner_user_id, redaction_operation_id, 'system', 'evidence',
      candidate.evidence_id, 'evidence_excerpt_redacted',
      candidate.selected_sha256, redacted_state_hash,
      'retention_expired', event_hash
    );
    redacted_total := redacted_total + 1;
  END LOOP;

  RETURN QUERY SELECT redacted_total;
END;
$function$;

CREATE FUNCTION memory_private.correct_claim(
  p_operation_id uuid,
  p_claim_id uuid,
  p_expected_revision_sha256 text,
  p_expected_state_sha256 text,
  p_expected_predicate_catalog_sha256 text,
  p_replacement jsonb
)
RETURNS TABLE(
  outcome text,
  proposal_id uuid,
  proposal_sha256 text,
  review_operation_id uuid
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  target memory.claim%ROWTYPE;
  current_revision memory.claim_revision%ROWTYPE;
  current_subject memory.entity%ROWTYPE;
  existing_proposal memory.proposal%ROWTYPE;
  new_evidence_id uuid;
  new_proposal_id uuid;
  new_proposal_sha text;
  new_source_sha text;
  correction_material text;
  correction_source_message_id uuid;
  correction_source_thread_id uuid;
  correction_source_window_id uuid;
  correction_admission_operation_id uuid;
  correction_window_sha text;
  correction_end_utf8 integer;
  new_selection_binding_sha text;
  computed_semantic_sha text;
  target_identity_sha text;
  prior_state_hash text;
  pending_state_hash text;
  policy_sha text;
  new_sequence integer;
  delete_manifest text;
  event_hash text;
  transition_at timestamptz;
  derived_surface text;
  derived_requires_explicit boolean;
  replacement_object_entity_key text;
  replacement_retrieval_text text;
BEGIN
  IF session_user <> 'governed_memory_api' THEN
    RAISE EXCEPTION 'api role required' USING ERRCODE = '42501';
  END IF;
  actor := memory_private.current_owner_id();
  transition_at := pg_catalog.transaction_timestamp();
  IF actor IS NULL OR p_operation_id IS NULL OR p_claim_id IS NULL
     OR p_expected_revision_sha256 IS NULL
     OR p_expected_revision_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_state_sha256 IS NULL
     OR p_expected_state_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_predicate_catalog_sha256 IS NULL
     OR p_expected_predicate_catalog_sha256 !~ '^[0-9a-f]{64}$'
     OR p_replacement IS NULL OR pg_catalog.jsonb_typeof(p_replacement) <> 'object'
     OR pg_catalog.pg_column_size(p_replacement) > 65536 THEN
    RAISE EXCEPTION 'invalid correction input' USING ERRCODE = '22023';
  END IF;
  IF ARRAY(
       SELECT key
       FROM pg_catalog.jsonb_object_keys(p_replacement) AS object_key(key)
       ORDER BY key
     ) <> ARRAY[
       'epistemic_state', 'object_display_name', 'object_entity_type',
       'object_kind', 'object_literal', 'sensitivity'
     ]::text[] THEN
    RAISE EXCEPTION 'replacement keys differ from the closed correction schema'
      USING ERRCODE = '22023';
  END IF;
  IF pg_catalog.jsonb_typeof(p_replacement->'object_kind') <> 'string'
     OR pg_catalog.jsonb_typeof(p_replacement->'epistemic_state') <> 'string'
     OR pg_catalog.jsonb_typeof(p_replacement->'sensitivity') <> 'string'
     OR COALESCE(p_replacement->>'epistemic_state', '') NOT IN (
       'supported', 'uncertain', 'disputed'
     ) OR COALESCE(p_replacement->>'object_kind', '') NOT IN (
       'literal', 'entity'
     ) OR COALESCE(p_replacement->>'sensitivity', '') NOT IN (
       'ordinary', 'sensitive_self', 'sensitive_third_party'
     )
     OR (
       p_replacement->>'object_kind' = 'literal'
       AND (
         pg_catalog.jsonb_typeof(p_replacement->'object_literal') <> 'string'
         OR pg_catalog.octet_length(p_replacement->>'object_literal')
              NOT BETWEEN 1 AND 2000
         OR p_replacement->'object_entity_type' <> 'null'::jsonb
         OR p_replacement->'object_display_name' <> 'null'::jsonb
       )
     ) OR (
       p_replacement->>'object_kind' = 'entity'
       AND (
         p_replacement->'object_literal' <> 'null'::jsonb
         OR pg_catalog.jsonb_typeof(p_replacement->'object_entity_type') <> 'string'
         OR pg_catalog.jsonb_typeof(p_replacement->'object_display_name') <> 'string'
         OR pg_catalog.octet_length(p_replacement->>'object_display_name')
              NOT BETWEEN 1 AND 256
         OR COALESCE(p_replacement->>'object_entity_type', '') NOT IN (
           'self', 'person', 'pet', 'organization', 'place', 'other'
         )
       )
     ) THEN
    RAISE EXCEPTION 'replacement state is invalid' USING ERRCODE = '22023';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      actor::text || '|operation|' || p_operation_id::text, 0
    )
  );
  new_evidence_id := memory_private.derived_uuid(
    p_operation_id, 'correction-evidence'
  );
  new_proposal_id := memory_private.uuid5(
    new_evidence_id, 'correction-proposal'
  );
  correction_admission_operation_id := memory_private.uuid5(
    new_evidence_id, 'correction-admission'
  );
  SELECT proposal.* INTO existing_proposal
  FROM memory.proposal AS proposal
  JOIN memory.evidence AS evidence
    ON evidence.owner_user_id = proposal.owner_user_id
   AND evidence.evidence_id = proposal.evidence_id
  WHERE proposal.owner_user_id = actor
    AND proposal.proposal_id = new_proposal_id
    AND proposal.operation_id = correction_admission_operation_id
    AND evidence.evidence_id = new_evidence_id
    AND evidence.operation_id = p_operation_id;
  IF FOUND THEN
    replacement_object_entity_key := CASE
      WHEN p_replacement->>'object_kind' = 'literal' THEN NULL::text
      ELSE memory_private.source_local_entity_key(
        actor, existing_proposal.selection_binding_sha256,
        0, 'object', p_replacement->>'object_entity_type'
      )
    END;
    IF existing_proposal.proposal_purpose <> 'correction'
       OR existing_proposal.correction_of_claim_id <> p_claim_id
       OR existing_proposal.expected_revision_sha256
            <> p_expected_revision_sha256
       OR existing_proposal.correction_target_state_sha256
            <> p_expected_state_sha256
       OR existing_proposal.correction_pending_state_sha256 IS NULL
       OR existing_proposal.predicate_catalog_sha256
            <> p_expected_predicate_catalog_sha256
       OR existing_proposal.object_kind <> p_replacement->>'object_kind'
       OR existing_proposal.object_entity_key
            IS DISTINCT FROM replacement_object_entity_key
       OR existing_proposal.object_entity_type IS DISTINCT FROM
            NULLIF(p_replacement->>'object_entity_type', '')
       OR existing_proposal.object_display_name IS DISTINCT FROM
            NULLIF(p_replacement->>'object_display_name', '')
       OR existing_proposal.object_literal IS DISTINCT FROM (
            CASE WHEN p_replacement->>'object_kind' = 'literal'
              THEN p_replacement->'object_literal' ELSE NULL::jsonb END
          )
       OR existing_proposal.epistemic_state
            <> p_replacement->>'epistemic_state'
       OR existing_proposal.sensitivity <> p_replacement->>'sensitivity' THEN
      RAISE EXCEPTION 'correction operation replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT 'replayed'::text, existing_proposal.proposal_id,
      existing_proposal.proposal_sha256, existing_proposal.operation_id;
    RETURN;
  END IF;
  IF memory_private.operation_id_conflicts(
       actor, p_operation_id, NULL::uuid, NULL::uuid, NULL::uuid,
       ARRAY[]::text[]
     ) THEN
    RAISE EXCEPTION 'operation id is reserved by another mutation'
      USING ERRCODE = '23514';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(actor::text || '|claim|' || p_claim_id::text, 0)
  );
  SELECT value.* INTO STRICT target
  FROM memory.claim AS value
  WHERE value.owner_user_id = actor AND value.claim_id = p_claim_id
  FOR UPDATE;
  SELECT value.* INTO STRICT current_revision
  FROM memory.claim_revision AS value
  WHERE value.owner_user_id = actor
    AND value.claim_id = target.claim_id
    AND value.revision_id = target.current_revision_id;
  SELECT value.* INTO STRICT current_subject
  FROM memory.entity AS value
  WHERE value.owner_user_id = actor
    AND value.entity_id = current_revision.subject_entity_id;
  target_identity_sha := memory_private.claim_identity_sha256(
    current_subject.entity_key, current_revision.predicate
  );
  derived_surface := CASE WHEN p_replacement->>'sensitivity' = 'ordinary'
    THEN 'normal' ELSE 'explicit_only' END;
  derived_requires_explicit := p_replacement->>'sensitivity' <> 'ordinary';
  correction_material := memory_private.correction_source_material(
    current_subject.entity_key, current_revision.predicate,
    p_replacement->>'object_kind',
    NULLIF(p_replacement->>'object_entity_type', ''),
    NULLIF(p_replacement->>'object_display_name', ''),
    CASE WHEN p_replacement->>'object_kind' = 'literal'
      THEN p_replacement->'object_literal' ELSE NULL::jsonb END,
    p_replacement->>'epistemic_state', p_replacement->>'sensitivity'
  );
  new_source_sha := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(correction_material, 'UTF8')
  ), 'hex');
  new_evidence_id := memory_private.derived_uuid(
    p_operation_id, 'correction-evidence'
  );
  new_proposal_id := memory_private.uuid5(
    new_evidence_id, 'correction-proposal'
  );
  correction_admission_operation_id := memory_private.uuid5(
    new_evidence_id, 'correction-admission'
  );
  correction_source_message_id := memory_private.uuid5(
    new_evidence_id, 'source-message'
  );
  correction_source_thread_id := memory_private.uuid5(
    new_evidence_id, 'source-thread'
  );
  correction_source_window_id := memory_private.uuid5(
    new_evidence_id, 'source-window'
  );
  correction_window_sha := memory_private.correction_window_sha256(
    actor, target.claim_id, current_revision.revision_id,
    p_operation_id, new_evidence_id, correction_source_message_id,
    correction_source_thread_id, correction_source_window_id, new_source_sha
  );
  correction_end_utf8 := pg_catalog.octet_length(
    pg_catalog.convert_to(correction_material, 'UTF8')
  );
  new_selection_binding_sha := memory_private.selection_binding_sha256(
    actor, 'owner_correction', correction_source_message_id,
    correction_source_thread_id, correction_source_window_id,
    correction_window_sha, new_source_sha, new_source_sha,
    0, correction_end_utf8, NULL::uuid, NULL::text
  );
  replacement_object_entity_key := CASE
    WHEN p_replacement->>'object_kind' = 'literal' THEN NULL::text
    ELSE memory_private.source_local_entity_key(
      actor, new_selection_binding_sha, 0, 'object',
      p_replacement->>'object_entity_type'
    )
  END;
  replacement_retrieval_text := memory_private.render_relational_fact(
    current_subject.entity_type, current_subject.entity_key,
    CASE WHEN current_subject.entity_type = 'self'
      THEN NULL::text ELSE current_subject.display_name END,
    current_revision.predicate, p_replacement->>'object_kind',
    NULLIF(p_replacement->>'object_entity_type', ''),
    replacement_object_entity_key,
    NULLIF(p_replacement->>'object_display_name', ''),
    CASE WHEN p_replacement->>'object_kind' = 'literal'
      THEN p_replacement->'object_literal' ELSE NULL::jsonb END
  );
  IF pg_catalog.octet_length(replacement_retrieval_text)
       NOT BETWEEN 1 AND 4096 THEN
    RAISE EXCEPTION 'corrected retrieval rendering exceeds bound'
      USING ERRCODE = '22001';
  END IF;
  computed_semantic_sha := memory_private.semantic_key_sha256(
    current_subject.entity_key, current_revision.predicate,
    p_replacement->>'object_kind', replacement_object_entity_key,
    CASE WHEN p_replacement->>'object_kind' = 'literal'
      THEN p_replacement->'object_literal' ELSE NULL::jsonb END
  );

  prior_state_hash := memory_private.claim_state_sha256(
    target.owner_user_id, target.claim_id, target.semantic_key_sha256,
    target.claim_identity_sha256, target.lifecycle_state, true,
    target.current_revision_id,
    target.current_revision_number, current_revision.revision_sha256,
    target.projection_sequence
  );
  IF target.lifecycle_state <> 'active'
     OR current_revision.revision_sha256 <> p_expected_revision_sha256
     OR current_revision.predicate_catalog_sha256
          <> p_expected_predicate_catalog_sha256
     OR target.claim_identity_sha256 <> target_identity_sha
     OR prior_state_hash <> target.current_state_sha256
     OR target.current_state_sha256 <> p_expected_state_sha256
     OR NOT EXISTS (
       SELECT 1
       FROM memory.predicate_catalog AS predicate
       WHERE predicate.predicate = current_revision.predicate
         AND predicate.active
         AND predicate.catalog_sha256 = p_expected_predicate_catalog_sha256
         AND current_subject.entity_type = ANY(predicate.subject_kinds)
         AND p_replacement->>'object_kind' = ANY(predicate.object_kinds)
         AND p_replacement->>'sensitivity' = ANY(predicate.sensitivities)
         AND p_replacement->>'epistemic_state'
               = ANY(predicate.epistemic_statuses)
     ) THEN
    RAISE EXCEPTION 'claim state or correction catalog changed'
      USING ERRCODE = '40001';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.claim AS collision
    WHERE collision.owner_user_id = actor
      AND collision.semantic_key_sha256 = computed_semantic_sha
      AND collision.claim_id <> target.claim_id
  ) THEN
    RAISE EXCEPTION 'corrected semantic identity collides with another claim'
      USING ERRCODE = '23505';
  END IF;

  new_sequence := target.projection_sequence + 1;
  pending_state_hash := memory_private.claim_state_sha256(
    target.owner_user_id, target.claim_id, target.semantic_key_sha256,
    target.claim_identity_sha256, 'correction_pending', true,
    target.current_revision_id, target.current_revision_number,
    current_revision.revision_sha256, new_sequence
  );

  policy_sha := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to('owner-correction-policy-v1', 'UTF8')
  ), 'hex');
  new_proposal_sha := memory_private.proposal_sha256(
    actor, NULL::uuid, new_evidence_id, NULL::uuid, 'owner_correction',
    correction_source_message_id, correction_source_thread_id,
    correction_source_window_id, correction_window_sha, new_source_sha,
    new_source_sha, new_selection_binding_sha, NULL::uuid, NULL::text,
    p_expected_predicate_catalog_sha256, NULL::text, NULL::text,
    'correction', target.claim_id, current_revision.revision_id,
    current_revision.revision_number, current_revision.revision_sha256,
    prior_state_hash, target_identity_sha, target.projection_sequence,
    pending_state_hash,
    NULL::smallint,
    new_proposal_id, correction_admission_operation_id,
    current_subject.entity_type,
    current_subject.entity_key,
    CASE WHEN current_subject.entity_type = 'self'
      THEN NULL::text ELSE current_subject.display_name END,
    current_revision.predicate, p_replacement->>'object_kind',
    NULLIF(p_replacement->>'object_entity_type', ''),
    replacement_object_entity_key,
    NULLIF(p_replacement->>'object_display_name', ''),
    CASE WHEN p_replacement->>'object_kind' = 'literal'
      THEN p_replacement->'object_literal' ELSE NULL::jsonb END,
    p_replacement->>'epistemic_state', p_replacement->>'sensitivity',
    computed_semantic_sha, true, ARRAY[]::text[], ARRAY[]::text[],
    derived_surface, derived_requires_explicit,
    NULL::timestamptz, NULL::timestamptz
  );
  INSERT INTO memory.evidence(
    evidence_id, owner_user_id, operation_id, source_kind,
    source_message_id, source_thread_id, source_window_id,
    source_window_sha256,
    source_sha256, selected_sha256, selection_binding_sha256,
    selected_start_utf8, selected_end_utf8,
    source_created_at, eligibility_decision, eligibility_policy_sha256
  ) VALUES (
    new_evidence_id, actor, p_operation_id, 'owner_correction',
    correction_source_message_id, correction_source_thread_id,
    correction_source_window_id, correction_window_sha,
    new_source_sha, new_source_sha, new_selection_binding_sha,
    0, correction_end_utf8,
    transition_at, 'route_internal', policy_sha
  );

  INSERT INTO memory.proposal(
    proposal_id, owner_user_id, evidence_id, operation_id,
    proposal_purpose, fact_index, proposal_sha256, semantic_key_sha256,
    selected_sha256,
    selection_binding_sha256, predicate_catalog_sha256, subject_entity_key,
    subject_entity_type, subject_display_name, predicate, object_kind,
    object_entity_key, object_entity_type, object_display_name,
    object_literal, epistemic_state, projectable, sensitivity, domains,
    intents, surface, requires_explicit, valid_from, valid_to,
    correction_of_claim_id, correction_target_revision_id,
    correction_target_revision_number, expected_revision_sha256,
    correction_target_state_sha256, correction_target_identity_sha256,
    correction_target_projection_sequence,
    correction_pending_state_sha256,
    expires_at, purge_after
  ) VALUES (
    new_proposal_id, actor, new_evidence_id,
    correction_admission_operation_id,
    'correction', NULL, new_proposal_sha, computed_semantic_sha, new_source_sha,
    new_selection_binding_sha, p_expected_predicate_catalog_sha256,
    current_subject.entity_key, current_subject.entity_type,
    CASE WHEN current_subject.entity_type = 'self'
      THEN NULL::text ELSE current_subject.display_name END,
    current_revision.predicate,
    p_replacement->>'object_kind',
    replacement_object_entity_key,
    NULLIF(p_replacement->>'object_entity_type', ''),
    NULLIF(p_replacement->>'object_display_name', ''),
    CASE WHEN p_replacement->>'object_kind' = 'literal'
      THEN p_replacement->'object_literal' ELSE NULL::jsonb END,
    p_replacement->>'epistemic_state',
    true, p_replacement->>'sensitivity',
    ARRAY[]::text[], ARRAY[]::text[], derived_surface,
    derived_requires_explicit, NULL, NULL,
    target.claim_id, current_revision.revision_id,
    current_revision.revision_number, current_revision.revision_sha256,
    prior_state_hash, target_identity_sha, target.projection_sequence,
    pending_state_hash,
    transition_at + interval '7 days',
    transition_at + interval '30 days'
  );

  delete_manifest := memory_private.projection_manifest_sha256(
    actor, target.claim_id, current_revision.revision_id,
    p_operation_id, 'delete', new_sequence,
    current_revision.revision_sha256,
    current_revision.selection_binding_sha256,
    current_revision.retrieval_text_sha256,
    current_revision.retrieval_text_sha256
  );
  UPDATE memory.claim
  SET lifecycle_state = 'correction_pending',
      current_state_sha256 = pending_state_hash,
      correction_pending_at = transition_at,
      projection_sequence = new_sequence,
      updated_at = transition_at
  WHERE owner_user_id = actor AND claim_id = target.claim_id;
  INSERT INTO memory.projection_outbox(
    owner_user_id, claim_id, revision_id, operation_id, sequence_number,
    operation, point_id, revision_sha256, selection_binding_sha256,
    projection_manifest_sha256
  ) VALUES (
    actor, target.claim_id, current_revision.revision_id,
    p_operation_id, new_sequence, 'delete', target.claim_id,
    current_revision.revision_sha256,
    current_revision.selection_binding_sha256, delete_manifest
  );
  event_hash := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(
      actor::text || '|' || p_operation_id::text || '|correction_pending|'
      || new_proposal_sha || '|' || new_selection_binding_sha || '|'
      || p_expected_predicate_catalog_sha256, 'UTF8'
    )
  ), 'hex');
  INSERT INTO memory.audit_event(
    owner_user_id, operation_id, actor_kind, actor_user_id,
    object_type, object_id, transition_code, prior_state_sha256,
    new_state_sha256, reason_code, event_sha256
  ) VALUES (
    actor, p_operation_id, 'owner', actor, 'claim', target.claim_id,
    'correction_requested', prior_state_hash,
    pending_state_hash, 'explicit_owner_correction', event_hash
  );
  RETURN QUERY SELECT 'correction_pending'::text, new_proposal_id,
    new_proposal_sha, correction_admission_operation_id;
END;
$function$;

CREATE FUNCTION memory_private.reject_pending_correction_for_lifecycle(
  p_owner_user_id uuid,
  p_claim_id uuid,
  p_operation_id uuid
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  target memory.claim%ROWTYPE;
  revision memory.claim_revision%ROWTYPE;
  pending memory.proposal%ROWTYPE;
  pending_proposal_id uuid;
  receipt_hash text;
BEGIN
  IF session_user <> 'governed_memory_api' THEN
    RAISE EXCEPTION 'api role required' USING ERRCODE = '42501';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      p_owner_user_id::text || '|claim|' || p_claim_id::text, 0
    )
  );
  SELECT value.* INTO STRICT target
  FROM memory.claim AS value
  WHERE value.owner_user_id = p_owner_user_id
    AND value.claim_id = p_claim_id;
  IF target.lifecycle_state <> 'correction_pending' THEN
    RAISE EXCEPTION 'claim has no pending correction' USING ERRCODE = '40001';
  END IF;
  SELECT value.* INTO STRICT revision
  FROM memory.claim_revision AS value
  WHERE value.owner_user_id = target.owner_user_id
    AND value.claim_id = target.claim_id
    AND value.revision_id = target.current_revision_id;
  SELECT value.proposal_id INTO STRICT pending_proposal_id
  FROM memory.proposal AS value
  WHERE value.owner_user_id = target.owner_user_id
    AND value.correction_of_claim_id = target.claim_id
    AND value.review_state = 'pending_review'
    AND value.correction_target_revision_id = revision.revision_id
    AND value.correction_target_revision_number = revision.revision_number
    AND value.expected_revision_sha256 = revision.revision_sha256
    AND value.correction_target_identity_sha256 = target.claim_identity_sha256
    AND value.correction_target_projection_sequence + 1
          = target.projection_sequence
    AND value.correction_pending_state_sha256 = target.current_state_sha256;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      p_owner_user_id::text || '|proposal|' || pending_proposal_id::text, 0
    )
  );
  SELECT value.* INTO STRICT target
  FROM memory.claim AS value
  WHERE value.owner_user_id = p_owner_user_id
    AND value.claim_id = p_claim_id
  FOR UPDATE;
  IF target.lifecycle_state <> 'correction_pending' THEN
    RAISE EXCEPTION 'claim has no pending correction' USING ERRCODE = '40001';
  END IF;
  SELECT value.* INTO STRICT revision
  FROM memory.claim_revision AS value
  WHERE value.owner_user_id = target.owner_user_id
    AND value.claim_id = target.claim_id
    AND value.revision_id = target.current_revision_id;
  SELECT value.* INTO STRICT pending
  FROM memory.proposal AS value
  WHERE value.owner_user_id = target.owner_user_id
    AND value.proposal_id = pending_proposal_id
    AND value.correction_of_claim_id = target.claim_id
    AND value.review_state = 'pending_review'
    AND value.correction_target_revision_id = revision.revision_id
    AND value.correction_target_revision_number = revision.revision_number
    AND value.expected_revision_sha256 = revision.revision_sha256
    AND value.correction_target_identity_sha256 = target.claim_identity_sha256
    AND value.correction_target_projection_sequence + 1
          = target.projection_sequence
    AND value.correction_pending_state_sha256 = target.current_state_sha256
  FOR UPDATE;
  IF NOT EXISTS (
    SELECT 1 FROM memory.audit_event AS transition
    WHERE transition.owner_user_id = target.owner_user_id
      AND transition.operation_id = (
        SELECT evidence.operation_id
        FROM memory.evidence AS evidence
        WHERE evidence.owner_user_id = pending.owner_user_id
          AND evidence.evidence_id = pending.evidence_id
      )
      AND transition.object_type = 'claim'
      AND transition.object_id = target.claim_id
      AND transition.transition_code = 'correction_requested'
      AND transition.prior_state_sha256
            = pending.correction_target_state_sha256
      AND transition.new_state_sha256 = pending.correction_pending_state_sha256
  ) THEN
    RAISE EXCEPTION 'pending correction lineage is incomplete'
      USING ERRCODE = '40001';
  END IF;
  UPDATE memory.proposal
  SET review_state = 'rejected', reviewer_kind = 'system',
      reviewer_user_id = NULL,
      review_reason_codes = ARRAY['lifecycle_override']::text[],
      reviewed_at = pg_catalog.clock_timestamp()
  WHERE owner_user_id = target.owner_user_id
    AND proposal_id = pending.proposal_id;
  receipt_hash := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(
      target.owner_user_id::text || '|' || p_operation_id::text || '|'
      || pending.proposal_id::text || '|lifecycle_override|'
      || pending.proposal_sha256, 'UTF8'
    )
  ), 'hex');
  INSERT INTO memory.audit_event(
    owner_user_id, operation_id, actor_kind, object_type, object_id,
    transition_code, prior_state_sha256, new_state_sha256,
    reason_code, event_sha256
  ) VALUES (
    target.owner_user_id, p_operation_id, 'system', 'proposal',
    pending.proposal_id, 'correction_rejected_lifecycle_override',
    pending.proposal_sha256, receipt_hash, 'lifecycle_override', receipt_hash
  );
END;
$function$;
REVOKE ALL ON FUNCTION
  memory_private.reject_pending_correction_for_lifecycle(uuid,uuid,uuid)
  FROM PUBLIC;

CREATE FUNCTION memory_private.retract_claim(
  p_operation_id uuid,
  p_claim_id uuid,
  p_expected_revision_sha256 text,
  p_expected_state_sha256 text
)
RETURNS TABLE(outcome text, outbox_id uuid)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  target memory.claim%ROWTYPE;
  revision memory.claim_revision%ROWTYPE;
  new_outbox_id uuid;
  new_sequence integer;
  manifest_hash text;
  event_hash text;
  prior_state_hash text;
  new_state_hash text;
  existing_outbox memory.projection_outbox%ROWTYPE;
  existing_transition memory.audit_event%ROWTYPE;
BEGIN
  IF session_user <> 'governed_memory_api' THEN
    RAISE EXCEPTION 'api role required' USING ERRCODE = '42501';
  END IF;
  actor := memory_private.current_owner_id();
  IF actor IS NULL OR p_operation_id IS NULL OR p_claim_id IS NULL
     OR p_expected_revision_sha256 IS NULL
     OR p_expected_revision_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_state_sha256 IS NULL
     OR p_expected_state_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid retraction input' USING ERRCODE = '22023';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      actor::text || '|operation|' || p_operation_id::text, 0
    )
  );
  SELECT value.* INTO existing_outbox
  FROM memory.projection_outbox AS value
  WHERE value.owner_user_id = actor
    AND value.operation_id = p_operation_id;
  IF FOUND THEN
    SELECT value.* INTO existing_transition
    FROM memory.audit_event AS value
    WHERE value.owner_user_id = actor
      AND value.operation_id = p_operation_id
      AND value.transition_code = 'claim_retracted';
    IF NOT FOUND THEN
      RAISE EXCEPTION 'retraction operation replay drifted'
        USING ERRCODE = '23514';
    END IF;
    event_hash := pg_catalog.encode(pg_catalog.sha256(
      pg_catalog.convert_to(
        actor::text || '|' || p_operation_id::text || '|retracted|'
        || p_expected_revision_sha256, 'UTF8'
      )
    ), 'hex');
    IF existing_outbox.claim_id IS DISTINCT FROM p_claim_id
       OR existing_outbox.operation IS DISTINCT FROM 'delete'
       OR existing_outbox.point_id IS DISTINCT FROM p_claim_id
       OR existing_outbox.revision_sha256
            IS DISTINCT FROM p_expected_revision_sha256
       OR existing_transition.actor_kind IS DISTINCT FROM 'owner'
       OR existing_transition.actor_user_id IS DISTINCT FROM actor
       OR existing_transition.object_type IS DISTINCT FROM 'claim'
       OR existing_transition.object_id IS DISTINCT FROM p_claim_id
       OR existing_transition.prior_state_sha256
            IS DISTINCT FROM p_expected_state_sha256
       OR existing_transition.reason_code
            IS DISTINCT FROM 'explicit_owner_retraction'
       OR existing_transition.event_sha256 IS DISTINCT FROM event_hash THEN
      RAISE EXCEPTION 'retraction operation replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT 'replayed'::text, existing_outbox.outbox_id;
    RETURN;
  END IF;
  IF memory_private.operation_id_conflicts(
       actor, p_operation_id, NULL::uuid, NULL::uuid, NULL::uuid,
       ARRAY[]::text[]
     ) THEN
    RAISE EXCEPTION 'operation id is reserved by another mutation'
      USING ERRCODE = '23514';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(actor::text || '|claim|' || p_claim_id::text, 0)
  );
  SELECT value.* INTO STRICT target
  FROM memory.claim AS value
  WHERE value.owner_user_id = actor AND value.claim_id = p_claim_id
  FOR UPDATE;
  SELECT value.* INTO STRICT revision
  FROM memory.claim_revision AS value
  WHERE value.owner_user_id = actor
    AND value.claim_id = target.claim_id
    AND value.revision_id = target.current_revision_id;
  prior_state_hash := memory_private.claim_state_sha256(
    target.owner_user_id, target.claim_id, target.semantic_key_sha256,
    target.claim_identity_sha256, target.lifecycle_state, true,
    target.current_revision_id, target.current_revision_number,
    revision.revision_sha256, target.projection_sequence
  );
  IF revision.revision_sha256 <> p_expected_revision_sha256
     OR target.current_state_sha256 <> p_expected_state_sha256
     OR target.current_state_sha256 <> prior_state_hash
     OR target.lifecycle_state NOT IN ('active', 'correction_pending') THEN
    RAISE EXCEPTION 'claim state changed' USING ERRCODE = '40001';
  END IF;
  IF target.lifecycle_state = 'correction_pending' THEN
    PERFORM memory_private.reject_pending_correction_for_lifecycle(
      actor, target.claim_id, p_operation_id
    );
  END IF;
  new_sequence := target.projection_sequence + 1;
  new_outbox_id := pg_catalog.gen_random_uuid();
  new_state_hash := memory_private.claim_state_sha256(
    target.owner_user_id, target.claim_id, target.semantic_key_sha256,
    target.claim_identity_sha256, 'retracted', true,
    target.current_revision_id, target.current_revision_number,
    revision.revision_sha256, new_sequence
  );
  manifest_hash := memory_private.projection_manifest_sha256(
    actor, target.claim_id, revision.revision_id, p_operation_id,
    'delete', new_sequence, revision.revision_sha256,
    revision.selection_binding_sha256, revision.retrieval_text_sha256,
    revision.retrieval_text_sha256
  );
  UPDATE memory.claim
  SET lifecycle_state = 'retracted', current_state_sha256 = new_state_hash,
      correction_pending_at = NULL,
      retracted_at = pg_catalog.clock_timestamp(),
      projection_sequence = new_sequence,
      updated_at = pg_catalog.clock_timestamp()
  WHERE owner_user_id = actor AND claim_id = target.claim_id;
  INSERT INTO memory.projection_outbox(
    outbox_id, owner_user_id, claim_id, revision_id, operation_id,
    sequence_number, operation, point_id, revision_sha256,
    selection_binding_sha256, projection_manifest_sha256
  ) VALUES (
    new_outbox_id, actor, target.claim_id, revision.revision_id,
    p_operation_id, new_sequence, 'delete', target.claim_id,
    revision.revision_sha256, revision.selection_binding_sha256, manifest_hash
  );
  event_hash := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(
      actor::text || '|' || p_operation_id::text || '|retracted|'
      || revision.revision_sha256, 'UTF8'
    )
  ), 'hex');
  INSERT INTO memory.audit_event(
    owner_user_id, operation_id, actor_kind, actor_user_id,
    object_type, object_id, transition_code, prior_state_sha256,
    new_state_sha256, reason_code, event_sha256
  ) VALUES (
    actor, p_operation_id, 'owner', actor, 'claim', target.claim_id,
    'claim_retracted', prior_state_hash, new_state_hash,
    'explicit_owner_retraction', event_hash
  );
  RETURN QUERY SELECT 'retracted'::text, new_outbox_id;
END;
$function$;

CREATE FUNCTION memory_private.request_claim_deletion(
  p_operation_id uuid,
  p_claim_id uuid,
  p_expected_revision_sha256 text,
  p_expected_state_sha256 text
)
RETURNS TABLE(outcome text, outbox_id uuid)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  target memory.claim%ROWTYPE;
  revision memory.claim_revision%ROWTYPE;
  new_outbox_id uuid;
  new_sequence integer;
  manifest_hash text;
  event_hash text;
  prior_state_hash text;
  new_state_hash text;
  existing_outbox memory.projection_outbox%ROWTYPE;
  existing_transition memory.audit_event%ROWTYPE;
  existing_deletion_event memory.audit_event%ROWTYPE;
  existing_deletion_receipt memory.claim_deletion_receipt%ROWTYPE;
BEGIN
  IF session_user <> 'governed_memory_api' THEN
    RAISE EXCEPTION 'api role required' USING ERRCODE = '42501';
  END IF;
  actor := memory_private.current_owner_id();
  IF actor IS NULL OR p_operation_id IS NULL OR p_claim_id IS NULL
     OR p_expected_revision_sha256 IS NULL
     OR p_expected_revision_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_state_sha256 IS NULL
     OR p_expected_state_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid deletion input' USING ERRCODE = '22023';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      actor::text || '|operation|' || p_operation_id::text, 0
    )
  );
  SELECT value.* INTO existing_deletion_receipt
  FROM memory.claim_deletion_receipt AS value
  WHERE value.owner_user_id = actor
    AND value.operation_id = p_operation_id;
  IF FOUND THEN
    SELECT value.* INTO existing_transition
    FROM memory.audit_event AS value
    WHERE value.owner_user_id = actor
      AND value.operation_id = p_operation_id
      AND value.transition_code = 'claim_deletion_requested';
    IF NOT FOUND THEN
      RAISE EXCEPTION 'claim-deletion request replay drifted'
        USING ERRCODE = '23514';
    END IF;
    SELECT value.* INTO existing_deletion_event
    FROM memory.audit_event AS value
    WHERE value.owner_user_id = actor
      AND value.event_id = existing_deletion_receipt.audit_event_id
      AND value.operation_id = p_operation_id
      AND value.transition_code = 'claim_deleted';
    IF NOT FOUND THEN
      RAISE EXCEPTION 'claim-deletion receipt replay drifted'
        USING ERRCODE = '23514';
    END IF;
    event_hash := pg_catalog.encode(pg_catalog.sha256(
      pg_catalog.convert_to(
        actor::text || '|' || p_operation_id::text || '|deletion_pending|'
        || p_expected_revision_sha256, 'UTF8'
      )
    ), 'hex');
    IF existing_deletion_receipt.claim_id IS DISTINCT FROM p_claim_id
       OR existing_deletion_receipt.point_id IS DISTINCT FROM p_claim_id
       OR existing_deletion_receipt.revision_sha256
            IS DISTINCT FROM p_expected_revision_sha256
       OR existing_transition.actor_kind IS DISTINCT FROM 'owner'
       OR existing_transition.actor_user_id IS DISTINCT FROM actor
       OR existing_transition.object_type IS DISTINCT FROM 'claim'
       OR existing_transition.object_id IS DISTINCT FROM p_claim_id
       OR existing_transition.prior_state_sha256
            IS DISTINCT FROM p_expected_state_sha256
       OR existing_transition.new_state_sha256
            IS DISTINCT FROM existing_deletion_receipt.prior_state_sha256
       OR existing_transition.reason_code
            IS DISTINCT FROM 'explicit_owner_deletion'
       OR existing_transition.event_sha256 IS DISTINCT FROM event_hash
       OR existing_deletion_event.actor_kind IS DISTINCT FROM 'worker'
       OR existing_deletion_event.actor_user_id IS NOT NULL
       OR existing_deletion_event.object_type IS DISTINCT FROM 'claim'
       OR existing_deletion_event.object_id IS DISTINCT FROM p_claim_id
       OR existing_deletion_event.prior_state_sha256
            IS DISTINCT FROM existing_deletion_receipt.prior_state_sha256
       OR existing_deletion_event.new_state_sha256
            IS DISTINCT FROM existing_deletion_receipt.receipt_sha256
       OR existing_deletion_event.reason_code
            IS DISTINCT FROM 'qdrant_delete_verified'
       OR existing_deletion_event.event_sha256
            IS DISTINCT FROM existing_deletion_receipt.receipt_sha256 THEN
      RAISE EXCEPTION 'claim-deletion request replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY
    SELECT 'replayed'::text, existing_deletion_receipt.delete_outbox_id;
    RETURN;
  END IF;
  SELECT value.* INTO existing_outbox
  FROM memory.projection_outbox AS value
  WHERE value.owner_user_id = actor
    AND value.operation_id = p_operation_id;
  IF FOUND THEN
    SELECT value.* INTO existing_transition
    FROM memory.audit_event AS value
    WHERE value.owner_user_id = actor
      AND value.operation_id = p_operation_id
      AND value.transition_code = 'claim_deletion_requested';
    IF NOT FOUND THEN
      RAISE EXCEPTION 'claim-deletion request replay drifted'
        USING ERRCODE = '23514';
    END IF;
    event_hash := pg_catalog.encode(pg_catalog.sha256(
      pg_catalog.convert_to(
        actor::text || '|' || p_operation_id::text || '|deletion_pending|'
        || p_expected_revision_sha256, 'UTF8'
      )
    ), 'hex');
    IF existing_outbox.claim_id IS DISTINCT FROM p_claim_id
       OR existing_outbox.operation IS DISTINCT FROM 'delete'
       OR existing_outbox.point_id IS DISTINCT FROM p_claim_id
       OR existing_outbox.revision_sha256
            IS DISTINCT FROM p_expected_revision_sha256
       OR existing_transition.actor_kind IS DISTINCT FROM 'owner'
       OR existing_transition.actor_user_id IS DISTINCT FROM actor
       OR existing_transition.object_type IS DISTINCT FROM 'claim'
       OR existing_transition.object_id IS DISTINCT FROM p_claim_id
       OR existing_transition.prior_state_sha256
            IS DISTINCT FROM p_expected_state_sha256
       OR existing_transition.reason_code
            IS DISTINCT FROM 'explicit_owner_deletion'
       OR existing_transition.event_sha256 IS DISTINCT FROM event_hash THEN
      RAISE EXCEPTION 'claim-deletion request replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT 'replayed'::text, existing_outbox.outbox_id;
    RETURN;
  END IF;
  IF memory_private.operation_id_conflicts(
       actor, p_operation_id, NULL::uuid, NULL::uuid, NULL::uuid,
       ARRAY[]::text[]
     ) THEN
    RAISE EXCEPTION 'operation id is reserved by another mutation'
      USING ERRCODE = '23514';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(actor::text || '|claim|' || p_claim_id::text, 0)
  );
  SELECT value.* INTO STRICT target
  FROM memory.claim AS value
  WHERE value.owner_user_id = actor AND value.claim_id = p_claim_id
  FOR UPDATE;
  SELECT value.* INTO STRICT revision
  FROM memory.claim_revision AS value
  WHERE value.owner_user_id = actor
    AND value.claim_id = target.claim_id
    AND value.revision_id = target.current_revision_id;
  prior_state_hash := memory_private.claim_state_sha256(
    target.owner_user_id, target.claim_id, target.semantic_key_sha256,
    target.claim_identity_sha256, target.lifecycle_state, true,
    target.current_revision_id, target.current_revision_number,
    revision.revision_sha256, target.projection_sequence
  );
  IF revision.revision_sha256 <> p_expected_revision_sha256
     OR target.current_state_sha256 <> p_expected_state_sha256
     OR target.current_state_sha256 <> prior_state_hash
     OR target.lifecycle_state NOT IN (
       'active', 'correction_pending', 'retracted'
     ) THEN
    RAISE EXCEPTION 'claim state changed' USING ERRCODE = '40001';
  END IF;
  IF target.lifecycle_state = 'correction_pending' THEN
    PERFORM memory_private.reject_pending_correction_for_lifecycle(
      actor, target.claim_id, p_operation_id
    );
  END IF;
  new_sequence := target.projection_sequence + 1;
  new_outbox_id := pg_catalog.gen_random_uuid();
  new_state_hash := memory_private.claim_state_sha256(
    target.owner_user_id, target.claim_id, target.semantic_key_sha256,
    target.claim_identity_sha256, 'deletion_pending', true,
    target.current_revision_id, target.current_revision_number,
    revision.revision_sha256, new_sequence
  );
  manifest_hash := memory_private.projection_manifest_sha256(
    actor, target.claim_id, revision.revision_id, p_operation_id,
    'delete', new_sequence, revision.revision_sha256,
    revision.selection_binding_sha256, revision.retrieval_text_sha256,
    revision.retrieval_text_sha256
  );
  UPDATE memory.claim
  SET lifecycle_state = 'deletion_pending',
      current_state_sha256 = new_state_hash,
      correction_pending_at = NULL,
      deletion_requested_at = pg_catalog.clock_timestamp(),
      projection_sequence = new_sequence,
      updated_at = pg_catalog.clock_timestamp()
  WHERE owner_user_id = actor AND claim_id = target.claim_id;
  INSERT INTO memory.projection_outbox(
    outbox_id, owner_user_id, claim_id, revision_id, operation_id,
    sequence_number, operation, point_id, revision_sha256,
    selection_binding_sha256, projection_manifest_sha256
  ) VALUES (
    new_outbox_id, actor, target.claim_id, revision.revision_id,
    p_operation_id, new_sequence, 'delete', target.claim_id,
    revision.revision_sha256, revision.selection_binding_sha256, manifest_hash
  );
  event_hash := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(
      actor::text || '|' || p_operation_id::text || '|deletion_pending|'
      || revision.revision_sha256, 'UTF8'
    )
  ), 'hex');
  INSERT INTO memory.audit_event(
    owner_user_id, operation_id, actor_kind, actor_user_id,
    object_type, object_id, transition_code, prior_state_sha256,
    new_state_sha256, reason_code, event_sha256
  ) VALUES (
    actor, p_operation_id, 'owner', actor, 'claim', target.claim_id,
    'claim_deletion_requested', prior_state_hash, new_state_hash,
    'explicit_owner_deletion', event_hash
  );
  RETURN QUERY SELECT 'deletion_pending'::text, new_outbox_id;
END;
$function$;

CREATE FUNCTION memory_private.lease_projection_jobs(
  p_worker_id text,
  p_limit integer,
  p_lease_seconds integer
)
RETURNS TABLE(
  owner_user_id uuid,
  outbox_id uuid,
  claim_id uuid,
  revision_id uuid,
  revision_number integer,
  operation_id uuid,
  operation text,
  sequence_number integer,
  point_id uuid,
  collection_alias text,
  revision_sha256 text,
  selection_binding_sha256 text,
  predicate_catalog_sha256 text,
  projection_contract_sha256 text,
  dimensions integer,
  embedding_model text,
  renderer_sha256 text,
  projection_manifest_sha256 text,
  retrieval_text text,
  retrieval_text_sha256 text,
  embedding_input_sha256 text,
  source_sha256 text,
  lifecycle_state text,
  is_current boolean,
  predicate text,
  epistemic_state text,
  sensitivity text,
  domains text[],
  intents text[],
  surface text,
  requires_explicit boolean,
  projectable boolean,
  valid_from timestamptz,
  valid_to timestamptz,
  claim_updated_at timestamptz,
  state_sha256 text,
  semantic_key_sha256 text,
  claim_identity_sha256 text,
  subject_entity_key text,
  subject_entity_type text,
  subject_display_name text,
  object_kind text,
  object_entity_key text,
  object_entity_type text,
  object_display_name text,
  object_literal jsonb,
  lease_token uuid,
  lease_expires_at timestamptz
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  candidate record;
  new_lease_token uuid;
  new_lease_expires_at timestamptz;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF COALESCE(p_worker_id, '') !~ '^[a-z][a-z0-9_:/.-]{0,127}$'
     OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
     OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 5 AND 300 THEN
    RAISE EXCEPTION 'invalid projection lease input' USING ERRCODE = '22023';
  END IF;

  UPDATE memory.projection_outbox AS expired_outbox
  SET state = CASE
        WHEN expired_outbox.attempt_count >= expired_outbox.max_attempts
          THEN 'failed_terminal'
        ELSE 'retryable'
      END,
      available_at = pg_catalog.clock_timestamp(),
      lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
      lease_expires_at = NULL, last_error_code = 'lease_expired',
      completion_lease_token = NULL, completion_outcome = NULL,
      updated_at = pg_catalog.clock_timestamp()
  WHERE expired_outbox.state = 'claimed'
    AND expired_outbox.lease_expires_at <= pg_catalog.clock_timestamp();

  WITH superseded AS (
    UPDATE memory.projection_outbox AS outbox
    SET state = 'superseded',
        last_error_code = 'stale_projection_sequence',
        completion_lease_token = NULL, completion_outcome = NULL,
        updated_at = pg_catalog.transaction_timestamp()
    WHERE outbox.state IN ('pending', 'retryable')
      AND EXISTS (
        SELECT 1
        FROM memory.claim AS claim
        WHERE claim.owner_user_id = outbox.owner_user_id
          AND claim.claim_id = outbox.claim_id
          AND outbox.sequence_number < claim.projection_sequence
      )
    RETURNING outbox.owner_user_id, outbox.outbox_id,
      outbox.operation_id, outbox.projection_manifest_sha256,
      outbox.sequence_number
  ), receipts AS (
    SELECT superseded.*,
      pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        superseded.owner_user_id::text || '|'
          || superseded.outbox_id::text || '|'
          || superseded.operation_id::text || '|'
          || superseded.sequence_number::text
          || '|stale_projection_sequence',
        'UTF8'
      )), 'hex') AS receipt_sha256
    FROM superseded
  )
  INSERT INTO memory.audit_event(
    owner_user_id, operation_id, actor_kind, object_type, object_id,
    transition_code, prior_state_sha256, new_state_sha256,
    reason_code, event_sha256
  )
  SELECT receipt.owner_user_id, receipt.operation_id,
    'system', 'projection_outbox', receipt.outbox_id,
    'projection_superseded', receipt.projection_manifest_sha256,
    receipt.receipt_sha256, 'stale_projection_sequence',
    receipt.receipt_sha256
  FROM receipts AS receipt;

  FOR candidate IN
    SELECT outbox.*, revision.revision_number,
           revision.predicate_catalog_sha256,
           revision.retrieval_text, revision.retrieval_text_sha256,
           revision.source_sha256, revision.predicate,
           revision.epistemic_state, revision.sensitivity,
           revision.domains, revision.intents, revision.surface,
           revision.requires_explicit, revision.projectable,
           revision.valid_from, revision.valid_to,
           claim.lifecycle_state, claim.current_revision_id,
           claim.current_state_sha256, claim.semantic_key_sha256,
           claim.claim_identity_sha256,
           revision.subject_entity_key, revision.subject_entity_type,
           revision.subject_display_name,
           revision.object_kind, revision.object_entity_key,
           revision.object_entity_type, revision.object_display_name,
           revision.object_literal,
           claim.updated_at AS claim_updated_at
    FROM memory.projection_outbox AS outbox
    JOIN memory.claim_revision AS revision
      ON revision.owner_user_id = outbox.owner_user_id
     AND revision.claim_id = outbox.claim_id
     AND revision.revision_id = outbox.revision_id
    JOIN memory.claim AS claim
      ON claim.owner_user_id = outbox.owner_user_id
     AND claim.claim_id = outbox.claim_id
    WHERE outbox.state IN ('pending', 'retryable')
      AND outbox.available_at <= pg_catalog.clock_timestamp()
      AND outbox.attempt_count < outbox.max_attempts
      AND claim.projection_sequence = outbox.sequence_number
      AND claim.current_revision_id = outbox.revision_id
      AND claim.current_state_sha256 = memory_private.claim_state_sha256(
        claim.owner_user_id, claim.claim_id, claim.semantic_key_sha256,
        claim.claim_identity_sha256, claim.lifecycle_state, true,
        claim.current_revision_id, claim.current_revision_number,
        revision.revision_sha256, claim.projection_sequence
      )
      AND outbox.projection_manifest_sha256
            = memory_private.projection_manifest_sha256(
                outbox.owner_user_id, outbox.claim_id, outbox.revision_id,
                outbox.operation_id, outbox.operation,
                outbox.sequence_number, outbox.revision_sha256,
                outbox.selection_binding_sha256,
                revision.retrieval_text_sha256,
                revision.retrieval_text_sha256
              )
      AND (
        (outbox.operation = 'upsert'
          AND claim.lifecycle_state = 'active'
          AND revision.projectable)
        OR
        (outbox.operation = 'delete'
          AND claim.lifecycle_state IN (
            'correction_pending', 'retracted', 'deletion_pending'
          ))
      )
      AND NOT EXISTS (
        SELECT 1 FROM memory.projection_outbox AS earlier
        WHERE earlier.owner_user_id = outbox.owner_user_id
          AND earlier.claim_id = outbox.claim_id
          AND earlier.sequence_number < outbox.sequence_number
          AND earlier.state IN ('pending', 'retryable', 'claimed')
      )
    ORDER BY outbox.available_at, outbox.created_at, outbox.outbox_id
    FOR UPDATE OF outbox SKIP LOCKED
    LIMIT p_limit
  LOOP
    new_lease_token := pg_catalog.gen_random_uuid();
    new_lease_expires_at := pg_catalog.clock_timestamp()
      + pg_catalog.make_interval(secs => p_lease_seconds);
    UPDATE memory.projection_outbox
    SET state = 'claimed', attempt_count = attempt_count + 1,
        lease_token = new_lease_token, claimed_by = p_worker_id,
        claimed_at = pg_catalog.clock_timestamp(),
        lease_expires_at = new_lease_expires_at,
        last_error_code = NULL, completion_lease_token = NULL,
        completion_outcome = NULL, updated_at = pg_catalog.clock_timestamp()
    WHERE projection_outbox.owner_user_id = candidate.owner_user_id
      AND projection_outbox.outbox_id = candidate.outbox_id;
    RETURN QUERY SELECT candidate.owner_user_id, candidate.outbox_id,
      candidate.claim_id, candidate.revision_id, candidate.revision_number,
      candidate.operation_id,
      candidate.operation,
      candidate.sequence_number, candidate.point_id,
      candidate.collection_alias,
      candidate.revision_sha256, candidate.selection_binding_sha256,
      candidate.predicate_catalog_sha256,
      memory_private.projection_contract_sha256(), 3072,
      'text-embedding-3-large'::text,
      'f77b782b3e549b30a46b4beb7e25b248018f6c0f3597b36d0020103570e66442',
      candidate.projection_manifest_sha256,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.retrieval_text ELSE NULL::text END,
      candidate.retrieval_text_sha256,
      candidate.retrieval_text_sha256,
      candidate.source_sha256, candidate.lifecycle_state,
      candidate.current_revision_id = candidate.revision_id,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.predicate ELSE NULL::text END,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.epistemic_state ELSE NULL::text END,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.sensitivity ELSE NULL::text END,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.domains ELSE ARRAY[]::text[] END,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.intents ELSE ARRAY[]::text[] END,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.surface ELSE NULL::text END,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.requires_explicit ELSE false END,
      CASE WHEN candidate.operation = 'upsert'
        THEN candidate.projectable ELSE false END,
      candidate.valid_from, candidate.valid_to, candidate.claim_updated_at,
      candidate.current_state_sha256, candidate.semantic_key_sha256,
      candidate.claim_identity_sha256,
      candidate.subject_entity_key, candidate.subject_entity_type,
      candidate.subject_display_name,
      candidate.object_kind, candidate.object_entity_key,
      candidate.object_entity_type, candidate.object_display_name,
      candidate.object_literal,
      new_lease_token, new_lease_expires_at;
  END LOOP;
END;
$function$;

CREATE FUNCTION memory_private.finish_projection_job(
  p_outbox_id uuid,
  p_lease_token uuid,
  p_outcome text,
  p_physical_collection_name text,
  p_vector_sha256 text,
  p_verification_sha256 text,
  p_verification_receipt_sha256 text,
  p_error_code text
)
RETURNS TABLE(outcome text, deletion_ready boolean)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  outbox memory.projection_outbox%ROWTYPE;
  target memory.claim%ROWTYPE;
  revision memory.claim_revision%ROWTYPE;
  expected_manifest text;
  completion_at timestamptz;
  resulting_state text;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_outbox_id IS NULL OR p_lease_token IS NULL
     OR p_outcome IS NULL
     OR p_outcome NOT IN ('applied', 'retryable', 'failed_terminal')
     OR (p_physical_collection_name IS NOT NULL AND
       p_physical_collection_name
         !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
     OR (p_vector_sha256 IS NOT NULL AND p_vector_sha256 !~ '^[0-9a-f]{64}$')
     OR (
       p_verification_sha256 IS NOT NULL
       AND p_verification_sha256 !~ '^[0-9a-f]{64}$'
     )
     OR (
       p_verification_receipt_sha256 IS NOT NULL
       AND p_verification_receipt_sha256 !~ '^[0-9a-f]{64}$'
     )
     OR (p_error_code IS NOT NULL
       AND p_error_code !~ '^[a-z][a-z0-9_]{0,127}$')
     OR (
       p_outcome = 'retryable'
       AND p_error_code NOT IN (
         'embedding_provider_unavailable', 'embedding_timeout',
         'qdrant_rate_limited', 'qdrant_timeout', 'qdrant_unavailable',
         'qdrant_verification_inconclusive'
       )
     )
     OR (
       p_outcome = 'failed_terminal'
       AND p_error_code NOT IN (
         'embedding_contract_violation', 'projection_contract_violation',
         'qdrant_collection_mismatch', 'qdrant_dimension_mismatch',
         'qdrant_receipt_contract_violation'
       )
     ) THEN
    RAISE EXCEPTION 'invalid projection completion input'
      USING ERRCODE = '22023';
  END IF;
  SELECT value.* INTO STRICT outbox
  FROM memory.projection_outbox AS value
  WHERE value.outbox_id = p_outbox_id
  FOR UPDATE;
  resulting_state := CASE
    WHEN p_outcome = 'applied' THEN 'applied'
    WHEN p_outcome = 'retryable' AND outbox.attempt_count < outbox.max_attempts
      THEN 'retryable'
    ELSE 'failed_terminal'
  END;
  IF outbox.completion_lease_token IS NOT NULL THEN
    IF outbox.completion_lease_token IS DISTINCT FROM p_lease_token THEN
      RAISE EXCEPTION 'stale projection lease' USING ERRCODE = '40001';
    END IF;
    IF outbox.completion_outcome IS DISTINCT FROM p_outcome
       OR outbox.state IS DISTINCT FROM resulting_state
       OR outbox.physical_collection_name
            IS DISTINCT FROM p_physical_collection_name
       OR outbox.vector_sha256 IS DISTINCT FROM p_vector_sha256
       OR outbox.verification_sha256 IS DISTINCT FROM p_verification_sha256
       OR outbox.verification_receipt_sha256
            IS DISTINCT FROM p_verification_receipt_sha256
       OR outbox.last_error_code IS DISTINCT FROM p_error_code THEN
      RAISE EXCEPTION 'projection completion replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT 'replayed'::text,
      outbox.state = 'applied' AND outbox.operation = 'delete';
    RETURN;
  END IF;
  IF outbox.state <> 'claimed' OR outbox.lease_token <> p_lease_token
     OR outbox.lease_expires_at <= pg_catalog.clock_timestamp() THEN
    RAISE EXCEPTION 'stale projection lease' USING ERRCODE = '40001';
  END IF;
  IF p_outcome = 'applied' AND (
    (outbox.operation = 'upsert' AND (
      p_physical_collection_name IS NULL
      OR p_vector_sha256 IS NULL
      OR p_verification_sha256 IS NOT NULL
      OR p_verification_receipt_sha256 IS NOT NULL
    ))
    OR (outbox.operation = 'delete' AND (
      p_physical_collection_name IS NULL
      OR p_vector_sha256 IS NOT NULL
      OR p_verification_sha256 IS NULL
      OR p_verification_receipt_sha256 IS NULL
    ))
    OR p_error_code IS NOT NULL
  ) THEN
    RAISE EXCEPTION 'applied projection receipt is invalid'
      USING ERRCODE = '22023';
  END IF;
  IF p_outcome <> 'applied' AND (
    p_error_code IS NULL OR p_physical_collection_name IS NOT NULL
    OR p_vector_sha256 IS NOT NULL
    OR p_verification_sha256 IS NOT NULL
    OR p_verification_receipt_sha256 IS NOT NULL
  ) THEN
    RAISE EXCEPTION 'failed projection requires an error code'
      USING ERRCODE = '22023';
  END IF;

  IF p_outcome <> 'applied' THEN
    UPDATE memory.projection_outbox
    SET state = resulting_state,
        available_at = CASE
          WHEN p_outcome = 'retryable' AND attempt_count < max_attempts
            THEN pg_catalog.clock_timestamp() + interval '30 seconds'
          ELSE available_at
        END,
        lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
        lease_expires_at = NULL, last_error_code = p_error_code,
        completion_lease_token = p_lease_token,
        completion_outcome = p_outcome,
        updated_at = pg_catalog.clock_timestamp()
    WHERE projection_outbox.owner_user_id = outbox.owner_user_id
      AND projection_outbox.outbox_id = outbox.outbox_id;
    RETURN QUERY SELECT resulting_state, false;
    RETURN;
  END IF;

  SELECT value.* INTO STRICT target
  FROM memory.claim AS value
  WHERE value.owner_user_id = outbox.owner_user_id
    AND value.claim_id = outbox.claim_id
  FOR UPDATE;
  SELECT value.* INTO STRICT revision
  FROM memory.claim_revision AS value
  WHERE value.owner_user_id = outbox.owner_user_id
    AND value.claim_id = outbox.claim_id
    AND value.revision_id = outbox.revision_id;
  expected_manifest := memory_private.projection_manifest_sha256(
    outbox.owner_user_id, outbox.claim_id, outbox.revision_id,
    outbox.operation_id, outbox.operation, outbox.sequence_number,
    outbox.revision_sha256, outbox.selection_binding_sha256,
    revision.retrieval_text_sha256,
    revision.retrieval_text_sha256
  );
  IF target.projection_sequence <> outbox.sequence_number
     OR target.current_revision_id <> outbox.revision_id
     OR revision.revision_sha256 <> outbox.revision_sha256
     OR revision.selection_binding_sha256
          <> outbox.selection_binding_sha256
     OR outbox.projection_manifest_sha256 <> expected_manifest
     OR (
       outbox.operation = 'upsert'
       AND (target.lifecycle_state <> 'active' OR NOT revision.projectable)
     )
     OR (
       outbox.operation = 'delete'
       AND target.lifecycle_state NOT IN (
         'correction_pending', 'retracted', 'deletion_pending'
       )
     ) THEN
    RAISE EXCEPTION 'projection authority advanced before completion'
      USING ERRCODE = '40001';
  END IF;

  completion_at := pg_catalog.transaction_timestamp();
  UPDATE memory.projection_outbox
  SET state = 'applied',
      physical_collection_name = p_physical_collection_name,
      vector_sha256 = p_vector_sha256,
      verification_sha256 = p_verification_sha256,
      verification_receipt_sha256 = p_verification_receipt_sha256,
      verified_at = CASE WHEN operation = 'delete'
        THEN completion_at ELSE NULL::timestamptz END,
      applied_at = completion_at,
      lease_token = NULL, claimed_by = NULL, claimed_at = NULL,
      lease_expires_at = NULL, last_error_code = NULL,
      completion_lease_token = p_lease_token,
      completion_outcome = p_outcome,
      updated_at = pg_catalog.clock_timestamp()
  WHERE projection_outbox.owner_user_id = outbox.owner_user_id
    AND projection_outbox.outbox_id = outbox.outbox_id;

  RETURN QUERY SELECT 'applied'::text, outbox.operation = 'delete';
END;
$function$;

CREATE FUNCTION memory_private.finalize_claim_deletion(
  p_operation_id uuid,
  p_owner_user_id uuid,
  p_claim_id uuid,
  p_expected_state_sha256 text,
  p_delete_outbox_id uuid,
  p_expected_revision_id uuid,
  p_expected_revision_sha256 text,
  p_expected_sequence_number integer,
  p_projection_manifest_sha256 text,
  p_physical_collection_name text,
  p_applied_at timestamptz,
  p_verified_at timestamptz,
  p_verification_sha256 text,
  p_verification_receipt_sha256 text
)
RETURNS TABLE(outcome text, audit_event_id uuid)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  existing_receipt memory.audit_event%ROWTYPE;
  stored_deletion_receipt memory.claim_deletion_receipt%ROWTYPE;
  target memory.claim%ROWTYPE;
  current_revision memory.claim_revision%ROWTYPE;
  delete_receipt memory.projection_outbox%ROWTYPE;
  new_audit_event_id uuid;
  event_hash text;
  deleted_revision_ids uuid[] := ARRAY[]::uuid[];
  deleted_proposal_ids uuid[] := ARRAY[]::uuid[];
  candidate_evidence_ids uuid[] := ARRAY[]::uuid[];
  candidate_entity_ids uuid[] := ARRAY[]::uuid[];
  candidate_call_ids uuid[] := ARRAY[]::uuid[];
  candidate_job_ids uuid[] := ARRAY[]::uuid[];
  recomputed_state_sha256 text;
  recomputed_manifest_sha256 text;
BEGIN
  IF session_user <> 'governed_memory_worker' THEN
    RAISE EXCEPTION 'worker role required' USING ERRCODE = '42501';
  END IF;
  IF p_operation_id IS NULL OR p_owner_user_id IS NULL
     OR p_owner_user_id = '00000000-0000-0000-0000-000000000000'::uuid
     OR p_claim_id IS NULL OR p_delete_outbox_id IS NULL
     OR p_expected_revision_id IS NULL
     OR p_expected_revision_sha256 IS NULL
     OR p_expected_revision_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_sequence_number IS NULL
     OR p_expected_sequence_number <= 0
     OR p_expected_state_sha256 IS NULL
     OR p_expected_state_sha256 !~ '^[0-9a-f]{64}$'
     OR p_projection_manifest_sha256 IS NULL
     OR p_projection_manifest_sha256 !~ '^[0-9a-f]{64}$'
     OR p_physical_collection_name IS NULL
     OR p_physical_collection_name
          !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
     OR p_applied_at IS NULL OR p_verified_at IS NULL
     OR p_verified_at < p_applied_at
     OR p_verification_sha256 IS NULL
     OR p_verification_sha256 !~ '^[0-9a-f]{64}$'
     OR p_verification_receipt_sha256 IS NULL
     OR p_verification_receipt_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid claim-deletion finalization input'
      USING ERRCODE = '22023';
  END IF;

  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      p_owner_user_id::text || '|operation|' || p_operation_id::text, 0
    )
  );
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      p_owner_user_id::text || '|claim|' || p_claim_id::text, 0
    )
  );
  SELECT value.* INTO stored_deletion_receipt
  FROM memory.claim_deletion_receipt AS value
  WHERE value.owner_user_id = p_owner_user_id
    AND value.operation_id = p_operation_id;
  IF FOUND THEN
    event_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
      'governed_memory.deletion_receipt.v1' || E'\n'
        || memory_private.framed_utf8_field(
             'owner_user_id', p_owner_user_id::text
           )
        || memory_private.framed_utf8_field('claim_id', p_claim_id::text)
        || memory_private.framed_utf8_field(
             'operation_id', p_operation_id::text
           )
        || memory_private.framed_utf8_field(
             'prior_state_sha256', p_expected_state_sha256
           )
        || memory_private.framed_utf8_field(
             'delete_outbox_id', p_delete_outbox_id::text
           )
        || memory_private.framed_utf8_field(
             'revision_id', p_expected_revision_id::text
           )
        || memory_private.framed_utf8_field(
             'revision_sha256', p_expected_revision_sha256
           )
        || memory_private.framed_utf8_field(
             'sequence_number', p_expected_sequence_number::text
           )
        || memory_private.framed_utf8_field(
             'collection_alias', 'governed_memory_active'
           )
        || memory_private.framed_utf8_field(
             'physical_collection_name', p_physical_collection_name
           )
        || memory_private.framed_utf8_field('point_id', p_claim_id::text)
        || memory_private.framed_utf8_field(
             'projection_manifest_sha256', p_projection_manifest_sha256
           )
        || memory_private.framed_utf8_field(
             'absence_verification_sha256', p_verification_sha256
           )
        || memory_private.framed_utf8_field(
             'absence_verification_receipt_sha256',
             p_verification_receipt_sha256
           )
        || memory_private.framed_utf8_field(
             'applied_at', memory_private.timestamp_utc_text(p_applied_at)
           )
        || memory_private.framed_utf8_field(
             'verified_at', memory_private.timestamp_utc_text(p_verified_at)
           ),
      'UTF8'
    )), 'hex');
    IF stored_deletion_receipt.claim_id <> p_claim_id
       OR stored_deletion_receipt.prior_state_sha256
            <> p_expected_state_sha256
       OR stored_deletion_receipt.delete_outbox_id <> p_delete_outbox_id
       OR stored_deletion_receipt.revision_id <> p_expected_revision_id
       OR stored_deletion_receipt.revision_sha256
            <> p_expected_revision_sha256
       OR stored_deletion_receipt.sequence_number
            <> p_expected_sequence_number
       OR stored_deletion_receipt.collection_alias
            <> 'governed_memory_active'
       OR stored_deletion_receipt.physical_collection_name
            <> p_physical_collection_name
       OR stored_deletion_receipt.point_id <> p_claim_id
       OR stored_deletion_receipt.projection_manifest_sha256
            <> p_projection_manifest_sha256
       OR stored_deletion_receipt.applied_at <> p_applied_at
       OR stored_deletion_receipt.verified_at <> p_verified_at
       OR stored_deletion_receipt.absence_verification_sha256
            <> p_verification_sha256
       OR stored_deletion_receipt.absence_verification_receipt_sha256
            <> p_verification_receipt_sha256
       OR stored_deletion_receipt.receipt_sha256 <> event_hash THEN
      RAISE EXCEPTION 'claim-deletion operation replay drifted'
        USING ERRCODE = '23514';
    END IF;
    SELECT value.* INTO STRICT existing_receipt
    FROM memory.audit_event AS value
    WHERE value.owner_user_id = p_owner_user_id
      AND value.event_id = stored_deletion_receipt.audit_event_id
      AND value.operation_id = p_operation_id
      AND value.transition_code = 'claim_deleted';
    IF existing_receipt.actor_kind <> 'worker'
       OR existing_receipt.actor_user_id IS NOT NULL
       OR existing_receipt.object_type <> 'claim'
       OR existing_receipt.object_id <> p_claim_id
       OR existing_receipt.prior_state_sha256 <> p_expected_state_sha256
       OR existing_receipt.new_state_sha256 <> event_hash
       OR existing_receipt.reason_code <> 'qdrant_delete_verified'
       OR existing_receipt.event_sha256 <> event_hash THEN
      RAISE EXCEPTION 'claim-deletion audit replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT 'replayed'::text, existing_receipt.event_id;
    RETURN;
  END IF;
  IF memory_private.operation_id_conflicts(
       p_owner_user_id, p_operation_id, NULL::uuid, p_delete_outbox_id,
       p_claim_id,
       ARRAY[
         'claim_deletion_requested',
         'correction_rejected_lifecycle_override'
       ]::text[]
     ) THEN
    RAISE EXCEPTION 'operation id is reserved by another mutation'
      USING ERRCODE = '23514';
  END IF;

  SELECT value.* INTO STRICT target
  FROM memory.claim AS value
  WHERE value.owner_user_id = p_owner_user_id
    AND value.claim_id = p_claim_id
  FOR UPDATE;
  SELECT value.* INTO STRICT current_revision
  FROM memory.claim_revision AS value
  WHERE value.owner_user_id = target.owner_user_id
    AND value.claim_id = target.claim_id
    AND value.revision_id = target.current_revision_id;
  SELECT value.* INTO STRICT delete_receipt
  FROM memory.projection_outbox AS value
  WHERE value.owner_user_id = p_owner_user_id
    AND value.claim_id = p_claim_id
    AND value.outbox_id = p_delete_outbox_id
  FOR UPDATE;
  recomputed_state_sha256 := memory_private.claim_state_sha256(
    target.owner_user_id, target.claim_id, target.semantic_key_sha256,
    target.claim_identity_sha256, target.lifecycle_state, true,
    target.current_revision_id, target.current_revision_number,
    current_revision.revision_sha256, target.projection_sequence
  );
  recomputed_manifest_sha256 := memory_private.projection_manifest_sha256(
    target.owner_user_id, target.claim_id, current_revision.revision_id,
    delete_receipt.operation_id, 'delete', target.projection_sequence,
    current_revision.revision_sha256,
    current_revision.selection_binding_sha256,
    current_revision.retrieval_text_sha256,
    current_revision.retrieval_text_sha256
  );
  IF target.lifecycle_state <> 'deletion_pending'
     OR target.current_state_sha256 <> p_expected_state_sha256
     OR recomputed_state_sha256 <> target.current_state_sha256
     OR target.current_revision_id <> p_expected_revision_id
     OR target.current_revision_number <> current_revision.revision_number
     OR target.projection_sequence <> p_expected_sequence_number
     OR delete_receipt.operation <> 'delete'
     OR delete_receipt.operation_id <> p_operation_id
     OR delete_receipt.state <> 'applied'
     OR delete_receipt.applied_at <> p_applied_at
     OR delete_receipt.verified_at <> p_verified_at
     OR delete_receipt.revision_id <> target.current_revision_id
     OR delete_receipt.revision_id <> p_expected_revision_id
     OR delete_receipt.sequence_number <> target.projection_sequence
     OR delete_receipt.point_id <> target.claim_id
     OR delete_receipt.collection_alias <> 'governed_memory_active'
     OR delete_receipt.physical_collection_name
          <> p_physical_collection_name
     OR delete_receipt.revision_sha256 <> current_revision.revision_sha256
     OR delete_receipt.revision_sha256 <> p_expected_revision_sha256
     OR delete_receipt.selection_binding_sha256
          <> current_revision.selection_binding_sha256
     OR delete_receipt.projection_manifest_sha256
          <> p_projection_manifest_sha256
     OR recomputed_manifest_sha256 <> p_projection_manifest_sha256
     OR delete_receipt.verification_sha256 <> p_verification_sha256
     OR delete_receipt.verification_receipt_sha256
          <> p_verification_receipt_sha256
     OR NOT EXISTS (
       SELECT 1 FROM memory.audit_event AS transition
       WHERE transition.owner_user_id = target.owner_user_id
         AND transition.operation_id = delete_receipt.operation_id
         AND transition.object_type = 'claim'
         AND transition.object_id = target.claim_id
         AND transition.transition_code = 'claim_deletion_requested'
         AND transition.reason_code = 'explicit_owner_deletion'
         AND transition.new_state_sha256 = target.current_state_sha256
     ) THEN
    RAISE EXCEPTION 'exact applied Qdrant delete receipt is absent or stale'
      USING ERRCODE = '40001';
  END IF;

  SELECT
    COALESCE(pg_catalog.array_agg(revision.revision_id), ARRAY[]::uuid[]),
    COALESCE(pg_catalog.array_agg(revision.source_proposal_id), ARRAY[]::uuid[]),
    COALESCE(pg_catalog.array_agg(revision.subject_entity_id), ARRAY[]::uuid[])
      || COALESCE(pg_catalog.array_agg(revision.object_entity_id)
        FILTER (WHERE revision.object_entity_id IS NOT NULL), ARRAY[]::uuid[])
  INTO deleted_revision_ids, deleted_proposal_ids, candidate_entity_ids
  FROM memory.claim_revision AS revision
  WHERE revision.owner_user_id = p_owner_user_id
    AND revision.claim_id = p_claim_id;
  IF pg_catalog.cardinality(deleted_revision_ids) = 0 THEN
    RAISE EXCEPTION 'claim has no revision set' USING ERRCODE = '23514';
  END IF;
  SELECT COALESCE(
    pg_catalog.array_agg(DISTINCT source.evidence_id), ARRAY[]::uuid[]
  ) INTO candidate_evidence_ids
  FROM (
    SELECT proposal.evidence_id
    FROM memory.proposal AS proposal
    WHERE proposal.owner_user_id = p_owner_user_id
      AND (
        proposal.proposal_id = ANY(deleted_proposal_ids)
        OR proposal.correction_of_claim_id = p_claim_id
      )
    UNION
    SELECT link.evidence_id
    FROM memory.claim_evidence AS link
    WHERE link.owner_user_id = p_owner_user_id
      AND link.claim_id = p_claim_id
  ) AS source;
  SELECT
    COALESCE(
      pg_catalog.array_agg(DISTINCT proposal.provider_call_id)
        FILTER (WHERE proposal.provider_call_id IS NOT NULL),
      ARRAY[]::uuid[]
    )
  INTO candidate_call_ids
  FROM memory.proposal AS proposal
  WHERE proposal.owner_user_id = p_owner_user_id
    AND (
      proposal.proposal_id = ANY(deleted_proposal_ids)
      OR proposal.correction_of_claim_id = p_claim_id
    );
  SELECT COALESCE(
    pg_catalog.array_agg(DISTINCT call.job_id), ARRAY[]::uuid[]
  )
  INTO candidate_job_ids
  FROM memory.provider_call AS call
  WHERE call.owner_user_id = p_owner_user_id
    AND call.provider_call_id = ANY(candidate_call_ids);

  event_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    'governed_memory.deletion_receipt.v1' || E'\n'
      || memory_private.framed_utf8_field(
           'owner_user_id', p_owner_user_id::text
         )
      || memory_private.framed_utf8_field('claim_id', p_claim_id::text)
      || memory_private.framed_utf8_field(
           'operation_id', p_operation_id::text
         )
      || memory_private.framed_utf8_field(
           'prior_state_sha256', p_expected_state_sha256
         )
      || memory_private.framed_utf8_field(
           'delete_outbox_id', p_delete_outbox_id::text
         )
      || memory_private.framed_utf8_field(
           'revision_id', delete_receipt.revision_id::text
         )
      || memory_private.framed_utf8_field(
           'revision_sha256', delete_receipt.revision_sha256
         )
      || memory_private.framed_utf8_field(
           'sequence_number', delete_receipt.sequence_number::text
         )
      || memory_private.framed_utf8_field(
           'collection_alias', delete_receipt.collection_alias
         )
      || memory_private.framed_utf8_field(
           'physical_collection_name',
           delete_receipt.physical_collection_name
         )
      || memory_private.framed_utf8_field(
           'point_id', delete_receipt.point_id::text
         )
      || memory_private.framed_utf8_field(
           'projection_manifest_sha256', p_projection_manifest_sha256
         )
      || memory_private.framed_utf8_field(
           'absence_verification_sha256', p_verification_sha256
         )
      || memory_private.framed_utf8_field(
           'absence_verification_receipt_sha256',
           p_verification_receipt_sha256
         )
      || memory_private.framed_utf8_field(
           'applied_at',
           memory_private.timestamp_utc_text(delete_receipt.applied_at)
         )
      || memory_private.framed_utf8_field(
           'verified_at',
           memory_private.timestamp_utc_text(delete_receipt.verified_at)
         ),
    'UTF8'
  )), 'hex');

  PERFORM pg_catalog.set_config(
    'app.memory_hard_delete_operation_id', p_operation_id::text, true
  );
  SET CONSTRAINTS ALL DEFERRED;
  DELETE FROM memory.answer_binding AS binding
  WHERE binding.owner_user_id = p_owner_user_id
    AND (
      binding.selected_revision_ids && deleted_revision_ids
      OR binding.injected_revision_ids && deleted_revision_ids
    );
  DELETE FROM memory.claim_evidence AS link
  WHERE link.owner_user_id = p_owner_user_id
    AND link.claim_id = p_claim_id;
  DELETE FROM memory.projection_outbox AS pending
  WHERE pending.owner_user_id = p_owner_user_id
    AND pending.claim_id = p_claim_id;
  DELETE FROM memory.proposal AS proposal
  WHERE proposal.owner_user_id = p_owner_user_id
    AND (
      proposal.proposal_id = ANY(deleted_proposal_ids)
      OR proposal.correction_of_claim_id = p_claim_id
    );
  DELETE FROM memory.claim_revision AS revision
  WHERE revision.owner_user_id = p_owner_user_id
    AND revision.claim_id = p_claim_id;
  DELETE FROM memory.claim AS claim
  WHERE claim.owner_user_id = p_owner_user_id
    AND claim.claim_id = p_claim_id;
  DELETE FROM memory.provider_call AS call
  WHERE call.owner_user_id = p_owner_user_id
    AND (
      call.provider_call_id = ANY(candidate_call_ids)
      OR call.job_id = ANY(candidate_job_ids)
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.proposal AS proposal
      WHERE proposal.owner_user_id = call.owner_user_id
        AND proposal.provider_call_id = call.provider_call_id
    );
  DELETE FROM memory.extraction_job AS job
  WHERE job.owner_user_id = p_owner_user_id
    AND job.job_id = ANY(candidate_job_ids)
    AND NOT EXISTS (
      SELECT 1 FROM memory.provider_call AS call
      WHERE call.owner_user_id = job.owner_user_id
        AND call.job_id = job.job_id
    );
  DELETE FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id = p_owner_user_id
    AND evidence.evidence_id = ANY(candidate_evidence_ids)
    AND NOT EXISTS (
      SELECT 1 FROM memory.claim_evidence AS link
      WHERE link.owner_user_id = evidence.owner_user_id
        AND link.evidence_id = evidence.evidence_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM memory.proposal AS proposal
      WHERE proposal.owner_user_id = evidence.owner_user_id
        AND proposal.evidence_id = evidence.evidence_id
    );
  DELETE FROM memory.entity AS entity
  WHERE entity.owner_user_id = p_owner_user_id
    AND entity.entity_id = ANY(candidate_entity_ids)
    AND NOT EXISTS (
      SELECT 1 FROM memory.claim_revision AS revision
      WHERE revision.owner_user_id = entity.owner_user_id
        AND (
          revision.subject_entity_id = entity.entity_id
          OR revision.object_entity_id = entity.entity_id
        )
    );
  IF EXISTS (
    SELECT 1 FROM memory.claim AS claim
    WHERE claim.owner_user_id = p_owner_user_id
      AND claim.claim_id = p_claim_id
  ) OR EXISTS (
    SELECT 1 FROM memory.claim_revision AS revision
    WHERE revision.owner_user_id = p_owner_user_id
      AND revision.claim_id = p_claim_id
  ) OR EXISTS (
    SELECT 1 FROM memory.claim_evidence AS link
    WHERE link.owner_user_id = p_owner_user_id
      AND link.claim_id = p_claim_id
  ) OR EXISTS (
    SELECT 1 FROM memory.projection_outbox AS outbox
    WHERE outbox.owner_user_id = p_owner_user_id
      AND outbox.claim_id = p_claim_id
  ) THEN
    RAISE EXCEPTION 'structured claim absence verification failed'
      USING ERRCODE = '40001';
  END IF;
  new_audit_event_id := pg_catalog.gen_random_uuid();
  INSERT INTO memory.audit_event(
    event_id, owner_user_id, operation_id, actor_kind, object_type,
    object_id, transition_code, prior_state_sha256, new_state_sha256,
    reason_code, event_sha256
  ) VALUES (
    new_audit_event_id, p_owner_user_id, p_operation_id, 'worker', 'claim',
    p_claim_id, 'claim_deleted', p_expected_state_sha256,
    event_hash, 'qdrant_delete_verified', event_hash
  );
  INSERT INTO memory.claim_deletion_receipt(
    audit_event_id, owner_user_id, operation_id, claim_id,
    prior_state_sha256, delete_outbox_id, revision_id, revision_sha256,
    sequence_number, collection_alias, physical_collection_name, point_id,
    projection_manifest_sha256, applied_at, verified_at,
    absence_verification_sha256,
    absence_verification_receipt_sha256, receipt_sha256
  ) VALUES (
    new_audit_event_id, p_owner_user_id, p_operation_id, p_claim_id,
    p_expected_state_sha256, p_delete_outbox_id,
    p_expected_revision_id, p_expected_revision_sha256,
    p_expected_sequence_number, delete_receipt.collection_alias,
    p_physical_collection_name, p_claim_id,
    p_projection_manifest_sha256, p_applied_at, p_verified_at,
    p_verification_sha256, p_verification_receipt_sha256, event_hash
  );
  RETURN QUERY SELECT 'deleted'::text, new_audit_event_id;
END;
$function$;

CREATE FUNCTION memory_private.record_answer_binding(
  p_operation_id uuid,
  p_response_id uuid,
  p_thread_id uuid,
  p_query_sha256 text,
  p_policy_sha256 text,
  p_allowed_predicates text[],
  p_domains text[],
  p_intents text[],
  p_max_records integer,
  p_policy_revision integer,
  p_renderer_sha256 text,
  p_prompt_sha256 text,
  p_explicit_recall boolean,
  p_selected_claim_ids uuid[],
  p_injected_claim_ids uuid[],
  p_outcome text,
  p_expected_selection_manifest_sha256 text,
  p_expected_injection_manifest_sha256 text,
  p_memory_block text,
  p_outbound_request text
)
RETURNS TABLE(
  binding_id uuid,
  selection_manifest_sha256 text,
  injection_manifest_sha256 text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path TO pg_catalog
AS $function$
DECLARE
  actor uuid;
  new_binding_id uuid;
  selection_hash text;
  injection_hash text;
  event_hash text;
  authoritative_selected_claim_ids uuid[] := ARRAY[]::uuid[];
  selected_revision_ids uuid[] := ARRAY[]::uuid[];
  selected_revision_sha256s text[] := ARRAY[]::text[];
  authoritative_injected_claim_ids uuid[] := ARRAY[]::uuid[];
  authoritative_injected_revision_ids uuid[] := ARRAY[]::uuid[];
  authoritative_injected_revision_sha256s text[] := ARRAY[]::text[];
  authoritative_memory_block text;
  candidate_memory_block text;
  rendered_record text;
  injection_prefix_closed boolean := false;
  selected_row record;
  existing memory.answer_binding%ROWTYPE;
  existing_count integer;
  memory_block_hash text;
  memory_block_bytes integer;
  escaped_memory_segment text;
  escaped_memory_segment_hash text;
  escaped_segment_start integer;
  escaped_segment_end integer;
  escaped_segment_occurrences integer;
  escaped_segment_character_position integer;
  outbound_request_hash text;
  authorization_at timestamptz;
BEGIN
  IF session_user <> 'governed_memory_api' THEN
    RAISE EXCEPTION 'api role required' USING ERRCODE = '42501';
  END IF;
  actor := memory_private.current_owner_id();
  IF actor IS NULL OR p_operation_id IS NULL OR p_response_id IS NULL
     OR p_thread_id IS NULL
     OR p_query_sha256 IS NULL
     OR p_query_sha256 !~ '^[0-9a-f]{64}$'
     OR p_policy_sha256 IS NULL
     OR p_policy_sha256 !~ '^[0-9a-f]{64}$'
     OR p_allowed_predicates IS NULL
     OR p_domains IS NULL OR p_intents IS NULL
     OR pg_catalog.cardinality(p_allowed_predicates) NOT BETWEEN 1 AND 8
     OR pg_catalog.cardinality(p_domains) NOT BETWEEN 0 AND 16
     OR pg_catalog.cardinality(p_intents) NOT BETWEEN 0 AND 16
     OR pg_catalog.array_position(p_allowed_predicates, NULL) IS NOT NULL
     OR pg_catalog.array_position(p_domains, NULL) IS NOT NULL
     OR pg_catalog.array_position(p_intents, NULL) IS NOT NULL
     OR p_allowed_predicates IS DISTINCT FROM ARRAY(
       SELECT DISTINCT value
       FROM pg_catalog.unnest(p_allowed_predicates) AS item(value)
       ORDER BY value
     )
     OR p_domains IS DISTINCT FROM ARRAY(
       SELECT DISTINCT value
       FROM pg_catalog.unnest(p_domains) AS item(value)
       ORDER BY value
     )
     OR p_intents IS DISTINCT FROM ARRAY(
       SELECT DISTINCT value
       FROM pg_catalog.unnest(p_intents) AS item(value)
       ORDER BY value
     )
     OR EXISTS (
       SELECT 1 FROM pg_catalog.unnest(p_allowed_predicates) AS item(value)
       WHERE item.value !~ '^[a-z][a-z0-9_.:-]{0,127}$'
     )
     OR EXISTS (
       SELECT 1
       FROM pg_catalog.unnest(p_domains || p_intents) AS item(value)
       WHERE item.value !~ '^[a-z][a-z0-9_.:-]{0,127}$'
     )
     OR p_max_records IS NULL OR p_max_records NOT BETWEEN 1 AND 8
     OR p_policy_revision IS NULL OR p_policy_revision <> 1
     OR p_explicit_recall IS NULL
     OR p_policy_sha256 <> memory_private.retrieval_policy_sha256(
       p_explicit_recall, p_allowed_predicates, p_domains, p_intents,
       p_max_records, p_policy_revision
     )
     OR p_renderer_sha256 IS NULL
     OR p_renderer_sha256 !~ '^[0-9a-f]{64}$'
     OR p_renderer_sha256 <> memory_private.answer_renderer_sha256()
     OR p_prompt_sha256 IS NULL
     OR p_prompt_sha256 !~ '^[0-9a-f]{64}$'
     OR p_expected_selection_manifest_sha256 IS NULL
     OR p_expected_selection_manifest_sha256 !~ '^[0-9a-f]{64}$'
     OR (
       p_expected_injection_manifest_sha256 IS NOT NULL
       AND p_expected_injection_manifest_sha256 !~ '^[0-9a-f]{64}$'
     )
     OR p_outcome IS NULL
     OR p_outcome NOT IN ('exposed', 'no_memory_selected')
     OR p_selected_claim_ids IS NULL OR p_injected_claim_ids IS NULL
     OR pg_catalog.cardinality(p_selected_claim_ids) NOT BETWEEN 0 AND 8
     OR pg_catalog.cardinality(p_selected_claim_ids) > p_max_records
     OR pg_catalog.cardinality(p_injected_claim_ids) NOT BETWEEN 0 AND 8
     OR pg_catalog.array_position(p_selected_claim_ids, NULL) IS NOT NULL
     OR pg_catalog.array_position(p_injected_claim_ids, NULL) IS NOT NULL
     OR (
       pg_catalog.cardinality(p_selected_claim_ids) > 0
       AND pg_catalog.array_lower(p_selected_claim_ids, 1) <> 1
     )
     OR (
       pg_catalog.cardinality(p_injected_claim_ids) > 0
       AND pg_catalog.array_lower(p_injected_claim_ids, 1) <> 1
     ) THEN
    RAISE EXCEPTION 'invalid answer binding input' USING ERRCODE = '22023';
  END IF;
  IF (p_outcome = 'exposed' AND (
        pg_catalog.cardinality(p_selected_claim_ids) = 0
        OR pg_catalog.cardinality(p_injected_claim_ids) = 0
        OR p_memory_block IS NULL
        OR pg_catalog.octet_length(p_memory_block) NOT BETWEEN 1 AND 32768
        OR p_memory_block <> normalize(p_memory_block, NFC)
        OR p_outbound_request IS NULL
        OR pg_catalog.octet_length(p_outbound_request)
             NOT BETWEEN 1 AND 1048576
        OR p_expected_injection_manifest_sha256 IS NULL
      )) OR (p_outcome = 'no_memory_selected' AND (
        pg_catalog.cardinality(p_selected_claim_ids) <> 0
        OR pg_catalog.cardinality(p_injected_claim_ids) <> 0
      )) OR (p_outcome <> 'exposed' AND (
        p_memory_block IS NOT NULL
        OR p_outbound_request IS NOT NULL
        OR p_expected_injection_manifest_sha256 IS NOT NULL
      )) OR pg_catalog.cardinality(p_selected_claim_ids) <> (
        SELECT pg_catalog.count(DISTINCT value)
        FROM pg_catalog.unnest(p_selected_claim_ids) AS selected(value)
      ) OR pg_catalog.cardinality(p_injected_claim_ids) <> (
        SELECT pg_catalog.count(DISTINCT value)
        FROM pg_catalog.unnest(p_injected_claim_ids) AS injected(value)
      ) OR p_injected_claim_ids IS DISTINCT FROM p_selected_claim_ids[
        1:pg_catalog.cardinality(p_injected_claim_ids)
      ] THEN
    RAISE EXCEPTION 'answer binding outcome shape is invalid'
      USING ERRCODE = '22023';
  END IF;

  IF p_outcome = 'exposed' THEN
    memory_block_bytes := pg_catalog.octet_length(
      pg_catalog.convert_to(p_memory_block, 'UTF8')
    );
    memory_block_hash := pg_catalog.encode(pg_catalog.sha256(
      pg_catalog.convert_to(p_memory_block, 'UTF8')
    ), 'hex');
    escaped_memory_segment := pg_catalog.to_jsonb(p_memory_block)::text;
    escaped_memory_segment_hash := pg_catalog.encode(pg_catalog.sha256(
      pg_catalog.convert_to(escaped_memory_segment, 'UTF8')
    ), 'hex');
    escaped_segment_occurrences := (
      pg_catalog.length(p_outbound_request)
        - pg_catalog.length(pg_catalog.replace(
            p_outbound_request, escaped_memory_segment, ''
          ))
    ) / pg_catalog.length(escaped_memory_segment);
    IF escaped_segment_occurrences <> 1 THEN
      RAISE EXCEPTION
        'escaped Memory segment must occur exactly once in outbound request'
        USING ERRCODE = '22023';
    END IF;
    escaped_segment_character_position := pg_catalog.strpos(
      p_outbound_request, escaped_memory_segment
    );
    escaped_segment_start := pg_catalog.octet_length(pg_catalog.convert_to(
      substring(
        p_outbound_request FROM 1
        FOR escaped_segment_character_position - 1
      ),
      'UTF8'
    ));
    escaped_segment_end := escaped_segment_start
      + pg_catalog.octet_length(pg_catalog.convert_to(
          escaped_memory_segment, 'UTF8'
        ));
    outbound_request_hash := pg_catalog.encode(pg_catalog.sha256(
      pg_catalog.convert_to(p_outbound_request, 'UTF8')
    ), 'hex');
  END IF;

  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      actor::text || '|operation|' || p_operation_id::text,
      0
    )
  );
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      actor::text || '|answer_binding_response|' || p_response_id::text,
      0
    )
  );
  SELECT pg_catalog.count(*)::integer INTO existing_count
  FROM memory.answer_binding AS value
  WHERE value.owner_user_id = actor
    AND (
      value.operation_id = p_operation_id
      OR value.response_id = p_response_id
    );
  IF existing_count > 1 THEN
    RAISE EXCEPTION 'answer operation and response identify different receipts'
      USING ERRCODE = '23514';
  ELSIF existing_count = 1 THEN
    SELECT value.* INTO STRICT existing
    FROM memory.answer_binding AS value
    WHERE value.owner_user_id = actor
      AND (
        value.operation_id = p_operation_id
        OR value.response_id = p_response_id
      );
    IF existing.operation_id <> p_operation_id
       OR existing.response_id <> p_response_id
       OR existing.thread_id <> p_thread_id
       OR existing.query_sha256 <> p_query_sha256
       OR existing.policy_sha256 <> p_policy_sha256
       OR existing.renderer_sha256 <> p_renderer_sha256
       OR existing.prompt_sha256 <> p_prompt_sha256
       OR existing.explicit_recall IS DISTINCT FROM p_explicit_recall
       OR existing.allowed_predicates
            IS DISTINCT FROM p_allowed_predicates
       OR existing.policy_domains IS DISTINCT FROM p_domains
       OR existing.policy_intents IS DISTINCT FROM p_intents
       OR existing.policy_max_records <> p_max_records
       OR existing.policy_revision <> p_policy_revision
       OR existing.selected_claim_ids <> p_selected_claim_ids
       OR existing.injected_claim_ids <> p_injected_claim_ids
       OR existing.outcome <> p_outcome
       OR existing.selection_manifest_sha256
            <> p_expected_selection_manifest_sha256
       OR existing.injection_manifest_sha256
            IS DISTINCT FROM p_expected_injection_manifest_sha256
       OR existing.memory_block_sha256
            IS DISTINCT FROM memory_block_hash
       OR existing.memory_block_utf8_bytes
            IS DISTINCT FROM memory_block_bytes
       OR existing.escaped_memory_segment_sha256
            IS DISTINCT FROM escaped_memory_segment_hash
       OR existing.escaped_segment_start_utf8
            IS DISTINCT FROM escaped_segment_start
       OR existing.escaped_segment_end_utf8
            IS DISTINCT FROM escaped_segment_end
       OR existing.escaped_segment_occurrence_count
            IS DISTINCT FROM escaped_segment_occurrences
       OR existing.outbound_request_sha256
            IS DISTINCT FROM outbound_request_hash THEN
      RAISE EXCEPTION 'answer binding replay drifted'
        USING ERRCODE = '23514';
    END IF;
    RETURN QUERY SELECT existing.binding_id,
      existing.selection_manifest_sha256,
      existing.injection_manifest_sha256;
    RETURN;
  END IF;
  IF memory_private.operation_id_conflicts(
       actor, p_operation_id, NULL::uuid, NULL::uuid, NULL::uuid,
       ARRAY[]::text[]
     ) THEN
    RAISE EXCEPTION 'operation id is reserved by another mutation'
      USING ERRCODE = '23514';
  END IF;

  authorization_at := pg_catalog.transaction_timestamp();
  FOR selected_row IN
    SELECT input.ordinality, claim.claim_id, revision.revision_id,
           revision.revision_sha256, revision.epistemic_state,
           revision.predicate, revision.subject_display_name,
           revision.subject_entity_type, revision.object_kind,
           revision.object_display_name, revision.object_entity_type,
           CASE WHEN revision.object_kind = 'literal'
             THEN revision.object_literal #>> '{}' ELSE NULL::text END
             AS object_literal_text
    FROM pg_catalog.unnest(p_selected_claim_ids) WITH ORDINALITY
      AS input(claim_id, ordinality)
    JOIN memory.claim AS claim
      ON claim.owner_user_id = actor
     AND claim.claim_id = input.claim_id
     AND claim.lifecycle_state = 'active'
    JOIN memory.claim_revision AS revision
      ON revision.owner_user_id = claim.owner_user_id
     AND revision.claim_id = claim.claim_id
     AND revision.revision_id = claim.current_revision_id
     AND revision.revision_number = claim.current_revision_number
     AND revision.projectable
     AND revision.surface <> 'never'
     AND revision.predicate = ANY(p_allowed_predicates)
     AND (NOT revision.requires_explicit OR p_explicit_recall)
     AND (
       pg_catalog.cardinality(p_domains) = 0
       OR pg_catalog.cardinality(revision.domains) = 0
       OR revision.domains && p_domains
     )
     AND (
       pg_catalog.cardinality(p_intents) = 0
       OR pg_catalog.cardinality(revision.intents) = 0
       OR revision.intents && p_intents
     )
     AND (
       revision.valid_from IS NULL OR revision.valid_from <= authorization_at
     )
     AND (
       revision.valid_to IS NULL OR revision.valid_to > authorization_at
     )
     AND claim.current_state_sha256 = memory_private.claim_state_sha256(
       claim.owner_user_id, claim.claim_id, claim.semantic_key_sha256,
       claim.claim_identity_sha256, claim.lifecycle_state, true,
       claim.current_revision_id, claim.current_revision_number,
       revision.revision_sha256, claim.projection_sequence
     )
    JOIN memory.predicate_catalog AS predicate_catalog
      ON predicate_catalog.catalog_sha256
           = revision.predicate_catalog_sha256
     AND predicate_catalog.predicate = revision.predicate
     AND predicate_catalog.active
     AND revision.subject_entity_type
           = ANY(predicate_catalog.subject_kinds)
     AND revision.object_kind = ANY(predicate_catalog.object_kinds)
     AND revision.sensitivity = ANY(predicate_catalog.sensitivities)
     AND revision.epistemic_state
           = ANY(predicate_catalog.epistemic_statuses)
    JOIN memory.entity AS subject_entity
      ON subject_entity.owner_user_id = revision.owner_user_id
     AND subject_entity.entity_id = revision.subject_entity_id
     AND subject_entity.entity_type = revision.subject_entity_type
     AND subject_entity.entity_key = revision.subject_entity_key
    LEFT JOIN memory.entity AS object_entity
      ON object_entity.owner_user_id = revision.owner_user_id
     AND object_entity.entity_id = revision.object_entity_id
    WHERE (
      revision.object_kind = 'literal'
      AND object_entity.entity_id IS NULL
    ) OR (
      revision.object_kind = 'entity'
      AND object_entity.entity_id IS NOT NULL
      AND object_entity.entity_type = revision.object_entity_type
      AND object_entity.entity_key = revision.object_entity_key
    )
    ORDER BY input.ordinality
  LOOP
    authoritative_selected_claim_ids := pg_catalog.array_append(
      authoritative_selected_claim_ids, selected_row.claim_id
    );
    selected_revision_ids := pg_catalog.array_append(
      selected_revision_ids, selected_row.revision_id
    );
    selected_revision_sha256s := pg_catalog.array_append(
      selected_revision_sha256s, selected_row.revision_sha256
    );
    rendered_record := memory_private.render_answer_memory_record(
      selected_row.epistemic_state, selected_row.predicate,
      selected_row.subject_display_name, selected_row.subject_entity_type,
      selected_row.object_kind, selected_row.object_display_name,
      selected_row.object_entity_type, selected_row.object_literal_text
    );
    IF rendered_record IS NULL
       OR rendered_record <> normalize(rendered_record, NFC) THEN
      RAISE EXCEPTION 'answer Memory rendering failed'
        USING ERRCODE = '23514';
    END IF;
    IF NOT injection_prefix_closed THEN
      candidate_memory_block := CASE
        WHEN authoritative_memory_block IS NULL THEN rendered_record
        ELSE authoritative_memory_block || E'\n' || rendered_record
      END;
      IF pg_catalog.cardinality(authoritative_injected_claim_ids) < 8
         AND pg_catalog.octet_length(
           pg_catalog.convert_to(candidate_memory_block, 'UTF8')
         ) <= 32768 THEN
        authoritative_memory_block := candidate_memory_block;
        authoritative_injected_claim_ids := pg_catalog.array_append(
          authoritative_injected_claim_ids, selected_row.claim_id
        );
        authoritative_injected_revision_ids := pg_catalog.array_append(
          authoritative_injected_revision_ids, selected_row.revision_id
        );
        authoritative_injected_revision_sha256s := pg_catalog.array_append(
          authoritative_injected_revision_sha256s,
          selected_row.revision_sha256
        );
      ELSE
        injection_prefix_closed := true;
      END IF;
    END IF;
  END LOOP;
  IF pg_catalog.cardinality(authoritative_selected_claim_ids)
       <> pg_catalog.cardinality(p_selected_claim_ids) THEN
    RAISE EXCEPTION 'answer selection revalidation failed'
      USING ERRCODE = '40001';
  END IF;
  IF authoritative_selected_claim_ids IS DISTINCT FROM p_selected_claim_ids
     OR authoritative_injected_claim_ids IS DISTINCT FROM p_injected_claim_ids
     OR (
       p_outcome = 'no_memory_selected'
       AND pg_catalog.cardinality(authoritative_selected_claim_ids) <> 0
     )
     OR (
       p_outcome = 'exposed'
       AND (
         pg_catalog.cardinality(authoritative_injected_claim_ids) = 0
         OR p_memory_block IS DISTINCT FROM authoritative_memory_block
       )
     ) THEN
    RAISE EXCEPTION 'authoritative answer injection mismatch'
      USING ERRCODE = '40001';
  END IF;
  IF p_outcome = 'exposed' AND EXISTS (
    SELECT 1
    FROM (
      SELECT claim_id::text AS token
      FROM pg_catalog.unnest(p_selected_claim_ids) AS claim(claim_id)
      UNION ALL
      SELECT revision_id::text
      FROM pg_catalog.unnest(selected_revision_ids) AS revision(revision_id)
      UNION ALL
      SELECT revision_sha256
      FROM pg_catalog.unnest(selected_revision_sha256s)
        AS revision_hash(revision_sha256)
    ) AS internal_identifier
    WHERE pg_catalog.strpos(
            authoritative_memory_block, internal_identifier.token
          ) > 0
       OR pg_catalog.strpos(
            p_outbound_request, internal_identifier.token
          ) > 0
  ) THEN
    RAISE EXCEPTION 'provider-visible request contains internal identity'
      USING ERRCODE = '22023';
  END IF;

  selection_hash := memory_private.answer_selection_manifest_sha256(
    actor, p_response_id, p_query_sha256, p_policy_sha256,
    p_renderer_sha256, p_prompt_sha256, p_explicit_recall,
    authoritative_selected_claim_ids,
    selected_revision_ids, selected_revision_sha256s,
    p_outcome
  );
  IF selection_hash <> p_expected_selection_manifest_sha256 THEN
    RAISE EXCEPTION 'answer selection manifest mismatch'
      USING ERRCODE = '40001';
  END IF;
  IF p_outcome = 'exposed' THEN
    injection_hash := memory_private.answer_injection_manifest_sha256(
      selection_hash, authoritative_injected_claim_ids,
      authoritative_injected_revision_ids,
      authoritative_injected_revision_sha256s,
      memory_block_hash, memory_block_bytes,
      escaped_memory_segment_hash, escaped_segment_start,
      escaped_segment_end, escaped_segment_occurrences,
      outbound_request_hash
    );
    IF injection_hash <> p_expected_injection_manifest_sha256 THEN
      RAISE EXCEPTION 'answer injection manifest mismatch'
        USING ERRCODE = '40001';
    END IF;
  END IF;

  new_binding_id := pg_catalog.gen_random_uuid();
  INSERT INTO memory.answer_binding(
    binding_id, owner_user_id, operation_id, response_id, thread_id,
    query_sha256, policy_sha256, renderer_sha256, prompt_sha256,
    explicit_recall, allowed_predicates, policy_domains,
    policy_intents, policy_max_records, policy_revision,
    selection_manifest_sha256, selected_claim_ids, selected_revision_ids,
    selected_revision_sha256s, injected_claim_ids, injected_revision_ids,
    injected_revision_sha256s, memory_block_sha256,
    memory_block_utf8_bytes, escaped_memory_segment_sha256,
    escaped_segment_start_utf8, escaped_segment_end_utf8,
    escaped_segment_occurrence_count,
    outbound_request_sha256, injection_manifest_sha256, dispatched_at,
    outcome, expires_at, created_at
  ) VALUES (
    new_binding_id, actor, p_operation_id, p_response_id, p_thread_id,
    p_query_sha256, p_policy_sha256, p_renderer_sha256, p_prompt_sha256,
    p_explicit_recall, p_allowed_predicates, p_domains, p_intents,
    p_max_records, p_policy_revision,
    selection_hash, authoritative_selected_claim_ids, selected_revision_ids,
    selected_revision_sha256s, authoritative_injected_claim_ids,
    authoritative_injected_revision_ids,
    authoritative_injected_revision_sha256s,
    memory_block_hash, memory_block_bytes,
    escaped_memory_segment_hash, escaped_segment_start,
    escaped_segment_end, escaped_segment_occurrences,
    outbound_request_hash,
    injection_hash,
    CASE WHEN p_outcome = 'exposed'
      THEN authorization_at ELSE NULL::timestamptz END,
    p_outcome, authorization_at + interval '90 days', authorization_at
  );
  event_hash := pg_catalog.encode(pg_catalog.sha256(
    pg_catalog.convert_to(
      actor::text || '|' || p_operation_id::text || '|answer_binding|'
      || CASE WHEN p_explicit_recall THEN 'true' ELSE 'false' END || '|'
      || selection_hash || '|' || COALESCE(injection_hash, '-'), 'UTF8'
    )
  ), 'hex');
  INSERT INTO memory.audit_event(
    owner_user_id, operation_id, actor_kind, actor_user_id,
    object_type, object_id, transition_code, new_state_sha256,
    reason_code, event_sha256
  ) VALUES (
    actor, p_operation_id, 'owner', actor, 'answer_binding',
    new_binding_id, 'answer_binding_recorded',
    COALESCE(injection_hash, selection_hash),
    p_outcome, event_hash
  );
  RETURN QUERY SELECT new_binding_id, selection_hash, injection_hash;
END;
$function$;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA memory_private
  FROM PUBLIC, governed_memory_api, governed_memory_worker;

GRANT EXECUTE ON FUNCTION
  memory_private.current_owner_id(),
  memory_private.list_proposals(integer,timestamptz,uuid),
  memory_private.review_proposal(uuid,uuid,text,text,text,text,text,text,text[]),
  memory_private.read_claim_candidates(uuid[]),
  memory_private.list_claims(uuid,integer,timestamptz,uuid),
  memory_private.correct_claim(uuid,uuid,text,text,text,jsonb),
  memory_private.retract_claim(uuid,uuid,text,text),
  memory_private.request_claim_deletion(uuid,uuid,text,text),
  memory_private.record_answer_binding(
    uuid,uuid,uuid,text,text,text[],text[],text[],integer,integer,text,text,
    boolean,uuid[],uuid[],text,text,text,text,text
  ),
  memory_private.read_operation(uuid),
  memory_private.read_status()
TO governed_memory_api;

GRANT EXECUTE ON FUNCTION
  memory_private.next_worker_lane(),
  memory_private.record_selected_evidence(
    uuid,uuid,text,uuid,uuid,uuid,text,text,text,integer,integer,uuid,text,
    timestamptz,text,text,text
  ),
  memory_private.read_ingest_receipt(uuid,uuid,text),
  memory_private.lease_extraction_jobs(
    text,integer,integer,text,text,text,text,text,text,integer,integer
  ),
  memory_private.mark_provider_call_dispatched(
    uuid,uuid,uuid,text,text,text,text
  ),
  memory_private.complete_extraction(
    uuid,uuid,uuid,text,text,text,integer,integer,text,jsonb
  ),
  memory_private.expire_proposals(integer),
  memory_private.purge_terminal_proposals(integer),
  memory_private.purge_expired_answer_bindings(integer),
  memory_private.redact_expired_evidence_excerpts(integer),
  memory_private.read_projection_rebuild_batch(uuid,uuid,integer),
  memory_private.lease_projection_jobs(text,integer,integer),
  memory_private.finish_projection_job(
    uuid,uuid,text,text,text,text,text,text
  )
TO governed_memory_worker;
GRANT EXECUTE ON FUNCTION memory_private.finalize_claim_deletion(
  uuid,uuid,uuid,text,uuid,uuid,text,integer,text,text,
  timestamptz,timestamptz,text,text
) TO governed_memory_worker;

DO $postflight$
DECLARE
  relation_name text;
  forbidden_role text;
BEGIN
  IF (
    SELECT pg_catalog.count(*)
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'memory' AND relation.relkind = 'r'
  ) <> 13 THEN
    RAISE EXCEPTION 'foundation must contain exactly 13 tables';
  END IF;

  FOREACH relation_name IN ARRAY ARRAY[
    'evidence', 'extraction_job', 'provider_call', 'proposal', 'entity',
    'claim', 'claim_revision', 'claim_evidence', 'projection_outbox',
    'answer_binding', 'audit_event', 'claim_deletion_receipt'
  ] LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_class AS relation
      JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname = 'memory'
        AND relation.relname = relation_name
        AND relation.relrowsecurity
        AND relation.relforcerowsecurity
        AND relation.relowner = 'governed_memory_owner'::regrole
    ) THEN
      RAISE EXCEPTION 'owner table % lacks forced RLS or exact owner',
        relation_name;
    END IF;
    IF pg_catalog.has_table_privilege(
      'governed_memory_api', 'memory.' || relation_name, 'SELECT'
    ) OR pg_catalog.has_table_privilege(
      'governed_memory_api', 'memory.' || relation_name, 'INSERT'
    ) OR pg_catalog.has_table_privilege(
      'governed_memory_api', 'memory.' || relation_name, 'UPDATE'
    ) OR pg_catalog.has_table_privilege(
      'governed_memory_api', 'memory.' || relation_name, 'DELETE'
    ) OR pg_catalog.has_table_privilege(
      'governed_memory_worker', 'memory.' || relation_name, 'SELECT'
    ) OR pg_catalog.has_table_privilege(
      'governed_memory_worker', 'memory.' || relation_name, 'INSERT'
    ) OR pg_catalog.has_table_privilege(
      'governed_memory_worker', 'memory.' || relation_name, 'UPDATE'
    ) OR pg_catalog.has_table_privilege(
      'governed_memory_worker', 'memory.' || relation_name, 'DELETE'
    ) THEN
      RAISE EXCEPTION 'runtime role has direct table authority on %',
        relation_name;
    END IF;
  END LOOP;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'memory_private'
      AND relation.relname = 'worker_lane_sequence'
      AND relation.relkind = 'S'
      AND relation.relowner = 'governed_memory_owner'::regrole
  ) OR pg_catalog.has_sequence_privilege(
    'governed_memory_api',
    'memory_private.worker_lane_sequence',
    'USAGE'
  ) OR pg_catalog.has_sequence_privilege(
    'governed_memory_worker',
    'memory_private.worker_lane_sequence',
    'USAGE'
  ) OR NOT pg_catalog.has_function_privilege(
    'governed_memory_worker',
    'memory_private.next_worker_lane()',
    'EXECUTE'
  ) OR pg_catalog.has_function_privilege(
    'governed_memory_api',
    'memory_private.next_worker_lane()',
    'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'worker lane scheduler authority differs';
  END IF;

  FOREACH forbidden_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    IF pg_catalog.to_regrole(forbidden_role) IS NOT NULL AND EXISTS (
      SELECT 1
      FROM pg_catalog.pg_class AS relation
      JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname IN ('memory', 'memory_private')
        AND (
          pg_catalog.has_table_privilege(forbidden_role, relation.oid, 'SELECT')
          OR pg_catalog.has_table_privilege(forbidden_role, relation.oid, 'INSERT')
          OR pg_catalog.has_table_privilege(forbidden_role, relation.oid, 'UPDATE')
          OR pg_catalog.has_table_privilege(forbidden_role, relation.oid, 'DELETE')
          OR pg_catalog.has_any_column_privilege(
            forbidden_role, relation.oid, 'SELECT'
          )
          OR pg_catalog.has_any_column_privilege(
            forbidden_role, relation.oid, 'INSERT'
          )
          OR pg_catalog.has_any_column_privilege(
            forbidden_role, relation.oid, 'UPDATE'
          )
        )
    ) THEN
      RAISE EXCEPTION 'forbidden role % can access successor relations',
        forbidden_role;
    END IF;
  END LOOP;
END;
$postflight$;
