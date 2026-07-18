BEGIN;

DO $prerequisite$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'V5 project shadow read API migration requires sage';
  END IF;
  IF to_regrole('memory_v5_reader') IS NULL
     OR to_regrole('memory_v5_extraction_maintainer') IS NULL
     OR to_regprocedure('memory.require_v5_reader_context()') IS NULL
     OR to_regclass('memory.current_project_thread_binding_v5') IS NULL
     OR to_regclass('memory.project_component_v5') IS NULL
     OR to_regclass('memory.project_knowledge_head_v5') IS NULL
     OR to_regclass('memory.project_knowledge_revision_v5') IS NULL
     OR to_regclass('memory.project_knowledge_revision_observation') IS NULL
     OR to_regclass('memory.projection_apply_event') IS NULL THEN
    RAISE EXCEPTION 'V5 project shadow read API prerequisites are missing';
  END IF;
END
$prerequisite$;

CREATE TABLE IF NOT EXISTS memory.project_thread_component_binding_event_v5 (
  binding_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  thread_id uuid NOT NULL,
  project_id uuid NOT NULL,
  component_id uuid NOT NULL,
  component_key text NOT NULL,
  action text NOT NULL,
  reason_code text NOT NULL,
  source_project_binding_event_id uuid NOT NULL,
  source_evidence_id uuid NOT NULL,
  source_evidence_content_sha256 text NOT NULL,
  actor_user_id uuid NOT NULL,
  invoked_by_role text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_user_id,binding_event_id),
  UNIQUE (owner_user_id,operation_id),
  FOREIGN KEY (owner_user_id,thread_id)
    REFERENCES public.threads(owner_user_id,id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,project_id)
    REFERENCES memory.project_space(owner_user_id,project_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,project_id,component_id)
    REFERENCES memory.project_component_v5(
      owner_user_id,project_id,component_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,source_project_binding_event_id)
    REFERENCES memory.project_thread_binding_event(
      owner_user_id,binding_event_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id,source_evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  CHECK (
    component_key ~ '^[a-z][a-z0-9-]{0,99}$'
    AND component_key NOT LIKE '%--%'
    AND right(component_key,1)<>'-'
  ),
  CHECK (action IN ('bind','unbind')),
  CHECK (reason_code ~ '^[a-z][a-z0-9_]{1,99}$'),
  CHECK (source_evidence_content_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (actor_user_id=owner_user_id),
  CHECK (btrim(invoked_by_role)<>'' AND length(invoked_by_role)<=200)
);

ALTER TABLE memory.project_thread_component_binding_event_v5 OWNER TO sage;

CREATE INDEX IF NOT EXISTS project_thread_component_binding_current_idx
  ON memory.project_thread_component_binding_event_v5(
    owner_user_id,thread_id,project_id,component_id,
    created_at DESC,binding_event_id DESC
  );

CREATE OR REPLACE FUNCTION memory.guard_project_component_binding_append_only_v5()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
BEGIN
  RAISE EXCEPTION 'memory.project_thread_component_binding_event_v5 is append-only'
    USING ERRCODE='42501';
END
$function$;

ALTER FUNCTION memory.guard_project_component_binding_append_only_v5()
  OWNER TO memory_v5_extraction_maintainer;
REVOKE ALL ON FUNCTION memory.guard_project_component_binding_append_only_v5()
  FROM PUBLIC,brains_app,memory_v5_reader;
GRANT EXECUTE ON FUNCTION memory.guard_project_component_binding_append_only_v5()
  TO memory_v5_extraction_maintainer;

DROP TRIGGER IF EXISTS project_thread_component_binding_append_only_guard
  ON memory.project_thread_component_binding_event_v5;
CREATE TRIGGER project_thread_component_binding_append_only_guard
BEFORE UPDATE OR DELETE ON memory.project_thread_component_binding_event_v5
FOR EACH ROW
EXECUTE FUNCTION memory.guard_project_component_binding_append_only_v5();

ALTER TABLE memory.project_thread_component_binding_event_v5
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.project_thread_component_binding_event_v5
  FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS owner_isolation
  ON memory.project_thread_component_binding_event_v5;
CREATE POLICY owner_isolation
  ON memory.project_thread_component_binding_event_v5
  USING (owner_user_id=(SELECT memory.current_actor_user_id()))
  WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()));

CREATE OR REPLACE VIEW memory.current_project_thread_component_binding_v5
WITH (security_invoker=true,security_barrier=true)
AS
SELECT
  latest.owner_user_id,
  latest.thread_id,
  latest.project_id,
  latest.component_id,
  latest.component_key,
  latest.binding_event_id,
  latest.source_project_binding_event_id,
  latest.source_evidence_id,
  latest.created_at AS bound_at
