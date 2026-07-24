BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'V5.2 atom admission install requires sage';
  END IF;
  IF to_regprocedure('memory.require_v5_writer_context()') IS NULL
     OR to_regprocedure('memory.v5_digest_text(text)') IS NULL
     OR to_regprocedure('memory.v5_canonical_json_text(jsonb)') IS NULL
     OR to_regprocedure('memory.v5_source_spans_valid(jsonb)') IS NULL
     OR to_regprocedure(
       'memory.authoritative_owner_v5_2_packet_id_v1(uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.guard_v5_2_terminal_evidence_from_stage_v1()'
     ) IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.v5_2_local_packet_route_event') IS NULL
     OR to_regclass('memory.relational_stage_batch') IS NULL THEN
    RAISE EXCEPTION 'V5.2 atom admission prerequisites are absent';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_v5_2_atom_admission_maintainer') IS NULL THEN
    CREATE ROLE memory_v5_2_atom_admission_maintainer
      NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END
$role$;

DO $types$
BEGIN
  BEGIN
    CREATE TYPE memory.v5_2_atom_review_decision AS ENUM (
      'authorized','deferred','rejected'
    );
  EXCEPTION WHEN duplicate_object THEN NULL;
  END;
END
$types$;

CREATE TABLE IF NOT EXISTS memory.v5_2_atom_admission_proposal (
  proposal_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  packet_id uuid NOT NULL,
  job_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  policy_version text NOT NULL,
  validator_packet_sha256 text NOT NULL,
  packet_storage_sha256 text NOT NULL,
  source_atom_manifest_sha256 text NOT NULL,
  proposal jsonb NOT NULL,
  proposal_sha256 text NOT NULL,
  stage_projection_sha256 text NOT NULL,
  source_atom_count smallint NOT NULL,
  admitted_entity_mention_count smallint NOT NULL,
  admitted_observation_count smallint NOT NULL,
  admitted_comparison_hint_count smallint NOT NULL,
  deferred_atom_count smallint NOT NULL,
  retained_source_only_count smallint NOT NULL,
  rejected_atom_count smallint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,proposal_id),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,packet_id,policy_version),
  UNIQUE(owner_user_id,proposal_sha256),
  FOREIGN KEY(owner_user_id,packet_id)
    REFERENCES memory.evidence_extraction_packet_v5_local(
      owner_user_id,packet_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,job_id)
    REFERENCES memory.evidence_extraction_job(owner_user_id,job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  CHECK (policy_version='memory_v1_v5_2_atom_admission_policy_v1'),
  CHECK (memory.v5_sha256_valid(validator_packet_sha256)),
  CHECK (memory.v5_sha256_valid(packet_storage_sha256)),
  CHECK (memory.v5_sha256_valid(source_atom_manifest_sha256)),
  CHECK (jsonb_typeof(proposal)='object' AND pg_column_size(proposal)<=524288),
  CHECK (memory.v5_sha256_valid(proposal_sha256)),
  CHECK (memory.v5_sha256_valid(stage_projection_sha256)),
  CHECK (source_atom_count BETWEEN 1 AND 152),
  CHECK (admitted_entity_mention_count BETWEEN 0 AND 24),
  CHECK (admitted_observation_count BETWEEN 0 AND 32),
  CHECK (admitted_comparison_hint_count BETWEEN 0 AND 32),
  CHECK (deferred_atom_count BETWEEN 0 AND 152),
  CHECK (retained_source_only_count BETWEEN 0 AND 152),
  CHECK (rejected_atom_count BETWEEN 0 AND 152),
  CHECK (
    admitted_entity_mention_count+admitted_observation_count+
    admitted_comparison_hint_count+deferred_atom_count+
    retained_source_only_count+rejected_atom_count=source_atom_count
  )
);

CREATE TABLE IF NOT EXISTS memory.v5_2_atom_admission_review (
  review_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  proposal_id uuid NOT NULL,
  review_number integer NOT NULL,
  prior_review_id uuid,
  decision memory.v5_2_atom_review_decision NOT NULL,
  reviewer_type text NOT NULL,
  reviewer_ref text NOT NULL,
  review_reason_codes jsonb NOT NULL,
  proposal_sha256 text NOT NULL,
  authorization_manifest_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,review_id),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,proposal_id,review_number),
  UNIQUE(owner_user_id,authorization_manifest_sha256),
  FOREIGN KEY(owner_user_id,proposal_id)
    REFERENCES memory.v5_2_atom_admission_proposal(
      owner_user_id,proposal_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,prior_review_id)
    REFERENCES memory.v5_2_atom_admission_review(owner_user_id,review_id)
    ON DELETE RESTRICT,
  CHECK (review_number>=1),
  CHECK ((review_number=1)=(prior_review_id IS NULL)),
  CHECK (reviewer_type IN ('user','admin','system')),
  CHECK (btrim(reviewer_ref)<>'' AND length(reviewer_ref)<=500),
  CHECK (
    memory.v5_reason_codes_valid(review_reason_codes,32)
    AND jsonb_array_length(review_reason_codes)>=1
  ),
  CHECK (memory.v5_sha256_valid(proposal_sha256)),
  CHECK (memory.v5_sha256_valid(authorization_manifest_sha256))
);

CREATE TABLE IF NOT EXISTS memory.v5_2_atom_admission_apply (
  apply_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  proposal_id uuid NOT NULL,
  review_id uuid NOT NULL,
  packet_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  proposal_sha256 text NOT NULL,
  stage_projection_sha256 text NOT NULL,
  review_authorization_manifest_sha256 text NOT NULL,
  apply_manifest_sha256 text NOT NULL,
  admitted_entity_mention_count smallint NOT NULL,
  admitted_observation_count smallint NOT NULL,
  admitted_comparison_hint_count smallint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,apply_id),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,review_id),
  UNIQUE(owner_user_id,apply_manifest_sha256),
  FOREIGN KEY(owner_user_id,proposal_id)
    REFERENCES memory.v5_2_atom_admission_proposal(
      owner_user_id,proposal_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,review_id)
    REFERENCES memory.v5_2_atom_admission_review(owner_user_id,review_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,packet_id)
    REFERENCES memory.evidence_extraction_packet_v5_local(
      owner_user_id,packet_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  CHECK (memory.v5_sha256_valid(proposal_sha256)),
  CHECK (memory.v5_sha256_valid(stage_projection_sha256)),
  CHECK (memory.v5_sha256_valid(review_authorization_manifest_sha256)),
  CHECK (memory.v5_sha256_valid(apply_manifest_sha256)),
  CHECK (admitted_entity_mention_count BETWEEN 0 AND 24),
  CHECK (admitted_observation_count BETWEEN 1 AND 32),
  CHECK (admitted_comparison_hint_count BETWEEN 0 AND 32)
);

CREATE TABLE IF NOT EXISTS memory.v5_2_atom_admission_operation (
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  operation text NOT NULL,
  manifest_sha256 text NOT NULL,
  result jsonb NOT NULL,
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(owner_user_id,operation_id),
  UNIQUE(owner_user_id,operation,manifest_sha256),
  CHECK (operation IN ('record_proposal','record_review','apply_review')),
  CHECK (memory.v5_sha256_valid(manifest_sha256)),
  CHECK (jsonb_typeof(result)='object' AND pg_column_size(result)<=32768)
);

