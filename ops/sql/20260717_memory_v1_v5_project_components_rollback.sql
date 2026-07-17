BEGIN;

DO $preflight$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'V5 project component rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.project_component_registration_event_v5 LIMIT 1
  ) OR EXISTS (
    SELECT 1 FROM memory.project_component_alias_v5 LIMIT 1
  ) OR EXISTS (
    SELECT 1 FROM memory.project_component_v5 LIMIT 1
  ) THEN
    RAISE EXCEPTION 'refusing to remove non-empty V5 project component tables';
  END IF;
END
$preflight$;

CREATE OR REPLACE FUNCTION memory.v5_project_scope_valid(value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
  key_count integer;
  state text;
  project_key text;
  binding_source text;
BEGIN
  IF jsonb_typeof(value) <> 'object' THEN
    RETURN false;
  END IF;
  SELECT count(*) INTO key_count FROM jsonb_object_keys(value);
  state := value->>'state';
  project_key := value->>'project_key';
  binding_source := value->>'binding_source';
  RETURN key_count = 3
    AND value ?& ARRAY['state','project_key','binding_source']
    AND state IN ('not_applicable','resolved','unresolved')
    AND binding_source IN (
      'not_applicable','explicit_source_text',
      'trusted_thread_binding','unresolved'
    )
    AND (
      (state = 'not_applicable'
       AND value->'project_key' = 'null'::jsonb
       AND binding_source = 'not_applicable')
      OR
      (state = 'unresolved'
       AND value->'project_key' = 'null'::jsonb
       AND binding_source = 'unresolved')
      OR
      (state = 'resolved'
       AND project_key IS NOT NULL
       AND btrim(project_key) <> ''
       AND length(project_key) <= 500
       AND binding_source IN ('explicit_source_text','trusted_thread_binding'))
    );
END
$function$;

DROP FUNCTION IF EXISTS memory.read_owner_project_components_v5(uuid);
DROP FUNCTION IF EXISTS memory.apply_owner_project_component_v5(
  uuid,uuid,text,text,uuid,text[],jsonb
);
DROP FUNCTION IF EXISTS memory.normalize_project_component_alias_v5(text);

DROP TABLE IF EXISTS memory.project_component_registration_event_v5;
DROP TABLE IF EXISTS memory.project_component_alias_v5;
DROP TABLE IF EXISTS memory.project_component_v5;

COMMIT;