FROM (
  SELECT DISTINCT ON (
    event.owner_user_id,event.thread_id,event.project_id,event.component_id
  )
    event.owner_user_id,
    event.thread_id,
    event.project_id,
    event.component_id,
    event.component_key,
    event.binding_event_id,
    event.source_project_binding_event_id,
    event.source_evidence_id,
    event.action,
    event.created_at
  FROM memory.project_thread_component_binding_event_v5 AS event
  ORDER BY
    event.owner_user_id,event.thread_id,event.project_id,event.component_id,
    event.created_at DESC,event.binding_event_id DESC
) AS latest
WHERE latest.action='bind';

CREATE OR REPLACE FUNCTION memory.apply_owner_project_thread_component_binding_v5(
  p_operation_id uuid,
  p_thread_id uuid,
  p_project_id uuid,
  p_component_id uuid,
  p_source_project_binding_event_id uuid,
  p_source_evidence_id uuid,
  p_expected_evidence_content_sha256 text,
  p_action text,
  p_reason_code text
)
RETURNS TABLE(
  binding_event_id uuid,
  thread_id uuid,
  project_id uuid,
  component_id uuid,
  component_key text,
  action text,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  project_binding record;
  component_record memory.project_component_v5%ROWTYPE;
  evidence_record memory.evidence%ROWTYPE;
  replayed memory.project_thread_component_binding_event_v5%ROWTYPE;
  current_binding record;
  new_event_id uuid := gen_random_uuid();
BEGIN
  IF session_user<>'brains_app'
     OR current_user<>'memory_v5_extraction_maintainer' THEN
    RAISE EXCEPTION 'component binding requires the brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor := memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'component binding requires an authenticated actor'
      USING ERRCODE='42501';
  END IF;
  IF p_operation_id IS NULL
     OR p_thread_id IS NULL
     OR p_project_id IS NULL
     OR p_component_id IS NULL
     OR p_source_project_binding_event_id IS NULL
     OR p_source_evidence_id IS NULL
     OR p_expected_evidence_content_sha256 IS NULL
     OR p_expected_evidence_content_sha256 !~ '^[0-9a-f]{64}$'
     OR p_action NOT IN ('bind','unbind')
     OR p_reason_code IS NULL
     OR p_reason_code !~ '^[a-z][a-z0-9_]{1,99}$' THEN
    RAISE EXCEPTION 'component binding inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|',actor::text,p_thread_id::text,p_component_id::text),0
  ));

  SELECT event.* INTO replayed
  FROM memory.project_thread_component_binding_event_v5 AS event
  WHERE event.owner_user_id=actor
    AND event.operation_id=p_operation_id;
  IF FOUND THEN
    IF replayed.thread_id<>p_thread_id
       OR replayed.project_id<>p_project_id
       OR replayed.component_id<>p_component_id
       OR replayed.source_project_binding_event_id
            <>p_source_project_binding_event_id
       OR replayed.source_evidence_id<>p_source_evidence_id
       OR replayed.source_evidence_content_sha256
            <>p_expected_evidence_content_sha256
       OR replayed.action<>p_action
       OR replayed.reason_code<>p_reason_code THEN
      RAISE EXCEPTION 'component binding replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      replayed.binding_event_id,replayed.thread_id,replayed.project_id,
      replayed.component_id,replayed.component_key,replayed.action,
      'replayed'::text;
    RETURN;
  END IF;

  SELECT binding.* INTO project_binding
  FROM memory.current_project_thread_binding_v5 AS binding
  WHERE binding.owner_user_id=actor
    AND binding.thread_id=p_thread_id;
  IF NOT FOUND
     OR project_binding.project_id<>p_project_id
     OR project_binding.binding_event_id<>p_source_project_binding_event_id THEN
    RAISE EXCEPTION 'current owner project binding does not match'
      USING ERRCODE='23514';
  END IF;

  SELECT component.* INTO component_record
  FROM memory.project_component_v5 AS component
  WHERE component.owner_user_id=actor
    AND component.project_id=p_project_id
    AND component.component_id=p_component_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner project component is absent'
      USING ERRCODE='23514';
  END IF;

  SELECT evidence.* INTO evidence_record
  FROM memory.evidence AS evidence
  WHERE evidence.owner_user_id=actor
    AND evidence.evidence_id=p_source_evidence_id;
  IF NOT FOUND
     OR evidence_record.status<>'active'
     OR evidence_record.source_system<>'public.chat_log'
     OR evidence_record.content_sha256<>p_expected_evidence_content_sha256
     OR evidence_record.metadata->>'thread_id'<>p_thread_id::text THEN
    RAISE EXCEPTION 'component binding evidence is invalid'
      USING ERRCODE='23514';
  END IF;

  SELECT binding.* INTO current_binding
  FROM memory.current_project_thread_component_binding_v5 AS binding
  WHERE binding.owner_user_id=actor
    AND binding.thread_id=p_thread_id
    AND binding.project_id=p_project_id
    AND binding.component_id=p_component_id;
  IF p_action='bind' AND FOUND THEN
    RETURN QUERY SELECT
      current_binding.binding_event_id,p_thread_id,p_project_id,p_component_id,
      current_binding.component_key,p_action,'already_bound'::text;
    RETURN;
  ELSIF p_action='unbind' AND NOT FOUND THEN
    RAISE EXCEPTION 'component unbind has no current binding'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.project_thread_component_binding_event_v5(
    binding_event_id,owner_user_id,operation_id,thread_id,project_id,
    component_id,component_key,action,reason_code,
    source_project_binding_event_id,source_evidence_id,
    source_evidence_content_sha256,actor_user_id,invoked_by_role
  ) VALUES (
    new_event_id,actor,p_operation_id,p_thread_id,p_project_id,
    p_component_id,component_record.component_key,p_action,p_reason_code,
    p_source_project_binding_event_id,p_source_evidence_id,
    p_expected_evidence_content_sha256,actor,session_user
  );

  RETURN QUERY SELECT
    new_event_id,p_thread_id,p_project_id,p_component_id,
    component_record.component_key,p_action,'applied'::text;