CREATE INDEX IF NOT EXISTS v5_2_atom_proposal_packet_idx
  ON memory.v5_2_atom_admission_proposal(
    owner_user_id,packet_id,created_at DESC
  );
CREATE INDEX IF NOT EXISTS v5_2_atom_review_proposal_idx
  ON memory.v5_2_atom_admission_review(
    owner_user_id,proposal_id,review_number DESC
  );
CREATE INDEX IF NOT EXISTS v5_2_atom_apply_packet_idx
  ON memory.v5_2_atom_admission_apply(
    owner_user_id,packet_id,created_at DESC
  );

DO $append_only$
DECLARE relation_name text;
BEGIN
  FOREACH relation_name IN ARRAY ARRAY[
    'v5_2_atom_admission_proposal',
    'v5_2_atom_admission_review',
    'v5_2_atom_admission_apply',
    'v5_2_atom_admission_operation'
  ] LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS %I ON memory.%I',
      relation_name||'_append_only_guard',relation_name
    );
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON memory.%I '
      'FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only()',
      relation_name||'_append_only_guard',relation_name
    );
  END LOOP;
END
$append_only$;

DO $rls$
DECLARE relation_name text;
BEGIN
  FOREACH relation_name IN ARRAY ARRAY[
    'v5_2_atom_admission_proposal',
    'v5_2_atom_admission_review',
    'v5_2_atom_admission_apply',
    'v5_2_atom_admission_operation'
  ] LOOP
    EXECUTE format('ALTER TABLE memory.%I ENABLE ROW LEVEL SECURITY',relation_name);
    EXECUTE format('ALTER TABLE memory.%I FORCE ROW LEVEL SECURITY',relation_name);
    EXECUTE format(
      'DROP POLICY IF EXISTS owner_isolation ON memory.%I',relation_name
    );
    EXECUTE format(
      'CREATE POLICY owner_isolation ON memory.%I '
      'FOR ALL TO memory_v5_2_atom_admission_maintainer '
      'USING (owner_user_id=(SELECT memory.current_actor_user_id())) '
      'WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()))',
      relation_name
    );
    EXECUTE format(
      'REVOKE ALL ON memory.%I FROM PUBLIC,brains_app',relation_name
    );
  END LOOP;
END
$rls$;

DROP POLICY IF EXISTS v5_2_atom_admission_packet_read
  ON memory.evidence_extraction_packet_v5_local;
CREATE POLICY v5_2_atom_admission_packet_read
  ON memory.evidence_extraction_packet_v5_local
  FOR SELECT TO memory_v5_2_atom_admission_maintainer
  USING (owner_user_id=(SELECT memory.current_actor_user_id()));

DROP POLICY IF EXISTS v5_2_atom_admission_route_read
  ON memory.v5_2_local_packet_route_event;
CREATE POLICY v5_2_atom_admission_route_read
  ON memory.v5_2_local_packet_route_event
  FOR SELECT TO memory_v5_2_atom_admission_maintainer
  USING (owner_user_id=(SELECT memory.current_actor_user_id()));