END
$function$;

ALTER FUNCTION memory.apply_owner_project_thread_component_binding_v5(
  uuid,uuid,uuid,uuid,uuid,uuid,text,text,text
) OWNER TO memory_v5_extraction_maintainer;

CREATE OR REPLACE FUNCTION memory.read_v5_shadow_project_knowledge(
  p_thread_id uuid,
  p_limit integer DEFAULT 4
)
RETURNS TABLE(
  owner_user_id uuid,
  thread_id uuid,
  project_id uuid,
  project_key text,
  component_id uuid,
  component_key text,
  knowledge_id uuid,
  knowledge_kind text,
  knowledge_key text,
  canonical_text text,
  status text,
  revision_id uuid,
  revision_number integer,
  document_state text,
  authority_level text,
  surface_policy text,
  content_sha256 text,
  effective_precision text,
  effective_instant_at timestamptz,
  effective_calendar_range daterange,
  effective_instant_range tstzrange,
  projection_review_decision text,
  projection_apply_outcome text,
  evidence_ids text[],
  observation_ids text[]
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
BEGIN
  actor := memory.require_v5_reader_context();
  IF p_thread_id IS NULL OR p_limit<1 OR p_limit>8 THEN
    RAISE EXCEPTION 'thread_id and limit are invalid'
      USING ERRCODE='22023';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM public.threads AS thread
    WHERE thread.owner_user_id=actor AND thread.id=p_thread_id
  ) THEN
    RAISE EXCEPTION 'owner thread is absent'
      USING ERRCODE='42501';
  END IF;

  RETURN QUERY
  WITH scope AS (
    SELECT
      project_binding.owner_user_id,
      project_binding.thread_id,
      project_binding.project_id,
      project_binding.project_key,
      component_binding.component_id,
      component_binding.component_key
    FROM memory.current_project_thread_binding_v5 AS project_binding
    JOIN memory.current_project_thread_component_binding_v5 AS component_binding
      ON component_binding.owner_user_id=project_binding.owner_user_id
     AND component_binding.thread_id=project_binding.thread_id
     AND component_binding.project_id=project_binding.project_id
    WHERE project_binding.owner_user_id=actor
      AND project_binding.thread_id=p_thread_id
  )
  SELECT
    head.owner_user_id,
    scope.thread_id,
    head.project_id,
    scope.project_key,
    scope.component_id,
    head.component_key,
    head.knowledge_id,
    head.knowledge_kind,
    head.knowledge_key,
    revision.canonical_text,
    head.status::text,
    revision.revision_id,
    revision.revision_number,
    revision.document_state,
    revision.authority_level,
    revision.surface_policy::text,
    revision.content_sha256,
    revision.effective_precision::text,
    revision.effective_instant_at,
    revision.effective_calendar_range,
    revision.effective_instant_range,
    applied.review_decision::text,
    applied.outcome,
    provenance.evidence_ids,
    provenance.observation_ids
  FROM scope
  JOIN memory.project_knowledge_head_v5 AS head
    ON head.owner_user_id=scope.owner_user_id
   AND head.project_id=scope.project_id
   AND head.component_key=scope.component_key
   AND head.status='active'
   AND head.current_revision_id IS NOT NULL
  JOIN memory.project_knowledge_revision_v5 AS revision
    ON revision.owner_user_id=head.owner_user_id
   AND revision.project_id=head.project_id
   AND revision.knowledge_id=head.knowledge_id
   AND revision.revision_id=head.current_revision_id
   AND revision.revision_number=head.revision_number
   AND revision.document_state IN ('working','ratified')
   AND revision.surface_policy='exact_project_scope_only'
  JOIN LATERAL (
    SELECT event.review_decision,event.outcome
    FROM memory.projection_apply_event AS event
    WHERE event.owner_user_id=head.owner_user_id
      AND event.lane='project_knowledge'
      AND event.outcome='applied'
      AND event.resulting_project_id=head.project_id
      AND event.resulting_project_revision_id=revision.revision_id
    ORDER BY event.created_at DESC,event.event_id DESC
    LIMIT 1
  ) AS applied ON TRUE
  JOIN LATERAL (
    SELECT
      array_agg(DISTINCT evidence.evidence_id::text
        ORDER BY evidence.evidence_id::text) AS evidence_ids,
      array_agg(DISTINCT observation.observation_id::text
        ORDER BY observation.observation_id::text) AS observation_ids
    FROM memory.project_knowledge_revision_observation AS link
    JOIN memory.observation AS observation
      ON observation.owner_user_id=link.owner_user_id
     AND observation.observation_id=link.observation_id
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=observation.owner_user_id
     AND evidence.evidence_id=observation.evidence_id
     AND evidence.status='active'
    WHERE link.owner_user_id=revision.owner_user_id
      AND link.project_id=revision.project_id
      AND link.revision_id=revision.revision_id
      AND link.stance='supports'
  ) AS provenance ON cardinality(provenance.evidence_ids)>0
  ORDER BY head.updated_at DESC,head.knowledge_key,head.knowledge_id
  LIMIT p_limit;
END
$function$;

ALTER FUNCTION memory.read_v5_shadow_project_knowledge(uuid,integer)
  OWNER TO memory_v5_reader;

GRANT USAGE ON SCHEMA memory TO memory_v5_extraction_maintainer,memory_v5_reader;
GRANT SELECT,INSERT ON memory.project_thread_component_binding_event_v5
  TO memory_v5_extraction_maintainer;
GRANT SELECT ON
  public.threads,
  memory.project_space,
  memory.project_component_v5,
  memory.project_thread_binding_event,
  memory.evidence
TO memory_v5_extraction_maintainer;
GRANT SELECT ON memory.current_project_thread_binding_v5
  TO memory_v5_extraction_maintainer;

GRANT SELECT ON
  public.threads,
  memory.project_space,
  memory.project_component_v5,
  memory.project_thread_binding_event,
  memory.project_thread_component_binding_event_v5,
  memory.project_knowledge_head_v5,
  memory.project_knowledge_revision_v5,
  memory.project_knowledge_revision_observation,
  memory.observation,
  memory.evidence,
  memory.projection_apply_event
TO memory_v5_reader;
GRANT SELECT ON memory.current_project_thread_binding_v5
  TO memory_v5_reader;

DO $reader_policies$
DECLARE
  relation_name text;
BEGIN
  FOREACH relation_name IN ARRAY ARRAY[
    'project_knowledge_head_v5',
    'project_knowledge_revision_v5',
    'project_knowledge_revision_observation'
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_policies
      WHERE schemaname='memory'
        AND tablename=relation_name
        AND policyname='owner_isolation_v5_project_reader'
    ) THEN
      EXECUTE format(
        'CREATE POLICY owner_isolation_v5_project_reader ON memory.%I '
        'FOR SELECT TO memory_v5_reader '
        'USING (owner_user_id=(SELECT memory.current_actor_user_id()))',
        relation_name
      );
    END IF;
  END LOOP;
END
$reader_policies$;

REVOKE ALL ON memory.project_thread_component_binding_event_v5
  FROM PUBLIC,brains_app,memory_v5_reader;
GRANT SELECT ON memory.project_thread_component_binding_event_v5
  TO memory_v5_reader;
REVOKE ALL ON memory.current_project_thread_component_binding_v5
  FROM PUBLIC,brains_app;
GRANT SELECT ON memory.current_project_thread_component_binding_v5
  TO memory_v5_extraction_maintainer,memory_v5_reader;

REVOKE ALL ON FUNCTION memory.apply_owner_project_thread_component_binding_v5(
  uuid,uuid,uuid,uuid,uuid,uuid,text,text,text
) FROM PUBLIC,memory_v5_reader,memory_v5_writer;
GRANT EXECUTE ON FUNCTION memory.apply_owner_project_thread_component_binding_v5(
  uuid,uuid,uuid,uuid,uuid,uuid,text,text,text
) TO brains_app;

REVOKE ALL ON FUNCTION memory.read_v5_shadow_project_knowledge(uuid,integer)
  FROM PUBLIC,memory_v5_writer,memory_v5_extraction_maintainer;
GRANT EXECUTE ON FUNCTION memory.read_v5_shadow_project_knowledge(uuid,integer)
  TO brains_app;

COMMIT;