CREATE OR REPLACE FUNCTION memory.v5_2_source_spans_overlap_v1(
  left_spans jsonb,
  right_spans jsonb
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path=''
AS $function$
BEGIN
  IF NOT memory.v5_source_spans_valid(left_spans)
     OR NOT memory.v5_source_spans_valid(right_spans) THEN
    RETURN false;
  END IF;
  RETURN EXISTS (
    SELECT 1
    FROM jsonb_array_elements(left_spans) AS left_item(value)
    CROSS JOIN jsonb_array_elements(right_spans) AS right_item(value)
    WHERE (left_item.value->>'start')::integer
            <(right_item.value->>'end')::integer
      AND (right_item.value->>'start')::integer
            <(left_item.value->>'end')::integer
  );
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
  RETURN false;
END
$function$;

CREATE OR REPLACE FUNCTION memory.v5_2_atom_proposal_sha256_v1(
  proposal jsonb
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path=''
AS $function$
  SELECT memory.v5_digest_text(
    memory.v5_canonical_json_text(proposal-'proposal_sha256')
  )
$function$;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_2_atom_admission_v1(
  p_packet_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  atom_decisions jsonb;
  stage_projection jsonb;
  source_manifest text;
  stage_projection_sha text;
  base_proposal jsonb;
  proposal_value jsonb;
  source_count integer;
  admitted_mentions integer;
  admitted_observations integer;
  admitted_hints integer;
  deferred_count integer;
  retained_count integer;
  rejected_count integer;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_packet_id IS NULL THEN
    RAISE EXCEPTION 'packet_id is required' USING ERRCODE='22004';
  END IF;

  SELECT source.* INTO packet
  FROM memory.evidence_extraction_packet_v5_local AS source
  WHERE source.owner_user_id=actor
    AND source.packet_id=p_packet_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped V5.2 packet is unavailable'
      USING ERRCODE='P0002';
  END IF;
  IF packet.normalized_packet->>'contract_version'
       <>'memory_v1_relational_extraction_v5_2'
     OR packet.normalized_packet->>'predicate_registry_version'
       <>'memory_predicate_registry_v5_2'
     OR packet.packet_id IS DISTINCT FROM
       memory.authoritative_owner_v5_2_packet_id_v1(packet.evidence_id) THEN
    RAISE EXCEPTION 'V5.2 packet is not the authoritative owner leaf'
      USING ERRCODE='23514';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM memory.v5_2_local_packet_route_event AS route
    WHERE route.owner_user_id=actor
      AND route.packet_id=packet.packet_id
      AND route.evidence_id=packet.evidence_id
      AND route.route='manual_review_artifact_ready'
      AND route.reason_code='reviewable_relational_packet_v5_2'
  ) OR EXISTS (
    SELECT 1
    FROM memory.v5_2_local_packet_route_event AS route
    WHERE route.owner_user_id=actor
      AND route.packet_id=packet.packet_id
      AND route.route='terminal_no_stage'
  ) THEN
    RAISE EXCEPTION 'V5.2 packet lacks an active atom-review route'
      USING ERRCODE='23514';
  END IF;
  IF NOT memory.v5_jsonb_exact_keys(packet.normalized_packet,ARRAY[
       'contract_version','source_envelope','predicate_registry_version',
       'entity_mentions','observations','comparison_hints',
       'deferrals','packet_findings'
     ])
     OR jsonb_typeof(packet.normalized_packet->'entity_mentions')<>'array'
     OR jsonb_typeof(packet.normalized_packet->'observations')<>'array'
     OR jsonb_typeof(packet.normalized_packet->'comparison_hints')<>'array'
     OR jsonb_typeof(packet.normalized_packet->'deferrals')<>'array'
     OR jsonb_typeof(packet.normalized_packet->'packet_findings')<>'array'
     OR jsonb_array_length(packet.normalized_packet->'entity_mentions')
          <>packet.entity_mention_count
     OR jsonb_array_length(packet.normalized_packet->'observations')
          <>packet.observation_count
     OR jsonb_array_length(packet.normalized_packet->'comparison_hints')
          <>packet.comparison_hint_count
     OR jsonb_array_length(packet.normalized_packet->'deferrals')
          <>packet.deferral_count THEN
    RAISE EXCEPTION 'immutable V5.2 packet shape or counts are invalid'
      USING ERRCODE='23514';
  END IF;

  WITH
  deferral_spans AS (
    SELECT span.value
    FROM jsonb_array_elements(packet.normalized_packet->'deferrals')
      AS deferral(value)
    CROSS JOIN LATERAL jsonb_array_elements(deferral.value->'source_spans')
      AS span(value)
  ),
  observations AS (
    SELECT item.value AS atom,item.ordinality::integer AS ordinal,
      NOT EXISTS (
        SELECT 1 FROM deferral_spans AS blocked
        WHERE memory.v5_2_source_spans_overlap_v1(
          item.value->'source_spans',jsonb_build_array(blocked.value)
        )
      ) AS span_safe
    FROM jsonb_array_elements(packet.normalized_packet->'observations')
      WITH ORDINALITY AS item(value,ordinality)
  ),
  candidate_refs AS (
    SELECT atom->>'subject_entity_ref' AS entity_ref
    FROM observations WHERE span_safe
    UNION
    SELECT atom#>>'{object,entity_ref}' AS entity_ref
    FROM observations
    WHERE span_safe AND atom#>>'{object,kind}'='entity'
  ),
  mentions AS (
    SELECT item.value AS atom,item.ordinality::integer AS ordinal,
      item.value->>'entity_ref' AS entity_ref,
      (
        item.value->>'entity_type'='self'
        AND item.value->>'mention_kind'='self_reference'
        AND item.value->'name_text'='null'::jsonb
        AND item.value->>'relationship_role'='user:self'
      ) AS trusted_self,
      NOT EXISTS (
        SELECT 1 FROM deferral_spans AS blocked
        WHERE memory.v5_2_source_spans_overlap_v1(
          item.value->'source_spans',jsonb_build_array(blocked.value)
        )
      ) AS span_safe
    FROM jsonb_array_elements(packet.normalized_packet->'entity_mentions')
      WITH ORDINALITY AS item(value,ordinality)
  ),
  safe_mentions AS (
    SELECT mention.entity_ref
    FROM mentions AS mention
    JOIN candidate_refs AS needed USING(entity_ref)
    WHERE mention.span_safe OR mention.trusted_self
  ),
  final_observations AS (
    SELECT observation.*,
      observation.span_safe
      AND EXISTS (
        SELECT 1 FROM safe_mentions
        WHERE entity_ref=observation.atom->>'subject_entity_ref'
      )
      AND (
        observation.atom#>>'{object,kind}'<>'entity'
        OR EXISTS (
          SELECT 1 FROM safe_mentions
          WHERE entity_ref=observation.atom#>>'{object,entity_ref}'
        )
      ) AS admitted
    FROM observations AS observation
  ),
  admitted_refs AS (
    SELECT atom->>'subject_entity_ref' AS entity_ref
    FROM final_observations WHERE admitted
    UNION
    SELECT atom#>>'{object,entity_ref}' AS entity_ref
    FROM final_observations
    WHERE admitted AND atom#>>'{object,kind}'='entity'
  ),
  mention_decisions AS (
    SELECT 1 AS kind_order,'entity_mention'::text AS atom_kind,
      mention.entity_ref AS atom_ref,mention.ordinal,mention.atom,
      CASE
        WHEN admitted.entity_ref IS NOT NULL THEN 'admit'
        WHEN candidate.entity_ref IS NOT NULL THEN 'defer'
        ELSE 'retain_source_only'
      END AS disposition,
      CASE
        WHEN admitted.entity_ref IS NOT NULL AND mention.trusted_self
          THEN '["required_by_admitted_observation","trusted_owner_self_dependency"]'::jsonb
        WHEN admitted.entity_ref IS NOT NULL
          THEN '["required_by_admitted_observation","deferral_span_disjoint"]'::jsonb
        WHEN candidate.entity_ref IS NOT NULL
          THEN '["entity_dependency_deferred"]'::jsonb
        ELSE '["unreferenced_entity_source_only"]'::jsonb
      END AS decision_reasons
    FROM mentions AS mention
    LEFT JOIN admitted_refs AS admitted USING(entity_ref)
    LEFT JOIN candidate_refs AS candidate USING(entity_ref)
  ),
  observation_decisions AS (
    SELECT 2 AS kind_order,'observation'::text AS atom_kind,
      atom->>'observation_ref' AS atom_ref,ordinal,atom,
      CASE WHEN admitted THEN 'admit' ELSE 'defer' END AS disposition,
      CASE
        WHEN admitted
          THEN '["deferral_span_disjoint","entity_dependencies_admissible"]'::jsonb
        WHEN NOT span_safe
          THEN '["overlaps_source_deferral"]'::jsonb
        ELSE '["entity_dependency_deferred"]'::jsonb
      END AS decision_reasons
    FROM final_observations
  ),
  comparison_decisions AS (
    SELECT 3 AS kind_order,'comparison_hint'::text AS atom_kind,
      'comparison_'||lpad(item.ordinality::text,2,'0') AS atom_ref,
      item.ordinality::integer AS ordinal,item.value AS atom,
      CASE WHEN EXISTS (
        SELECT 1 FROM final_observations AS observation
        WHERE observation.admitted
          AND observation.atom->>'observation_ref'
                =item.value->>'observation_ref'
      ) THEN 'admit' ELSE 'defer' END AS disposition,
      CASE WHEN EXISTS (
        SELECT 1 FROM final_observations AS observation
        WHERE observation.admitted
          AND observation.atom->>'observation_ref'
                =item.value->>'observation_ref'
      ) THEN '["attached_to_admitted_observation"]'::jsonb
      ELSE '["attached_observation_deferred"]'::jsonb END AS decision_reasons
    FROM jsonb_array_elements(packet.normalized_packet->'comparison_hints')
      WITH ORDINALITY AS item(value,ordinality)
  ),
  deferral_decisions AS (
    SELECT 4 AS kind_order,'deferral'::text AS atom_kind,
      'deferral_'||lpad(item.ordinality::text,2,'0') AS atom_ref,
      item.ordinality::integer AS ordinal,item.value AS atom,'defer' AS disposition,
      jsonb_build_array(
        'source_deferral',
        'source_deferral:'||(item.value->>'reason_code')
      ) AS decision_reasons
    FROM jsonb_array_elements(packet.normalized_packet->'deferrals')
      WITH ORDINALITY AS item(value,ordinality)
  ),
  finding_decisions AS (
    SELECT 5 AS kind_order,'packet_finding'::text AS atom_kind,
      'finding_'||lpad(item.ordinality::text,2,'0') AS atom_ref,
      item.ordinality::integer AS ordinal,item.value AS atom,
      'retain_source_only' AS disposition,
      '["packet_finding_source_only"]'::jsonb AS decision_reasons
    FROM jsonb_array_elements(packet.normalized_packet->'packet_findings')
      WITH ORDINALITY AS item(value,ordinality)
  ),
  decisions AS (
    SELECT * FROM mention_decisions
    UNION ALL SELECT * FROM observation_decisions
    UNION ALL SELECT * FROM comparison_decisions
    UNION ALL SELECT * FROM deferral_decisions
    UNION ALL SELECT * FROM finding_decisions
  ),
  normalized_decisions AS (
    SELECT kind_order,ordinal,atom_kind,atom_ref,atom,disposition,
      jsonb_build_object(
        'atom_kind',atom_kind,
        'atom_ref',atom_ref,
        'ordinal',ordinal,
        'atom_sha256',memory.v5_digest_text(
          memory.v5_canonical_json_text(atom)
        ),
        'source_spans_sha256',memory.v5_digest_text(
          memory.v5_canonical_json_text(
            CASE WHEN jsonb_typeof(atom)='object' AND atom ? 'source_spans'
              THEN atom->'source_spans' ELSE '[]'::jsonb END
          )
        ),
        'proposed_disposition',disposition,
        'reason_codes',decision_reasons
      ) AS decision
    FROM decisions
  ),
  projection AS (
    SELECT jsonb_build_object(
      'contract_version',packet.normalized_packet->'contract_version',
      'source_envelope',packet.normalized_packet->'source_envelope',
      'predicate_registry_version',
        packet.normalized_packet->'predicate_registry_version',
      'entity_mentions',COALESCE((
        SELECT jsonb_agg(atom ORDER BY ordinal)
        FROM normalized_decisions
        WHERE atom_kind='entity_mention' AND disposition='admit'
      ),'[]'::jsonb),
      'observations',COALESCE((
        SELECT jsonb_agg(atom ORDER BY ordinal)
        FROM normalized_decisions
        WHERE atom_kind='observation' AND disposition='admit'
      ),'[]'::jsonb),
      'comparison_hints',COALESCE((
        SELECT jsonb_agg(atom ORDER BY ordinal)
        FROM normalized_decisions
        WHERE atom_kind='comparison_hint' AND disposition='admit'
      ),'[]'::jsonb),
      'deferrals','[]'::jsonb,
      'packet_findings','[]'::jsonb
    ) AS value
  )
  SELECT
    jsonb_agg(decision ORDER BY kind_order,ordinal),
    projection.value,
    memory.v5_digest_text(COALESCE(string_agg(
      concat_ws('|',atom_kind,atom_ref,
        memory.v5_digest_text(memory.v5_canonical_json_text(atom))
      ),E'\n' ORDER BY kind_order,ordinal
    ),'none')),
    count(*),
    count(*) FILTER (
      WHERE atom_kind='entity_mention' AND disposition='admit'
    ),
    count(*) FILTER (
      WHERE atom_kind='observation' AND disposition='admit'
    ),
    count(*) FILTER (
      WHERE atom_kind='comparison_hint' AND disposition='admit'
    ),
    count(*) FILTER (WHERE disposition='defer'),
    count(*) FILTER (WHERE disposition='retain_source_only'),
    count(*) FILTER (WHERE disposition='reject')
  INTO atom_decisions,stage_projection,source_manifest,source_count,
       admitted_mentions,admitted_observations,admitted_hints,
       deferred_count,retained_count,rejected_count
  FROM normalized_decisions
  CROSS JOIN projection
  GROUP BY projection.value;

  IF source_count IS NULL OR source_count<1
     OR atom_decisions IS NULL OR stage_projection IS NULL THEN
    RAISE EXCEPTION 'V5.2 packet produced no accountable atoms'
      USING ERRCODE='23514';
  END IF;
  stage_projection_sha:=memory.v5_digest_text(
    memory.v5_canonical_json_text(stage_projection)
  );
  base_proposal:=jsonb_build_object(
    'contract_version','memory_v1_v5_2_atom_admission_proposal_v1',
    'policy_version','memory_v1_v5_2_atom_admission_policy_v1',
    'source_packet_id',packet.packet_id::text,
    'source_job_id',packet.job_id::text,
    'source_evidence_id',packet.evidence_id::text,
    'source_validator_packet_sha256',packet.validator_packet_sha256,
    'source_packet_storage_sha256',packet.packet_storage_sha256,
    'source_atom_manifest_sha256',source_manifest,
    'atom_decisions',atom_decisions,
    'stage_projection',stage_projection,
    'stage_projection_sha256',stage_projection_sha,
    'counts',jsonb_build_object(
      'source_atom_count',source_count,
      'admitted_entity_mention_count',admitted_mentions,
      'admitted_observation_count',admitted_observations,
      'admitted_comparison_hint_count',admitted_hints,
      'deferred_atom_count',deferred_count,
      'retained_source_only_count',retained_count,
      'rejected_atom_count',rejected_count
    )
  );
  proposal_value:=base_proposal||jsonb_build_object(
    'proposal_sha256',memory.v5_2_atom_proposal_sha256_v1(base_proposal)
  );
  RETURN proposal_value;
END
$function$;

CREATE OR REPLACE FUNCTION memory.record_owner_v5_2_atom_proposal_v1(
  p_operation_id uuid,
  p_proposal_id uuid,
  p_packet_id uuid,
  p_expected_packet_storage_sha256 text,
  p_expected_proposal_sha256 text
)
RETURNS TABLE(
  proposal_id uuid,
  outcome text,
  admitted_observations integer,
  deferred_atoms integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  proposal_value jsonb;
  source_packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  existing memory.v5_2_atom_admission_proposal%ROWTYPE;
  replay memory.v5_2_atom_admission_operation%ROWTYPE;
  operation_manifest text;
  result_value jsonb;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_operation_id IS NULL OR p_proposal_id IS NULL
     OR p_packet_id IS NULL
     OR NOT memory.v5_sha256_valid(p_expected_packet_storage_sha256)
     OR NOT memory.v5_sha256_valid(p_expected_proposal_sha256) THEN
    RAISE EXCEPTION 'atom proposal request inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|',actor::text,'v5_2_atom_proposal',p_packet_id::text),0
  ));
  proposal_value:=memory.plan_owner_v5_2_atom_admission_v1(p_packet_id);
  IF proposal_value->>'source_packet_storage_sha256'
       <>p_expected_packet_storage_sha256
     OR proposal_value->>'proposal_sha256'
       <>p_expected_proposal_sha256 THEN
    RAISE EXCEPTION 'atom proposal expected hash mismatch'
      USING ERRCODE='23514';
  END IF;
  operation_manifest:=memory.v5_digest_text(concat_ws('|',
    'memory_v1_v5_2_atom_proposal_record_v1',actor::text,
    p_proposal_id::text,p_packet_id::text,
    p_expected_packet_storage_sha256,p_expected_proposal_sha256
  ));

  SELECT * INTO replay
  FROM memory.v5_2_atom_admission_operation
  WHERE owner_user_id=actor AND operation_id=p_operation_id;
  IF FOUND THEN
    IF replay.operation<>'record_proposal'
       OR replay.manifest_sha256<>operation_manifest THEN
      RAISE EXCEPTION 'atom proposal request replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      (replay.result->>'proposal_id')::uuid,'replayed',
      (replay.result->>'admitted_observation_count')::integer,
      (replay.result->>'deferred_atom_count')::integer;
    RETURN;
  END IF;

  SELECT * INTO existing
  FROM memory.v5_2_atom_admission_proposal
  WHERE owner_user_id=actor
    AND packet_id=p_packet_id
    AND policy_version='memory_v1_v5_2_atom_admission_policy_v1';
  IF FOUND THEN
    IF existing.proposal_sha256<>p_expected_proposal_sha256 THEN
      RAISE EXCEPTION 'stored atom proposal differs from current plan'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      existing.proposal_id,'replayed',
      existing.admitted_observation_count::integer,
      existing.deferred_atom_count::integer;
    RETURN;
  END IF;

  SELECT * INTO STRICT source_packet
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id=actor AND packet_id=p_packet_id;
  INSERT INTO memory.v5_2_atom_admission_proposal(
    proposal_id,owner_user_id,operation_id,packet_id,job_id,evidence_id,
    policy_version,validator_packet_sha256,packet_storage_sha256,
    source_atom_manifest_sha256,proposal,proposal_sha256,
    stage_projection_sha256,source_atom_count,
    admitted_entity_mention_count,admitted_observation_count,
    admitted_comparison_hint_count,deferred_atom_count,
    retained_source_only_count,rejected_atom_count
  ) VALUES (
    p_proposal_id,actor,p_operation_id,p_packet_id,source_packet.job_id,
    source_packet.evidence_id,
    'memory_v1_v5_2_atom_admission_policy_v1',
    source_packet.validator_packet_sha256,source_packet.packet_storage_sha256,
    proposal_value->>'source_atom_manifest_sha256',proposal_value,
    proposal_value->>'proposal_sha256',
    proposal_value->>'stage_projection_sha256',
    (proposal_value#>>'{counts,source_atom_count}')::smallint,
    (proposal_value#>>'{counts,admitted_entity_mention_count}')::smallint,
    (proposal_value#>>'{counts,admitted_observation_count}')::smallint,
    (proposal_value#>>'{counts,admitted_comparison_hint_count}')::smallint,
    (proposal_value#>>'{counts,deferred_atom_count}')::smallint,
    (proposal_value#>>'{counts,retained_source_only_count}')::smallint,
    (proposal_value#>>'{counts,rejected_atom_count}')::smallint
  );
  result_value:=jsonb_build_object(
    'proposal_id',p_proposal_id::text,
    'proposal_sha256',p_expected_proposal_sha256,
    'admitted_observation_count',
      proposal_value#>>'{counts,admitted_observation_count}',
    'deferred_atom_count',proposal_value#>>'{counts,deferred_atom_count}'
  );
  INSERT INTO memory.v5_2_atom_admission_operation(
    owner_user_id,operation_id,operation,manifest_sha256,result,
    invoked_by_session
  ) VALUES (
    actor,p_operation_id,'record_proposal',operation_manifest,
    result_value,session_user
  );
  RETURN QUERY SELECT
    p_proposal_id,'applied',
    (proposal_value#>>'{counts,admitted_observation_count}')::integer,
    (proposal_value#>>'{counts,deferred_atom_count}')::integer;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_owner_v5_2_atom_review_v1(
  p_proposal_id uuid,
  p_decision memory.v5_2_atom_review_decision,
  p_reviewer_type text,
  p_reviewer_ref text,
  p_review_reason_codes jsonb
)
RETURNS TABLE(
  next_review_number integer,
  prior_review_id uuid,
  proposal_sha256 text,
  authorization_manifest_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  stored memory.v5_2_atom_admission_proposal%ROWTYPE;
  current_plan jsonb;
  latest memory.v5_2_atom_admission_review%ROWTYPE;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_proposal_id IS NULL OR p_decision IS NULL
     OR p_reviewer_type NOT IN ('user','admin','system')
     OR btrim(COALESCE(p_reviewer_ref,''))=''
     OR length(p_reviewer_ref)>500
     OR NOT memory.v5_reason_codes_valid(p_review_reason_codes,32)
     OR jsonb_array_length(p_review_reason_codes)<1 THEN
    RAISE EXCEPTION 'atom review inputs are invalid' USING ERRCODE='22023';
  END IF;
  SELECT proposal.* INTO stored
  FROM memory.v5_2_atom_admission_proposal AS proposal
  WHERE proposal.owner_user_id=actor
    AND proposal.proposal_id=p_proposal_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped atom proposal is unavailable'
      USING ERRCODE='P0002';
  END IF;
  current_plan:=memory.plan_owner_v5_2_atom_admission_v1(stored.packet_id);
  IF current_plan->>'proposal_sha256'<>stored.proposal_sha256
     OR current_plan IS DISTINCT FROM stored.proposal THEN
    RAISE EXCEPTION 'atom proposal is stale or non-deterministic'
      USING ERRCODE='23514';
  END IF;
  IF p_reviewer_type IN ('user','admin') AND p_reviewer_ref<>actor::text THEN
    RAISE EXCEPTION 'human atom reviewer must equal the actor owner'
      USING ERRCODE='42501';
  END IF;
  IF p_reviewer_type='system' AND p_reviewer_ref
       <>'memory_v1_v5_2_atom_admission_policy_v1' THEN
    RAISE EXCEPTION 'system atom reviewer identity is not policy-bound'
      USING ERRCODE='42501';
  END IF;
  IF p_reviewer_type='system' AND p_decision='authorized' AND (
       stored.admitted_observation_count<1
       OR stored.rejected_atom_count>0
     ) THEN
    RAISE EXCEPTION 'system cannot authorize an empty or rejected atom plan'
      USING ERRCODE='42501';
  END IF;

  SELECT review.* INTO latest
  FROM memory.v5_2_atom_admission_review AS review
  WHERE review.owner_user_id=actor
    AND review.proposal_id=p_proposal_id
  ORDER BY review.review_number DESC
  LIMIT 1;
  next_review_number:=COALESCE(latest.review_number,0)+1;
  prior_review_id:=latest.review_id;
  proposal_sha256:=stored.proposal_sha256;
  authorization_manifest_sha256:=memory.v5_digest_text(concat_ws('|',
    'memory_v1_v5_2_atom_review_authorization_v1',actor::text,
    p_proposal_id::text,stored.proposal_sha256,
    next_review_number::text,COALESCE(prior_review_id::text,''),
    p_decision::text,p_reviewer_type,p_reviewer_ref,
    memory.v5_canonical_json_text(p_review_reason_codes)
  ));
  RETURN NEXT;
END
$function$;

CREATE OR REPLACE FUNCTION memory.review_owner_v5_2_atom_proposal_v1(
  p_operation_id uuid,
  p_review_id uuid,
  p_proposal_id uuid,
  p_decision memory.v5_2_atom_review_decision,
  p_reviewer_type text,
  p_reviewer_ref text,
  p_review_reason_codes jsonb,
  p_authorization_manifest_sha256 text
)
RETURNS TABLE(
  review_id uuid,
  outcome text,
  review_number integer,
  decision memory.v5_2_atom_review_decision
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  preflight record;
  replay memory.v5_2_atom_admission_operation%ROWTYPE;
  operation_manifest text;
  result_value jsonb;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_operation_id IS NULL OR p_review_id IS NULL
     OR NOT memory.v5_sha256_valid(p_authorization_manifest_sha256) THEN
    RAISE EXCEPTION 'atom review request inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|',actor::text,'v5_2_atom_review',p_proposal_id::text),0
  ));
  operation_manifest:=memory.v5_digest_text(concat_ws('|',
    'memory_v1_v5_2_atom_review_record_v1',actor::text,
    p_review_id::text,p_proposal_id::text,
    p_authorization_manifest_sha256
  ));

  SELECT * INTO replay
  FROM memory.v5_2_atom_admission_operation
  WHERE owner_user_id=actor AND operation_id=p_operation_id;
  IF FOUND THEN
    IF replay.operation<>'record_review'
       OR replay.manifest_sha256<>operation_manifest THEN
      RAISE EXCEPTION 'atom review replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      (replay.result->>'review_id')::uuid,'replayed',
      (replay.result->>'review_number')::integer,
      (replay.result->>'decision')::memory.v5_2_atom_review_decision;
    RETURN;
  END IF;

  SELECT * INTO preflight
  FROM memory.preflight_owner_v5_2_atom_review_v1(
    p_proposal_id,p_decision,p_reviewer_type,p_reviewer_ref,
    p_review_reason_codes
  );
  IF preflight.authorization_manifest_sha256
       <>p_authorization_manifest_sha256 THEN
    RAISE EXCEPTION 'atom review authorization manifest mismatch'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.v5_2_atom_admission_review(
    review_id,owner_user_id,operation_id,proposal_id,review_number,
    prior_review_id,decision,reviewer_type,reviewer_ref,
    review_reason_codes,proposal_sha256,authorization_manifest_sha256
  ) VALUES (
    p_review_id,actor,p_operation_id,p_proposal_id,
    preflight.next_review_number,preflight.prior_review_id,p_decision,
    p_reviewer_type,p_reviewer_ref,p_review_reason_codes,
    preflight.proposal_sha256,p_authorization_manifest_sha256
  );
  result_value:=jsonb_build_object(
    'review_id',p_review_id::text,
    'review_number',preflight.next_review_number,
    'decision',p_decision::text
  );
  INSERT INTO memory.v5_2_atom_admission_operation(
    owner_user_id,operation_id,operation,manifest_sha256,result,
    invoked_by_session
  ) VALUES (
    actor,p_operation_id,'record_review',operation_manifest,
    result_value,session_user
  );
  RETURN QUERY SELECT
    p_review_id,'applied',preflight.next_review_number,p_decision;
END
$function$;

CREATE OR REPLACE FUNCTION memory.preflight_owner_v5_2_atom_apply_v1(
  p_review_id uuid
)
RETURNS TABLE(
  proposal_id uuid,
  packet_id uuid,
  evidence_id uuid,
  proposal_sha256 text,
  stage_projection_sha256 text,
  review_authorization_manifest_sha256 text,
  apply_manifest_sha256 text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  review memory.v5_2_atom_admission_review%ROWTYPE;
  proposal memory.v5_2_atom_admission_proposal%ROWTYPE;
  current_plan jsonb;
BEGIN
  actor:=memory.require_v5_writer_context();
  SELECT stored.* INTO review
  FROM memory.v5_2_atom_admission_review AS stored
  WHERE stored.owner_user_id=actor AND stored.review_id=p_review_id;
  IF NOT FOUND OR review.decision<>'authorized' THEN
    RAISE EXCEPTION 'latest authorized atom review is required'
      USING ERRCODE='23514';
  END IF;
  IF review.review_id IS DISTINCT FROM (
    SELECT latest.review_id
    FROM memory.v5_2_atom_admission_review AS latest
    WHERE latest.owner_user_id=actor
      AND latest.proposal_id=review.proposal_id
    ORDER BY latest.review_number DESC LIMIT 1
  ) THEN
    RAISE EXCEPTION 'atom review is not the latest decision'
      USING ERRCODE='23514';
  END IF;
  SELECT stored.* INTO STRICT proposal
  FROM memory.v5_2_atom_admission_proposal AS stored
  WHERE stored.owner_user_id=actor
    AND stored.proposal_id=review.proposal_id;
  current_plan:=memory.plan_owner_v5_2_atom_admission_v1(proposal.packet_id);
  IF current_plan IS DISTINCT FROM proposal.proposal
     OR current_plan->>'proposal_sha256'<>proposal.proposal_sha256
     OR proposal.admitted_observation_count<1
     OR jsonb_array_length(
          proposal.proposal#>'{stage_projection,deferrals}'
        )<>0 THEN
    RAISE EXCEPTION 'atom proposal is stale, empty, or not stage-safe'
      USING ERRCODE='23514';
  END IF;

  proposal_id:=proposal.proposal_id;
  packet_id:=proposal.packet_id;
  evidence_id:=proposal.evidence_id;
  proposal_sha256:=proposal.proposal_sha256;
  stage_projection_sha256:=proposal.stage_projection_sha256;
  review_authorization_manifest_sha256:=
    review.authorization_manifest_sha256;
  apply_manifest_sha256:=memory.v5_digest_text(concat_ws('|',
    'memory_v1_v5_2_atom_apply_v1',actor::text,review.review_id::text,
    proposal.proposal_id::text,proposal.packet_id::text,
    proposal.evidence_id::text,proposal.proposal_sha256,
    proposal.stage_projection_sha256,review.authorization_manifest_sha256
  ));
  RETURN NEXT;
END
$function$;

CREATE OR REPLACE FUNCTION memory.apply_owner_v5_2_atom_review_v1(
  p_operation_id uuid,
  p_apply_id uuid,
  p_review_id uuid,
  p_apply_manifest_sha256 text
)
RETURNS TABLE(
  apply_id uuid,
  outcome text,
  admitted_observations integer,
  stage_projection_sha256 text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  preflight record;
  proposal memory.v5_2_atom_admission_proposal%ROWTYPE;
  replay memory.v5_2_atom_admission_operation%ROWTYPE;
  operation_manifest text;
  result_value jsonb;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_operation_id IS NULL OR p_apply_id IS NULL
     OR NOT memory.v5_sha256_valid(p_apply_manifest_sha256) THEN
    RAISE EXCEPTION 'atom apply request inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|',actor::text,'v5_2_atom_apply',p_review_id::text),0
  ));
  operation_manifest:=memory.v5_digest_text(concat_ws('|',
    'memory_v1_v5_2_atom_apply_record_v1',actor::text,
    p_apply_id::text,p_review_id::text,p_apply_manifest_sha256
  ));
  SELECT * INTO replay
  FROM memory.v5_2_atom_admission_operation
  WHERE owner_user_id=actor AND operation_id=p_operation_id;
  IF FOUND THEN
    IF replay.operation<>'apply_review'
       OR replay.manifest_sha256<>operation_manifest THEN
      RAISE EXCEPTION 'atom apply replay payload mismatch'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      (replay.result->>'apply_id')::uuid,'replayed',
      (replay.result->>'admitted_observation_count')::integer,
      replay.result->>'stage_projection_sha256';
    RETURN;
  END IF;

  SELECT * INTO preflight
  FROM memory.preflight_owner_v5_2_atom_apply_v1(p_review_id);
  IF preflight.apply_manifest_sha256<>p_apply_manifest_sha256 THEN
    RAISE EXCEPTION 'atom apply authorization manifest mismatch'
      USING ERRCODE='23514';
  END IF;

  SELECT stored.* INTO STRICT proposal
  FROM memory.v5_2_atom_admission_proposal AS stored
  WHERE stored.owner_user_id=actor
    AND stored.proposal_id=preflight.proposal_id;

  INSERT INTO memory.v5_2_atom_admission_apply(
    apply_id,owner_user_id,operation_id,proposal_id,review_id,
    packet_id,evidence_id,proposal_sha256,stage_projection_sha256,
    review_authorization_manifest_sha256,apply_manifest_sha256,
    admitted_entity_mention_count,admitted_observation_count,
    admitted_comparison_hint_count
  ) VALUES (
    p_apply_id,actor,p_operation_id,proposal.proposal_id,p_review_id,
    proposal.packet_id,proposal.evidence_id,proposal.proposal_sha256,
    proposal.stage_projection_sha256,
    preflight.review_authorization_manifest_sha256,
    p_apply_manifest_sha256,proposal.admitted_entity_mention_count,
    proposal.admitted_observation_count,
    proposal.admitted_comparison_hint_count
  );
  result_value:=jsonb_build_object(
    'apply_id',p_apply_id::text,
    'admitted_observation_count',proposal.admitted_observation_count,
    'stage_projection_sha256',proposal.stage_projection_sha256
  );
  INSERT INTO memory.v5_2_atom_admission_operation(
    owner_user_id,operation_id,operation,manifest_sha256,result,
    invoked_by_session
  ) VALUES (
    actor,p_operation_id,'apply_review',operation_manifest,
    result_value,session_user
  );
  RETURN QUERY SELECT
    p_apply_id,'applied',proposal.admitted_observation_count::integer,
    proposal.stage_projection_sha256;
END
$function$;

CREATE OR REPLACE FUNCTION
  memory.v5_2_atom_stage_projection_authorized_v1(
    p_owner_user_id uuid,
    p_evidence_id uuid,
    p_stage_projection jsonb,
    p_stage_projection_sha256 text
  )
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path=''
AS $function$
  SELECT p_owner_user_id IS NOT NULL
    AND p_owner_user_id=memory.current_actor_user_id()
    AND p_evidence_id IS NOT NULL
    AND memory.v5_sha256_valid(p_stage_projection_sha256)
    AND EXISTS (
      SELECT 1
      FROM memory.v5_2_atom_admission_apply AS applied
      JOIN memory.v5_2_atom_admission_review AS review
        ON review.owner_user_id=applied.owner_user_id
       AND review.review_id=applied.review_id
      JOIN memory.v5_2_atom_admission_proposal AS proposal
        ON proposal.owner_user_id=applied.owner_user_id
       AND proposal.proposal_id=applied.proposal_id
      WHERE applied.owner_user_id=p_owner_user_id
        AND applied.evidence_id=p_evidence_id
        AND applied.stage_projection_sha256=p_stage_projection_sha256
        AND proposal.stage_projection_sha256=p_stage_projection_sha256
        AND proposal.proposal->'stage_projection'=p_stage_projection
        AND review.decision='authorized'
        AND review.review_id=(
          SELECT latest.review_id
          FROM memory.v5_2_atom_admission_review AS latest
          WHERE latest.owner_user_id=review.owner_user_id
            AND latest.proposal_id=review.proposal_id
          ORDER BY latest.review_number DESC LIMIT 1
        )
        AND proposal.packet_id=
          memory.authoritative_owner_v5_2_packet_id_v1(p_evidence_id)
        AND EXISTS (
          SELECT 1
          FROM memory.v5_2_local_packet_route_event AS route
          WHERE route.owner_user_id=proposal.owner_user_id
            AND route.packet_id=proposal.packet_id
            AND route.route='manual_review_artifact_ready'
        )
    )
$function$;

CREATE OR REPLACE FUNCTION
  memory.guard_v5_2_terminal_evidence_from_stage_v1()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  extraction jsonb;
  source_job_id uuid;
  source_packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  authoritative_packet_id uuid;
  exact_packet boolean;
  atom_projection boolean;
BEGIN
  IF TG_OP<>'INSERT' OR NEW.owner_user_id IS NULL OR NEW.evidence_id IS NULL THEN
    RAISE EXCEPTION 'V5.2 stage guard accepts bounded inserts only'
      USING ERRCODE='23514';
  END IF;
  IF memory.current_actor_user_id() IS DISTINCT FROM NEW.owner_user_id THEN
    RAISE EXCEPTION 'V5.2 stage guard requires exact owner context'
      USING ERRCODE='42501';
  END IF;

  BEGIN
    extraction:=NEW.extraction_packet_text::jsonb;
  EXCEPTION
    WHEN OTHERS THEN
      RAISE EXCEPTION 'relational stage extraction packet is invalid JSON'
        USING ERRCODE='23514';
  END;

  IF extraction->>'contract_version'
       <>'memory_v1_relational_extraction_v5_2' THEN
    IF EXISTS (
      SELECT 1
      FROM memory.v5_2_local_packet_route_event AS event
      WHERE event.owner_user_id=NEW.owner_user_id
        AND event.evidence_id=NEW.evidence_id
        AND event.route='terminal_no_stage'
    ) THEN
      RAISE EXCEPTION 'terminal V5.2 evidence cannot enter relational staging'
        USING ERRCODE='23514';
    END IF;
    RETURN NEW;
  END IF;

  BEGIN
    source_job_id:=(extraction#>>'{source_envelope,job_id}')::uuid;
  EXCEPTION
    WHEN OTHERS THEN
      RAISE EXCEPTION 'V5.2 stage packet has no valid source job'
        USING ERRCODE='23514';
  END;

  SELECT packet.* INTO source_packet
  FROM memory.evidence_extraction_packet_v5_local AS packet
  WHERE packet.owner_user_id=NEW.owner_user_id
    AND packet.evidence_id=NEW.evidence_id
    AND packet.job_id=source_job_id;
  IF source_packet.packet_id IS NULL THEN
    RAISE EXCEPTION 'V5.2 stage source packet is not owner-authoritative'
      USING ERRCODE='23514';
  END IF;

  exact_packet:=extraction IS NOT DISTINCT FROM source_packet.normalized_packet
    AND NEW.extraction_packet_sha256 IS NOT DISTINCT FROM
      source_packet.validator_packet_sha256;
  atom_projection:=memory.v5_2_atom_stage_projection_authorized_v1(
    NEW.owner_user_id,NEW.evidence_id,extraction,
    NEW.extraction_packet_sha256
  );
  IF NOT exact_packet AND NOT atom_projection THEN
    RAISE EXCEPTION
      'V5.2 stage packet differs from immutable extraction or authorized atom projection'
      USING ERRCODE='23514';
  END IF;

  authoritative_packet_id:=
    memory.authoritative_owner_v5_2_packet_id_v1(NEW.evidence_id);
  IF authoritative_packet_id IS NULL
     OR source_packet.packet_id<>authoritative_packet_id THEN
    RAISE EXCEPTION 'V5.2 stage source packet is not the authority leaf'
      USING ERRCODE='23514';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM memory.v5_2_local_packet_route_event AS event
    WHERE event.owner_user_id=NEW.owner_user_id
      AND event.evidence_id=NEW.evidence_id
      AND event.packet_id=source_packet.packet_id
      AND event.route='manual_review_artifact_ready'
  ) OR EXISTS (
    SELECT 1
    FROM memory.v5_2_local_packet_route_event AS event
    WHERE event.owner_user_id=NEW.owner_user_id
      AND event.evidence_id=NEW.evidence_id
      AND event.packet_id=source_packet.packet_id
      AND event.route='terminal_no_stage'
  ) THEN
    RAISE EXCEPTION 'V5.2 stage source packet lacks an active review route'
      USING ERRCODE='23514';
  END IF;

  IF (
       source_packet.deferral_count>0
       OR jsonb_array_length(source_packet.normalized_packet->'deferrals')>0
     ) AND NOT atom_projection THEN
    RAISE EXCEPTION
      'V5.2 packet deferrals require controlled atom-level admission'
      USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END
$function$;

ALTER TABLE memory.v5_2_atom_admission_proposal
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER TABLE memory.v5_2_atom_admission_review
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER TABLE memory.v5_2_atom_admission_apply
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER TABLE memory.v5_2_atom_admission_operation
  OWNER TO memory_v5_2_atom_admission_maintainer;

ALTER FUNCTION memory.v5_2_source_spans_overlap_v1(jsonb,jsonb)
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.v5_2_atom_proposal_sha256_v1(jsonb)
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.plan_owner_v5_2_atom_admission_v1(uuid)
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.record_owner_v5_2_atom_proposal_v1(
  uuid,uuid,uuid,text,text
) OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.preflight_owner_v5_2_atom_review_v1(
  uuid,memory.v5_2_atom_review_decision,text,text,jsonb
) OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.review_owner_v5_2_atom_proposal_v1(
  uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text
) OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.preflight_owner_v5_2_atom_apply_v1(uuid)
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.apply_owner_v5_2_atom_review_v1(
  uuid,uuid,uuid,text
) OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.v5_2_atom_stage_projection_authorized_v1(
  uuid,uuid,jsonb,text
) OWNER TO memory_v5_2_atom_admission_maintainer;

ALTER FUNCTION memory.guard_v5_2_terminal_evidence_from_stage_v1()
  OWNER TO memory_v5_2_local_router_maintainer;

GRANT USAGE ON SCHEMA memory
  TO memory_v5_2_atom_admission_maintainer;
GRANT SELECT ON memory.evidence_extraction_packet_v5_local,
  memory.v5_2_local_packet_route_event
  TO memory_v5_2_atom_admission_maintainer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id(),
  memory.require_v5_writer_context(),
  memory.v5_digest_text(text),
  memory.v5_canonical_json_text(jsonb),
  memory.v5_sha256_valid(text),
  memory.v5_reason_codes_valid(jsonb,integer),
  memory.v5_jsonb_exact_keys(jsonb,text[]),
  memory.v5_source_spans_valid(jsonb),
  memory.authoritative_owner_v5_2_packet_id_v1(uuid)
  TO memory_v5_2_atom_admission_maintainer;

REVOKE ALL ON FUNCTION
  memory.v5_2_source_spans_overlap_v1(jsonb,jsonb),
  memory.v5_2_atom_proposal_sha256_v1(jsonb),
  memory.v5_2_atom_stage_projection_authorized_v1(uuid,uuid,jsonb,text)
  FROM PUBLIC,brains_app;

GRANT EXECUTE ON FUNCTION
  memory.v5_2_atom_stage_projection_authorized_v1(uuid,uuid,jsonb,text)
  TO memory_v5_2_local_router_maintainer;

REVOKE ALL ON FUNCTION
  memory.plan_owner_v5_2_atom_admission_v1(uuid),
  memory.record_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,text,text),
  memory.preflight_owner_v5_2_atom_review_v1(
    uuid,memory.v5_2_atom_review_decision,text,text,jsonb
  ),
  memory.review_owner_v5_2_atom_proposal_v1(
    uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text
  ),
  memory.preflight_owner_v5_2_atom_apply_v1(uuid),
  memory.apply_owner_v5_2_atom_review_v1(uuid,uuid,uuid,text)
  FROM PUBLIC,brains_app;

GRANT EXECUTE ON FUNCTION
  memory.plan_owner_v5_2_atom_admission_v1(uuid),
  memory.record_owner_v5_2_atom_proposal_v1(uuid,uuid,uuid,text,text),
  memory.preflight_owner_v5_2_atom_review_v1(
    uuid,memory.v5_2_atom_review_decision,text,text,jsonb
  ),
  memory.review_owner_v5_2_atom_proposal_v1(
    uuid,uuid,uuid,memory.v5_2_atom_review_decision,text,text,jsonb,text
  ),
  memory.preflight_owner_v5_2_atom_apply_v1(uuid),
  memory.apply_owner_v5_2_atom_review_v1(uuid,uuid,uuid,text)
  TO brains_app;

REVOKE ALL ON FUNCTION
  memory.guard_v5_2_terminal_evidence_from_stage_v1()
  FROM PUBLIC,brains_app,memory_v5_2_local_router_maintainer;

COMMENT ON TABLE memory.v5_2_atom_admission_proposal IS
  'Append-only deterministic per-atom disposition plans derived from immutable authoritative V5.2 packets. A proposal never alters source evidence or extraction.';
COMMENT ON TABLE memory.v5_2_atom_admission_review IS
  'Append-only review chain over a deterministic V5.2 atom admission proposal. The latest decision is authoritative for apply eligibility.';
COMMENT ON TABLE memory.v5_2_atom_admission_apply IS
  'Append-only authorization binding for an exact reviewed stage projection. This table does not create relational stage rows, claims, Qdrant projections, or prompt influence.';
COMMENT ON FUNCTION memory.plan_owner_v5_2_atom_admission_v1(uuid) IS
  'Builds a deterministic owner-scoped plan. Observations disjoint from source deferrals may advance only when every entity dependency is admissible; overlapping or unsafe atoms remain deferred.';
COMMENT ON FUNCTION
  memory.guard_v5_2_terminal_evidence_from_stage_v1() IS
  'Requires either the exact immutable authoritative V5.2 packet with no deferrals or an exact current atom-admission stage projection authorized through append-only proposal/review/apply records.';

COMMIT;
