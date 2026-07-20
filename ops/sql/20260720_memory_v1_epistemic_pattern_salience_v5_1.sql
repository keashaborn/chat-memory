BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'epistemic pattern/salience V5.1 migration requires sage';
  END IF;
  IF to_regprocedure('memory.current_actor_user_id()') IS NULL
     OR to_regprocedure('memory.guard_v5_append_only()') IS NULL
     OR to_regprocedure('memory.v5_sha256_valid(text)') IS NULL
     OR to_regprocedure('memory.v5_reason_codes_valid(jsonb,integer)') IS NULL
     OR to_regprocedure('memory.v5_jsonb_exact_keys(jsonb,text[])') IS NULL
     OR to_regprocedure('memory.v5_canonical_json_text(jsonb)') IS NULL
     OR to_regprocedure('memory.v5_digest_text(text)') IS NULL
     OR to_regclass('memory.claim_revision') IS NULL
     OR to_regclass('memory.preference_revision_v5') IS NULL
     OR to_regclass('memory.project_knowledge_revision_v5') IS NULL
     OR to_regclass('memory.observation') IS NULL
     OR to_regclass('memory.retrieval_trace') IS NULL THEN
    RAISE EXCEPTION 'epistemic pattern/salience V5.1 prerequisites are absent';
  END IF;
END
$preflight$;

DO $role$
BEGIN
  IF to_regrole('memory_v5_epistemic_writer') IS NULL THEN
    CREATE ROLE memory_v5_epistemic_writer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END
$role$;

ALTER ROLE memory_v5_epistemic_writer
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

DO $types$
BEGIN
  BEGIN
    CREATE TYPE memory.epistemic_target_kind_v5_1 AS ENUM (
      'claim','preference','project_knowledge','pattern_hypothesis'
    );
  EXCEPTION WHEN duplicate_object THEN NULL;
  END;
  BEGIN
    CREATE TYPE memory.epistemic_assessment_state_v5_1 AS ENUM (
      'insufficient','emerging','supported','contested','superseded','retracted'
    );
  EXCEPTION WHEN duplicate_object THEN NULL;
  END;
  BEGIN
    CREATE TYPE memory.pattern_kind_v5_1 AS ENUM (
      'recurrence','persistence','transition','trend','co_occurrence','sequence'
    );
  EXCEPTION WHEN duplicate_object THEN NULL;
  END;
  BEGIN
    CREATE TYPE memory.pattern_state_v5_1 AS ENUM (
      'insufficient','emerging','supported','contested','ended','superseded','retracted'
    );
  EXCEPTION WHEN duplicate_object THEN NULL;
  END;
  BEGIN
    CREATE TYPE memory.pattern_observation_role_v5_1 AS ENUM (
      'occurrence','counterexample','boundary','context'
    );
  EXCEPTION WHEN duplicate_object THEN NULL;
  END;
  BEGIN
    CREATE TYPE memory.epistemic_observation_role_v5_1 AS ENUM (
      'supports','opposes','qualifies','corrects'
    );
  EXCEPTION WHEN duplicate_object THEN NULL;
  END;
  BEGIN
    CREATE TYPE memory.retrieval_outcome_v5_1 AS ENUM (
      'selected','injected','explicitly_helpful','explicitly_confirmed',
      'corrected','not_relevant','caused_confusion'
    );
  EXCEPTION WHEN duplicate_object THEN NULL;
  END;
END
$types$;

DO $type_contracts$
DECLARE
  actual text[];
BEGIN
  SELECT array_agg(enumlabel::text ORDER BY enumsortorder) INTO actual
  FROM pg_enum WHERE enumtypid='memory.epistemic_target_kind_v5_1'::regtype;
  IF actual<>ARRAY['claim','preference','project_knowledge','pattern_hypothesis'] THEN
    RAISE EXCEPTION 'epistemic target kind V5.1 enum changed';
  END IF;
  SELECT array_agg(enumlabel::text ORDER BY enumsortorder) INTO actual
  FROM pg_enum WHERE enumtypid='memory.epistemic_assessment_state_v5_1'::regtype;
  IF actual<>ARRAY['insufficient','emerging','supported','contested','superseded','retracted'] THEN
    RAISE EXCEPTION 'epistemic assessment state V5.1 enum changed';
  END IF;
  SELECT array_agg(enumlabel::text ORDER BY enumsortorder) INTO actual
  FROM pg_enum WHERE enumtypid='memory.pattern_kind_v5_1'::regtype;
  IF actual<>ARRAY['recurrence','persistence','transition','trend','co_occurrence','sequence'] THEN
    RAISE EXCEPTION 'pattern kind V5.1 enum changed';
  END IF;
  SELECT array_agg(enumlabel::text ORDER BY enumsortorder) INTO actual
  FROM pg_enum WHERE enumtypid='memory.pattern_state_v5_1'::regtype;
  IF actual<>ARRAY['insufficient','emerging','supported','contested','ended','superseded','retracted'] THEN
    RAISE EXCEPTION 'pattern state V5.1 enum changed';
  END IF;
END
$type_contracts$;

CREATE TABLE IF NOT EXISTS memory.pattern_hypothesis_v5_1 (
  pattern_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  pattern_key_sha256 text NOT NULL,
  pattern_kind memory.pattern_kind_v5_1 NOT NULL,
  subject_entity_id uuid NOT NULL,
  secondary_entity_id uuid,
  predicate_family text NOT NULL,
  sensitivity memory.sensitivity_level NOT NULL,
  status memory.record_status NOT NULL DEFAULT 'active',
  current_revision_id uuid,
  revision_number integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,pattern_id),
  UNIQUE(owner_user_id,pattern_key_sha256),
  FOREIGN KEY(owner_user_id,subject_entity_id)
    REFERENCES memory.entity(owner_user_id,entity_id) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,secondary_entity_id)
    REFERENCES memory.entity(owner_user_id,entity_id) ON DELETE RESTRICT,
  CHECK (memory.v5_sha256_valid(pattern_key_sha256)),
  CHECK (predicate_family ~ '^[a-z][a-z0-9_.:-]{1,239}$'),
  CHECK (secondary_entity_id IS NULL OR secondary_entity_id<>subject_entity_id),
  CHECK (
    (current_revision_id IS NULL AND revision_number=0)
    OR (current_revision_id IS NOT NULL AND revision_number>0)
  ),
  CHECK (updated_at>=created_at)
);

CREATE TABLE IF NOT EXISTS memory.pattern_hypothesis_revision_v5_1 (
  revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  pattern_id uuid NOT NULL,
  revision_number integer NOT NULL,
  pattern_state memory.pattern_state_v5_1 NOT NULL,
  occurrence_count integer NOT NULL,
  counterexample_count integer NOT NULL,
  independent_episode_count integer NOT NULL,
  distinct_temporal_bucket_count integer NOT NULL,
  valid_from date,
  valid_to date,
  automatic_review_eligible boolean NOT NULL,
  identity_inference_forbidden boolean NOT NULL DEFAULT true,
  causal_inference_forbidden boolean NOT NULL DEFAULT true,
  reason_codes jsonb NOT NULL,
  definition_sha256 text NOT NULL,
  input_manifest_sha256 text NOT NULL,
  prior_revision_id uuid,
  method_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,pattern_id,revision_id),
  UNIQUE(owner_user_id,pattern_id,revision_number),
  UNIQUE(owner_user_id,pattern_id,input_manifest_sha256),
  FOREIGN KEY(owner_user_id,pattern_id)
    REFERENCES memory.pattern_hypothesis_v5_1(owner_user_id,pattern_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,pattern_id,prior_revision_id)
    REFERENCES memory.pattern_hypothesis_revision_v5_1(
      owner_user_id,pattern_id,revision_id
    ) ON DELETE RESTRICT,
  CHECK (revision_number>0),
  CHECK (occurrence_count>=0 AND counterexample_count>=0),
  CHECK (independent_episode_count>=0
         AND independent_episode_count<=occurrence_count),
  CHECK (distinct_temporal_bucket_count>=0),
  CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to>=valid_from),
  CHECK (identity_inference_forbidden),
  CHECK (causal_inference_forbidden),
  CHECK (memory.v5_reason_codes_valid(reason_codes,32)),
  CHECK (memory.v5_sha256_valid(definition_sha256)),
  CHECK (memory.v5_sha256_valid(input_manifest_sha256)),
  CHECK (prior_revision_id IS NULL OR prior_revision_id<>revision_id),
  CHECK (method_version ~ '^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$')
);

DO $pattern_head_fk$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='memory.pattern_hypothesis_v5_1'::regclass
      AND conname='pattern_hypothesis_v5_1_current_revision_fk'
  ) THEN
    ALTER TABLE memory.pattern_hypothesis_v5_1
      ADD CONSTRAINT pattern_hypothesis_v5_1_current_revision_fk
      FOREIGN KEY(owner_user_id,pattern_id,current_revision_id)
      REFERENCES memory.pattern_hypothesis_revision_v5_1(
        owner_user_id,pattern_id,revision_id
      ) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
  END IF;
END
$pattern_head_fk$;

CREATE TABLE IF NOT EXISTS memory.pattern_observation_link_v5_1 (
  owner_user_id uuid NOT NULL,
  pattern_id uuid NOT NULL,
  pattern_revision_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  observation_role memory.pattern_observation_role_v5_1 NOT NULL,
  episode_key_sha256 text NOT NULL,
  independence_key_sha256 text NOT NULL,
  temporal_bucket_sha256 text NOT NULL,
  observation_sha256 text NOT NULL,
  evidence_content_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(owner_user_id,pattern_revision_id,observation_id),
  FOREIGN KEY(owner_user_id,pattern_id,pattern_revision_id)
    REFERENCES memory.pattern_hypothesis_revision_v5_1(
      owner_user_id,pattern_id,revision_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,observation_id)
    REFERENCES memory.observation(owner_user_id,observation_id)
    ON DELETE RESTRICT,
  CHECK (memory.v5_sha256_valid(episode_key_sha256)),
  CHECK (memory.v5_sha256_valid(independence_key_sha256)),
  CHECK (memory.v5_sha256_valid(temporal_bucket_sha256)),
  CHECK (memory.v5_sha256_valid(observation_sha256)),
  CHECK (memory.v5_sha256_valid(evidence_content_sha256))
);

CREATE UNIQUE INDEX IF NOT EXISTS pattern_observation_link_v5_1_episode_uq
  ON memory.pattern_observation_link_v5_1(
    owner_user_id,pattern_revision_id,observation_role,episode_key_sha256
  ) WHERE observation_role IN ('occurrence','counterexample');
CREATE INDEX IF NOT EXISTS pattern_observation_link_v5_1_observation_idx
  ON memory.pattern_observation_link_v5_1(owner_user_id,observation_id);

CREATE TABLE IF NOT EXISTS memory.epistemic_target_binding_v5_1 (
  target_binding_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  target_kind memory.epistemic_target_kind_v5_1 NOT NULL,
  target_id uuid NOT NULL,
  target_revision_number integer NOT NULL,
  semantic_key_sha256 text NOT NULL,
  claim_id uuid,
  preference_id uuid,
  project_id uuid,
  project_knowledge_id uuid,
  pattern_id uuid,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,target_binding_id),
  UNIQUE(owner_user_id,target_kind,target_id,target_revision_number),
  FOREIGN KEY(owner_user_id,claim_id,target_revision_number)
    REFERENCES memory.claim_revision(owner_user_id,claim_id,revision_number)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,preference_id,target_revision_number)
    REFERENCES memory.preference_revision_v5(
      owner_user_id,preference_id,revision_number
    ) ON DELETE RESTRICT,
  FOREIGN KEY(
    owner_user_id,project_id,project_knowledge_id,target_revision_number
  ) REFERENCES memory.project_knowledge_revision_v5(
    owner_user_id,project_id,knowledge_id,revision_number
  ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,pattern_id,target_revision_number)
    REFERENCES memory.pattern_hypothesis_revision_v5_1(
      owner_user_id,pattern_id,revision_number
    ) ON DELETE RESTRICT,
  CHECK (target_revision_number>0),
  CHECK (memory.v5_sha256_valid(semantic_key_sha256)),
  CHECK (
    (target_kind='claim' AND target_id=claim_id
      AND preference_id IS NULL AND project_id IS NULL
      AND project_knowledge_id IS NULL AND pattern_id IS NULL)
    OR
    (target_kind='preference' AND target_id=preference_id
      AND claim_id IS NULL AND project_id IS NULL
      AND project_knowledge_id IS NULL AND pattern_id IS NULL)
    OR
    (target_kind='project_knowledge' AND target_id=project_knowledge_id
      AND project_id IS NOT NULL AND claim_id IS NULL
      AND preference_id IS NULL AND pattern_id IS NULL)
    OR
    (target_kind='pattern_hypothesis' AND target_id=pattern_id
      AND claim_id IS NULL AND preference_id IS NULL
      AND project_id IS NULL AND project_knowledge_id IS NULL)
  )
);

CREATE TABLE IF NOT EXISTS memory.epistemic_assessment_snapshot_v5_1 (
  snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  target_binding_id uuid NOT NULL,
  assessment_state memory.epistemic_assessment_state_v5_1 NOT NULL,
  supporting_observation_count integer NOT NULL,
  opposing_observation_count integer NOT NULL,
  qualifying_observation_count integer NOT NULL,
  corrective_observation_count integer NOT NULL,
  independent_support_cluster_count integer NOT NULL,
  independent_opposition_cluster_count integer NOT NULL,
  source_diversity_count integer NOT NULL,
  directness numeric(5,4) NOT NULL,
  source_reliability numeric(5,4) NOT NULL,
  independence numeric(5,4) NOT NULL,
  relevance numeric(5,4) NOT NULL,
  temporal_fit numeric(5,4) NOT NULL,
  specificity numeric(5,4) NOT NULL,
  extraction_quality numeric(5,4) NOT NULL,
  support_strength numeric(5,4) NOT NULL,
  opposition_strength numeric(5,4) NOT NULL,
  reason_codes jsonb NOT NULL,
  method text NOT NULL,
  method_version text NOT NULL,
  inputs_sha256 text NOT NULL,
  packet_sha256 text NOT NULL,
  prior_snapshot_id uuid,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,snapshot_id),
  UNIQUE(owner_user_id,target_binding_id,snapshot_id),
  UNIQUE(owner_user_id,target_binding_id,inputs_sha256,method_version),
  FOREIGN KEY(owner_user_id,target_binding_id)
    REFERENCES memory.epistemic_target_binding_v5_1(
      owner_user_id,target_binding_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,target_binding_id,prior_snapshot_id)
    REFERENCES memory.epistemic_assessment_snapshot_v5_1(
      owner_user_id,target_binding_id,snapshot_id
    ) ON DELETE RESTRICT,
  CHECK (supporting_observation_count>=0 AND opposing_observation_count>=0),
  CHECK (qualifying_observation_count>=0 AND corrective_observation_count>=0),
  CHECK (independent_support_cluster_count>=0
         AND independent_support_cluster_count<=supporting_observation_count),
  CHECK (independent_opposition_cluster_count>=0
         AND independent_opposition_cluster_count<=opposing_observation_count),
  CHECK (source_diversity_count>=0),
  CHECK (directness BETWEEN 0 AND 1),
  CHECK (source_reliability BETWEEN 0 AND 1),
  CHECK (independence BETWEEN 0 AND 1),
  CHECK (relevance BETWEEN 0 AND 1),
  CHECK (temporal_fit BETWEEN 0 AND 1),
  CHECK (specificity BETWEEN 0 AND 1),
  CHECK (extraction_quality BETWEEN 0 AND 1),
  CHECK (support_strength BETWEEN 0 AND 1),
  CHECK (opposition_strength BETWEEN 0 AND 1),
  CHECK (memory.v5_reason_codes_valid(reason_codes,32)),
  CHECK (method ~ '^[a-z][a-z0-9_]{1,99}$'),
  CHECK (method_version ~ '^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$'),
  CHECK (memory.v5_sha256_valid(inputs_sha256)),
  CHECK (memory.v5_sha256_valid(packet_sha256)),
  CHECK (prior_snapshot_id IS NULL OR prior_snapshot_id<>snapshot_id)
);

CREATE TABLE IF NOT EXISTS memory.epistemic_assessment_observation_link_v5_1 (
  owner_user_id uuid NOT NULL,
  snapshot_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  observation_role memory.epistemic_observation_role_v5_1 NOT NULL,
  observation_sha256 text NOT NULL,
  evidence_id uuid NOT NULL,
  evidence_content_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(owner_user_id,snapshot_id,observation_id),
  FOREIGN KEY(owner_user_id,snapshot_id)
    REFERENCES memory.epistemic_assessment_snapshot_v5_1(
      owner_user_id,snapshot_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,observation_id)
    REFERENCES memory.observation(owner_user_id,observation_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  CHECK (memory.v5_sha256_valid(observation_sha256)),
  CHECK (memory.v5_sha256_valid(evidence_content_sha256))
);

CREATE INDEX IF NOT EXISTS epistemic_assessment_observation_v5_1_obs_idx
  ON memory.epistemic_assessment_observation_link_v5_1(
    owner_user_id,observation_id
  );

CREATE TABLE IF NOT EXISTS memory.salience_feature_snapshot_v5_1 (
  feature_snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  target_binding_id uuid NOT NULL,
  importance numeric(5,4) NOT NULL,
  frequency numeric(5,4) NOT NULL,
  recency numeric(5,4) NOT NULL,
  emotional_significance numeric(5,4) NOT NULL,
  goal_relevance numeric(5,4) NOT NULL,
  future_utility numeric(5,4) NOT NULL,
  retrieval_utility numeric(5,4) NOT NULL,
  contradiction_pressure numeric(5,4) NOT NULL,
  eligible_count integer NOT NULL,
  selected_count integer NOT NULL,
  injected_count integer NOT NULL,
  explicitly_helpful_count integer NOT NULL,
  explicitly_confirmed_count integer NOT NULL,
  corrected_count integer NOT NULL,
  not_relevant_count integer NOT NULL,
  caused_confusion_count integer NOT NULL,
  last_retrieval_trace_id uuid,
  as_of_bucket date NOT NULL,
  method_version text NOT NULL,
  signal_manifest_sha256 text NOT NULL,
  packet_sha256 text NOT NULL,
  prior_feature_snapshot_id uuid,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,feature_snapshot_id),
  UNIQUE(owner_user_id,target_binding_id,feature_snapshot_id),
  UNIQUE(
    owner_user_id,target_binding_id,as_of_bucket,
    method_version,signal_manifest_sha256
  ),
  FOREIGN KEY(owner_user_id,target_binding_id)
    REFERENCES memory.epistemic_target_binding_v5_1(
      owner_user_id,target_binding_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,last_retrieval_trace_id)
    REFERENCES memory.retrieval_trace(owner_user_id,trace_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,target_binding_id,prior_feature_snapshot_id)
    REFERENCES memory.salience_feature_snapshot_v5_1(
      owner_user_id,target_binding_id,feature_snapshot_id
    ) ON DELETE RESTRICT,
  CHECK (importance BETWEEN 0 AND 1),
  CHECK (frequency BETWEEN 0 AND 1),
  CHECK (recency BETWEEN 0 AND 1),
  CHECK (emotional_significance BETWEEN 0 AND 1),
  CHECK (goal_relevance BETWEEN 0 AND 1),
  CHECK (future_utility BETWEEN 0 AND 1),
  CHECK (retrieval_utility BETWEEN 0 AND 1),
  CHECK (contradiction_pressure BETWEEN 0 AND 1),
  CHECK (eligible_count>=0 AND selected_count>=0 AND injected_count>=0),
  CHECK (explicitly_helpful_count>=0 AND explicitly_confirmed_count>=0),
  CHECK (corrected_count>=0 AND not_relevant_count>=0
         AND caused_confusion_count>=0),
  CHECK (selected_count<=eligible_count),
  CHECK (injected_count<=selected_count),
  CHECK (explicitly_helpful_count+explicitly_confirmed_count+corrected_count
         +not_relevant_count+caused_confusion_count<=injected_count),
  CHECK (method_version='memory_v1_salience_features_v5_1'),
  CHECK (memory.v5_sha256_valid(signal_manifest_sha256)),
  CHECK (memory.v5_sha256_valid(packet_sha256)),
  CHECK (prior_feature_snapshot_id IS NULL
         OR prior_feature_snapshot_id<>feature_snapshot_id)
);

CREATE TABLE IF NOT EXISTS memory.retrieval_outcome_signal_v5_1 (
  signal_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  trace_id uuid NOT NULL,
  target_binding_id uuid NOT NULL,
  outcome memory.retrieval_outcome_v5_1 NOT NULL,
  source_evidence_id uuid,
  signal_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,signal_id),
  UNIQUE(owner_user_id,trace_id,target_binding_id,outcome,signal_sha256),
  FOREIGN KEY(owner_user_id,trace_id)
    REFERENCES memory.retrieval_trace(owner_user_id,trace_id)
    ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,target_binding_id)
    REFERENCES memory.epistemic_target_binding_v5_1(
      owner_user_id,target_binding_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,source_evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  CHECK (memory.v5_sha256_valid(signal_sha256)),
  CHECK (
    source_evidence_id IS NULL
    OR outcome IN ('explicitly_confirmed','corrected')
  )
);

CREATE TABLE IF NOT EXISTS memory.epistemic_operation_request_v5_1 (
  owner_user_id uuid NOT NULL,
  request_id uuid NOT NULL,
  operation text NOT NULL,
  manifest_sha256 text NOT NULL,
  result jsonb NOT NULL,
  invoked_by_session name NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(owner_user_id,request_id),
  UNIQUE(owner_user_id,operation,manifest_sha256),
  CHECK (operation IN ('persist_snapshot_packet','record_retrieval_outcome')),
  CHECK (memory.v5_sha256_valid(manifest_sha256)),
  CHECK (jsonb_typeof(result)='object' AND pg_column_size(result)<=16384)
);

CREATE INDEX IF NOT EXISTS pattern_hypothesis_v5_1_owner_state_idx
  ON memory.pattern_hypothesis_v5_1(owner_user_id,status,updated_at DESC);
CREATE INDEX IF NOT EXISTS pattern_hypothesis_v5_1_owner_subject_idx
  ON memory.pattern_hypothesis_v5_1(
    owner_user_id,subject_entity_id,pattern_kind,predicate_family
  );
CREATE INDEX IF NOT EXISTS pattern_hypothesis_v5_1_secondary_idx
  ON memory.pattern_hypothesis_v5_1(owner_user_id,secondary_entity_id)
  WHERE secondary_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS pattern_revision_v5_1_owner_time_idx
  ON memory.pattern_hypothesis_revision_v5_1(
    owner_user_id,pattern_id,created_at DESC
  );
CREATE INDEX IF NOT EXISTS epistemic_target_v5_1_owner_kind_idx
  ON memory.epistemic_target_binding_v5_1(
    owner_user_id,target_kind,target_id,target_revision_number DESC
  );
CREATE INDEX IF NOT EXISTS epistemic_assessment_v5_1_owner_target_idx
  ON memory.epistemic_assessment_snapshot_v5_1(
    owner_user_id,target_binding_id,created_at DESC
  );
CREATE INDEX IF NOT EXISTS salience_feature_v5_1_owner_target_idx
  ON memory.salience_feature_snapshot_v5_1(
    owner_user_id,target_binding_id,as_of_bucket DESC,created_at DESC
  );
CREATE INDEX IF NOT EXISTS retrieval_outcome_v5_1_owner_target_idx
  ON memory.retrieval_outcome_signal_v5_1(
    owner_user_id,target_binding_id,created_at DESC
  );
CREATE INDEX IF NOT EXISTS retrieval_outcome_v5_1_owner_trace_idx
  ON memory.retrieval_outcome_signal_v5_1(
    owner_user_id,trace_id,created_at DESC
  );
CREATE INDEX IF NOT EXISTS epistemic_operation_v5_1_owner_time_idx
  ON memory.epistemic_operation_request_v5_1(owner_user_id,created_at DESC);

DO $append_only$
DECLARE
  relation_name text;
BEGIN
  FOREACH relation_name IN ARRAY ARRAY[
    'pattern_hypothesis_revision_v5_1',
    'pattern_observation_link_v5_1',
    'epistemic_target_binding_v5_1',
    'epistemic_assessment_snapshot_v5_1',
    'epistemic_assessment_observation_link_v5_1',
    'salience_feature_snapshot_v5_1',
    'retrieval_outcome_signal_v5_1',
    'epistemic_operation_request_v5_1'
  ]
  LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON memory.%I',
      relation_name||'_append_only_guard',relation_name);
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON memory.%I '
      'FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_append_only()',
      relation_name||'_append_only_guard',relation_name
    );
  END LOOP;
END
$append_only$;

DO $rls$
DECLARE
  relation_name text;
BEGIN
  FOREACH relation_name IN ARRAY ARRAY[
    'pattern_hypothesis_v5_1',
    'pattern_hypothesis_revision_v5_1',
    'pattern_observation_link_v5_1',
    'epistemic_target_binding_v5_1',
    'epistemic_assessment_snapshot_v5_1',
    'epistemic_assessment_observation_link_v5_1',
    'salience_feature_snapshot_v5_1',
    'retrieval_outcome_signal_v5_1',
    'epistemic_operation_request_v5_1'
  ]
  LOOP
    EXECUTE format('ALTER TABLE memory.%I ENABLE ROW LEVEL SECURITY',relation_name);
    EXECUTE format('ALTER TABLE memory.%I FORCE ROW LEVEL SECURITY',relation_name);
    EXECUTE format('DROP POLICY IF EXISTS owner_isolation ON memory.%I',relation_name);
    EXECUTE format(
      'CREATE POLICY owner_isolation ON memory.%I '
      'FOR ALL TO memory_v5_epistemic_writer '
      'USING (owner_user_id=(SELECT memory.current_actor_user_id())) '
      'WITH CHECK (owner_user_id=(SELECT memory.current_actor_user_id()))',
      relation_name
    );
    EXECUTE format('REVOKE ALL ON memory.%I FROM PUBLIC,brains_app',relation_name);
  END LOOP;
END
$rls$;

DO $reference_policies$
DECLARE
  relation_name text;
BEGIN
  FOREACH relation_name IN ARRAY ARRAY[
    'entity','evidence','claim','claim_revision','observation',
    'preference_head_v5','preference_revision_v5',
    'project_knowledge_head_v5','project_knowledge_revision_v5',
    'retrieval_trace'
  ]
  LOOP
    EXECUTE format(
      'DROP POLICY IF EXISTS epistemic_v5_1_reference_read ON memory.%I',
      relation_name
    );
    EXECUTE format(
      'CREATE POLICY epistemic_v5_1_reference_read ON memory.%I '
      'FOR SELECT TO memory_v5_epistemic_writer '
      'USING (owner_user_id=(SELECT memory.current_actor_user_id()))',
      relation_name
    );
  END LOOP;
END
$reference_policies$;

CREATE OR REPLACE FUNCTION memory.require_v5_epistemic_writer_context()
RETURNS uuid
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'epistemic V5.1 APIs require a brains_app session'
      USING ERRCODE='42501';
  END IF;
  actor:=memory.current_actor_user_id();
  IF actor IS NULL THEN
    RAISE EXCEPTION 'epistemic V5.1 APIs require an actor user ID'
      USING ERRCODE='42501';
  END IF;
  RETURN actor;
END
$function$;

CREATE OR REPLACE FUNCTION memory.epistemic_packet_sha256_v5_1(
  packet jsonb
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY INVOKER
SET search_path=''
AS $function$
  SELECT memory.v5_digest_text(
    memory.v5_canonical_json_text(packet-'packet_sha256')
  )
$function$;

CREATE OR REPLACE FUNCTION memory.persist_epistemic_snapshot_packet_v5_1(
  p_request_id uuid,
  p_packet jsonb
)
RETURNS TABLE(
  target_binding_id uuid,
  assessment_snapshot_id uuid,
  feature_snapshot_id uuid,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  packet_sha text;
  target jsonb;
  assessment jsonb;
  dimensions jsonb;
  pattern_assessment jsonb;
  pattern_present boolean;
  salience jsonb;
  history jsonb;
  target_kind_value memory.epistemic_target_kind_v5_1;
  target_id_value uuid;
  target_revision_number_value integer;
  target_project_id uuid;
  semantic_key text;
  binding memory.epistemic_target_binding_v5_1%ROWTYPE;
  snapshot_id_value uuid;
  feature_id_value uuid;
  prior_snapshot uuid;
  prior_feature uuid;
  last_trace uuid;
  replayed memory.epistemic_operation_request_v5_1%ROWTYPE;
  observation_ids jsonb;
  link_role memory.epistemic_observation_role_v5_1;
  observation_id_value uuid;
  observation_row record;
  stored_result jsonb;
BEGIN
  actor:=memory.require_v5_epistemic_writer_context();
  IF p_request_id IS NULL OR jsonb_typeof(p_packet)<>'object' THEN
    RAISE EXCEPTION 'epistemic V5.1 snapshot inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  IF NOT memory.v5_jsonb_exact_keys(p_packet,ARRAY[
    'contract_version','policy_version','source_registry_version','target',
    'evidence_assessment','pattern_assessment','salience_features',
    'retrieval_history','reason_codes','input_manifest_sha256','packet_sha256'
  ])
     OR p_packet->>'contract_version'<>
       'memory_v1_epistemic_pattern_salience_v5_1'
     OR p_packet->>'policy_version'<>
       'memory_v1_epistemic_pattern_salience_policy_v5_1'
     OR p_packet->>'source_registry_version'<>'memory_predicate_registry_v5_1'
     OR NOT memory.v5_sha256_valid(p_packet->>'input_manifest_sha256')
     OR NOT memory.v5_sha256_valid(p_packet->>'packet_sha256')
     OR NOT memory.v5_reason_codes_valid(p_packet->'reason_codes',32) THEN
    RAISE EXCEPTION 'epistemic V5.1 packet envelope is invalid'
      USING ERRCODE='22023';
  END IF;
  packet_sha:=memory.epistemic_packet_sha256_v5_1(p_packet);
  IF packet_sha<>p_packet->>'packet_sha256' THEN
    RAISE EXCEPTION 'epistemic V5.1 packet hash mismatch'
      USING ERRCODE='23514';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_epistemic_v5_1',actor::text,packet_sha),0
  ));
  SELECT request.* INTO replayed
  FROM memory.epistemic_operation_request_v5_1 AS request
  WHERE request.owner_user_id=actor
    AND (request.request_id=p_request_id OR (
      request.operation='persist_snapshot_packet'
      AND request.manifest_sha256=packet_sha
    ))
  ORDER BY (request.request_id=p_request_id) DESC
  LIMIT 1;
  IF FOUND THEN
    IF replayed.operation<>'persist_snapshot_packet'
       OR replayed.manifest_sha256<>packet_sha THEN
      RAISE EXCEPTION 'epistemic V5.1 request replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT
      (replayed.result->>'target_binding_id')::uuid,
      (replayed.result->>'assessment_snapshot_id')::uuid,
      (replayed.result->>'feature_snapshot_id')::uuid,
      'replayed'::text;
    RETURN;
  END IF;

  target:=p_packet->'target';
  assessment:=p_packet->'evidence_assessment';
  dimensions:=assessment->'dimensions';
  pattern_assessment:=p_packet->'pattern_assessment';
  pattern_present:=jsonb_typeof(pattern_assessment)='object';
  salience:=p_packet->'salience_features';
  history:=p_packet->'retrieval_history';
  IF NOT memory.v5_jsonb_exact_keys(target,ARRAY[
       'target_kind','target_id','target_revision_number','semantic_key_sha256'
     ])
     OR NOT memory.v5_jsonb_exact_keys(assessment,ARRAY[
       'assessment_state','supporting_observation_count',
       'opposing_observation_count','qualifying_observation_count',
       'corrective_observation_count','independent_support_cluster_count',
       'independent_opposition_cluster_count','source_diversity_count',
       'supporting_observation_ids','opposing_observation_ids',
       'qualifying_observation_ids','corrective_observation_ids',
       'dimensions','method','method_version','inputs_sha256'
     ])
     OR NOT memory.v5_jsonb_exact_keys(dimensions,ARRAY[
       'directness','source_reliability','independence','relevance',
       'temporal_fit','specificity','extraction_quality','support_strength',
       'opposition_strength'
     ])
     OR NOT memory.v5_jsonb_exact_keys(salience,ARRAY[
       'importance','frequency','recency','emotional_significance',
       'goal_relevance','future_utility','retrieval_utility',
       'contradiction_pressure','as_of_date','method_version',
       'signal_manifest_sha256'
     ])
     OR NOT memory.v5_jsonb_exact_keys(history,ARRAY[
       'eligible_count','selected_count','injected_count',
       'explicitly_helpful_count','explicitly_confirmed_count',
       'corrected_count','not_relevant_count','caused_confusion_count',
       'last_retrieval_trace_id'
     ]) THEN
    RAISE EXCEPTION 'epistemic V5.1 packet shape is invalid'
      USING ERRCODE='22023';
  END IF;
  IF jsonb_typeof(pattern_assessment) NOT IN ('null','object')
     OR (pattern_present
     AND NOT memory.v5_jsonb_exact_keys(pattern_assessment,ARRAY[
       'pattern_kind','pattern_state','occurrence_count','counterexample_count',
       'independent_episode_count','distinct_temporal_bucket_count',
       'temporal_basis_observation_ids','automatic_review_eligible',
       'identity_inference_forbidden','causal_inference_forbidden','reason_codes'
     ])) THEN
    RAISE EXCEPTION 'epistemic V5.1 pattern assessment shape is invalid'
      USING ERRCODE='22023';
  END IF;

  target_kind_value:=(target->>'target_kind')::memory.epistemic_target_kind_v5_1;
  target_id_value:=(target->>'target_id')::uuid;
  target_revision_number_value:=(target->>'target_revision_number')::integer;
  semantic_key:=target->>'semantic_key_sha256';
  IF target_revision_number_value<1
     OR NOT memory.v5_sha256_valid(semantic_key)
     OR (target_kind_value='pattern_hypothesis')<>pattern_present
     OR (pattern_present AND (
       pattern_assessment->>'identity_inference_forbidden'<>'true'
       OR pattern_assessment->>'causal_inference_forbidden'<>'true'
       OR NOT memory.v5_reason_codes_valid(
         pattern_assessment->'reason_codes',32
       )
     )) THEN
    RAISE EXCEPTION 'epistemic V5.1 target or pattern binding is invalid'
      USING ERRCODE='22023';
  END IF;

  CASE target_kind_value
    WHEN 'claim' THEN
      PERFORM 1
      FROM memory.claim AS head
      JOIN memory.claim_revision AS revision
        ON revision.owner_user_id=head.owner_user_id
       AND revision.claim_id=head.claim_id
      WHERE head.owner_user_id=actor
        AND head.claim_id=target_id_value
        AND revision.revision_number=target_revision_number_value
        AND memory.v5_digest_text(head.canonical_key)=semantic_key;
    WHEN 'preference' THEN
      PERFORM 1
      FROM memory.preference_head_v5 AS head
      JOIN memory.preference_revision_v5 AS revision
        ON revision.owner_user_id=head.owner_user_id
       AND revision.preference_id=head.preference_id
      WHERE head.owner_user_id=actor
        AND head.preference_id=target_id_value
        AND revision.revision_number=target_revision_number_value
        AND head.semantic_key_sha256=semantic_key;
    WHEN 'project_knowledge' THEN
      SELECT head.project_id INTO target_project_id
      FROM memory.project_knowledge_head_v5 AS head
      JOIN memory.project_knowledge_revision_v5 AS revision
        ON revision.owner_user_id=head.owner_user_id
       AND revision.project_id=head.project_id
       AND revision.knowledge_id=head.knowledge_id
      WHERE head.owner_user_id=actor
        AND head.knowledge_id=target_id_value
        AND revision.revision_number=target_revision_number_value
        AND head.semantic_key_sha256=semantic_key;
    WHEN 'pattern_hypothesis' THEN
      PERFORM 1
      FROM memory.pattern_hypothesis_v5_1 AS head
      JOIN memory.pattern_hypothesis_revision_v5_1 AS revision
        ON revision.owner_user_id=head.owner_user_id
       AND revision.pattern_id=head.pattern_id
      WHERE head.owner_user_id=actor
        AND head.pattern_id=target_id_value
        AND revision.revision_number=target_revision_number_value
        AND head.pattern_key_sha256=semantic_key;
  END CASE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped epistemic V5.1 target revision is absent'
      USING ERRCODE='23514';
  END IF;

  INSERT INTO memory.epistemic_target_binding_v5_1(
    owner_user_id,target_kind,target_id,target_revision_number,
    semantic_key_sha256,claim_id,preference_id,project_id,
    project_knowledge_id,pattern_id
  ) VALUES (
    actor,target_kind_value,target_id_value,target_revision_number_value,
    semantic_key,
    CASE WHEN target_kind_value='claim' THEN target_id_value END,
    CASE WHEN target_kind_value='preference' THEN target_id_value END,
    target_project_id,
    CASE WHEN target_kind_value='project_knowledge' THEN target_id_value END,
    CASE WHEN target_kind_value='pattern_hypothesis' THEN target_id_value END
  ) ON CONFLICT(owner_user_id,target_kind,target_id,target_revision_number)
    DO NOTHING;
  SELECT value.* INTO binding
  FROM memory.epistemic_target_binding_v5_1 AS value
  WHERE value.owner_user_id=actor
    AND value.target_kind=target_kind_value
    AND value.target_id=target_id_value
    AND value.target_revision_number=target_revision_number_value;
  IF binding.semantic_key_sha256<>semantic_key THEN
    RAISE EXCEPTION 'epistemic V5.1 target binding conflicts'
      USING ERRCODE='23514';
  END IF;

  IF jsonb_typeof(assessment->'supporting_observation_ids')<>'array'
     OR jsonb_typeof(assessment->'opposing_observation_ids')<>'array'
     OR jsonb_typeof(assessment->'qualifying_observation_ids')<>'array'
     OR jsonb_typeof(assessment->'corrective_observation_ids')<>'array'
     OR jsonb_array_length(assessment->'supporting_observation_ids')<>
       (assessment->>'supporting_observation_count')::integer
     OR jsonb_array_length(assessment->'opposing_observation_ids')<>
       (assessment->>'opposing_observation_count')::integer
     OR jsonb_array_length(assessment->'qualifying_observation_ids')<>
       (assessment->>'qualifying_observation_count')::integer
     OR jsonb_array_length(assessment->'corrective_observation_ids')<>
       (assessment->>'corrective_observation_count')::integer
     OR assessment->>'inputs_sha256'<>p_packet->>'input_manifest_sha256' THEN
    RAISE EXCEPTION 'epistemic V5.1 observation manifest is invalid'
      USING ERRCODE='23514';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_epistemic_target_v5_1',actor::text,
      binding.target_binding_id::text),0
  ));
  SELECT value.snapshot_id INTO prior_snapshot
  FROM memory.epistemic_assessment_snapshot_v5_1 AS value
  WHERE value.owner_user_id=actor
    AND value.target_binding_id=binding.target_binding_id
  ORDER BY value.created_at DESC,value.snapshot_id DESC LIMIT 1;
  snapshot_id_value:=gen_random_uuid();
  INSERT INTO memory.epistemic_assessment_snapshot_v5_1(
    snapshot_id,owner_user_id,target_binding_id,assessment_state,
    supporting_observation_count,opposing_observation_count,
    qualifying_observation_count,corrective_observation_count,
    independent_support_cluster_count,independent_opposition_cluster_count,
    source_diversity_count,directness,source_reliability,independence,
    relevance,temporal_fit,specificity,extraction_quality,support_strength,
    opposition_strength,reason_codes,method,method_version,inputs_sha256,
    packet_sha256,prior_snapshot_id
  ) VALUES (
    snapshot_id_value,actor,binding.target_binding_id,
    (assessment->>'assessment_state')::memory.epistemic_assessment_state_v5_1,
    (assessment->>'supporting_observation_count')::integer,
    (assessment->>'opposing_observation_count')::integer,
    (assessment->>'qualifying_observation_count')::integer,
    (assessment->>'corrective_observation_count')::integer,
    (assessment->>'independent_support_cluster_count')::integer,
    (assessment->>'independent_opposition_cluster_count')::integer,
    (assessment->>'source_diversity_count')::integer,
    (dimensions->>'directness')::numeric,
    (dimensions->>'source_reliability')::numeric,
    (dimensions->>'independence')::numeric,
    (dimensions->>'relevance')::numeric,
    (dimensions->>'temporal_fit')::numeric,
    (dimensions->>'specificity')::numeric,
    (dimensions->>'extraction_quality')::numeric,
    (dimensions->>'support_strength')::numeric,
    (dimensions->>'opposition_strength')::numeric,
    p_packet->'reason_codes',assessment->>'method',
    assessment->>'method_version',assessment->>'inputs_sha256',
    packet_sha,prior_snapshot
  );

  FOR link_role,observation_ids IN
    SELECT value.link_role,value.observation_ids
    FROM (VALUES
      ('supports'::memory.epistemic_observation_role_v5_1,
        assessment->'supporting_observation_ids'),
      ('opposes'::memory.epistemic_observation_role_v5_1,
        assessment->'opposing_observation_ids'),
      ('qualifies'::memory.epistemic_observation_role_v5_1,
        assessment->'qualifying_observation_ids'),
      ('corrects'::memory.epistemic_observation_role_v5_1,
        assessment->'corrective_observation_ids')
    ) AS value(link_role,observation_ids)
  LOOP
    FOR observation_id_value IN
      SELECT item.value::uuid
      FROM jsonb_array_elements_text(observation_ids) AS item(value)
    LOOP
      SELECT observation.observation_sha256,observation.evidence_id,
             evidence.content_sha256
        INTO observation_row
      FROM memory.observation AS observation
      JOIN memory.evidence AS evidence
        ON evidence.owner_user_id=observation.owner_user_id
       AND evidence.evidence_id=observation.evidence_id
      WHERE observation.owner_user_id=actor
        AND observation.observation_id=observation_id_value
        AND evidence.status='active';
      IF NOT FOUND
         OR NOT memory.v5_sha256_valid(observation_row.observation_sha256)
         OR NOT memory.v5_sha256_valid(observation_row.content_sha256) THEN
        RAISE EXCEPTION 'owner-scoped assessment observation is invalid'
          USING ERRCODE='23514';
      END IF;
      INSERT INTO memory.epistemic_assessment_observation_link_v5_1(
        owner_user_id,snapshot_id,observation_id,observation_role,
        observation_sha256,evidence_id,evidence_content_sha256
      ) VALUES (
        actor,snapshot_id_value,observation_id_value,link_role,
        observation_row.observation_sha256,observation_row.evidence_id,
        observation_row.content_sha256
      );
    END LOOP;
  END LOOP;

  last_trace:=NULLIF(history->>'last_retrieval_trace_id','')::uuid;
  IF last_trace IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM memory.retrieval_trace AS trace
    WHERE trace.owner_user_id=actor AND trace.trace_id=last_trace
  ) THEN
    RAISE EXCEPTION 'owner-scoped last retrieval trace is absent'
      USING ERRCODE='23514';
  END IF;
  IF salience->>'method_version'<>'memory_v1_salience_features_v5_1'
     OR NOT memory.v5_sha256_valid(salience->>'signal_manifest_sha256') THEN
    RAISE EXCEPTION 'epistemic V5.1 salience contract is invalid'
      USING ERRCODE='22023';
  END IF;
  SELECT value.feature_snapshot_id INTO prior_feature
  FROM memory.salience_feature_snapshot_v5_1 AS value
  WHERE value.owner_user_id=actor
    AND value.target_binding_id=binding.target_binding_id
  ORDER BY value.created_at DESC,value.feature_snapshot_id DESC LIMIT 1;
  feature_id_value:=gen_random_uuid();
  INSERT INTO memory.salience_feature_snapshot_v5_1(
    feature_snapshot_id,owner_user_id,target_binding_id,
    importance,frequency,recency,emotional_significance,goal_relevance,
    future_utility,retrieval_utility,contradiction_pressure,
    eligible_count,selected_count,injected_count,explicitly_helpful_count,
    explicitly_confirmed_count,corrected_count,not_relevant_count,
    caused_confusion_count,last_retrieval_trace_id,as_of_bucket,
    method_version,signal_manifest_sha256,packet_sha256,
    prior_feature_snapshot_id
  ) VALUES (
    feature_id_value,actor,binding.target_binding_id,
    (salience->>'importance')::numeric,(salience->>'frequency')::numeric,
    (salience->>'recency')::numeric,
    (salience->>'emotional_significance')::numeric,
    (salience->>'goal_relevance')::numeric,
    (salience->>'future_utility')::numeric,
    (salience->>'retrieval_utility')::numeric,
    (salience->>'contradiction_pressure')::numeric,
    (history->>'eligible_count')::integer,
    (history->>'selected_count')::integer,
    (history->>'injected_count')::integer,
    (history->>'explicitly_helpful_count')::integer,
    (history->>'explicitly_confirmed_count')::integer,
    (history->>'corrected_count')::integer,
    (history->>'not_relevant_count')::integer,
    (history->>'caused_confusion_count')::integer,last_trace,
    (salience->>'as_of_date')::date,salience->>'method_version',
    salience->>'signal_manifest_sha256',packet_sha,prior_feature
  );

  stored_result:=jsonb_build_object(
    'target_binding_id',binding.target_binding_id::text,
    'assessment_snapshot_id',snapshot_id_value::text,
    'feature_snapshot_id',feature_id_value::text
  );
  INSERT INTO memory.epistemic_operation_request_v5_1(
    owner_user_id,request_id,operation,manifest_sha256,result,invoked_by_session
  ) VALUES (
    actor,p_request_id,'persist_snapshot_packet',packet_sha,
    stored_result,session_user
  );
  RETURN QUERY SELECT binding.target_binding_id,snapshot_id_value,
    feature_id_value,'applied'::text;
END
$function$;

CREATE OR REPLACE FUNCTION memory.record_retrieval_outcome_signal_v5_1(
  p_request_id uuid,
  p_trace_id uuid,
  p_target_binding_id uuid,
  p_outcome memory.retrieval_outcome_v5_1,
  p_source_evidence_id uuid,
  p_expected_signal_sha256 text
)
RETURNS TABLE(
  signal_id uuid,
  apply_outcome text
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path=pg_catalog
AS $function$
DECLARE
  actor uuid;
  calculated_sha text;
  signal_id_value uuid;
  replayed memory.epistemic_operation_request_v5_1%ROWTYPE;
BEGIN
  actor:=memory.require_v5_epistemic_writer_context();
  IF p_request_id IS NULL OR p_trace_id IS NULL
     OR p_target_binding_id IS NULL OR p_outcome IS NULL
     OR NOT memory.v5_sha256_valid(p_expected_signal_sha256)
     OR (p_source_evidence_id IS NOT NULL
         AND p_outcome NOT IN ('explicitly_confirmed','corrected')) THEN
    RAISE EXCEPTION 'retrieval outcome V5.1 inputs are invalid'
      USING ERRCODE='22023';
  END IF;
  calculated_sha:=memory.v5_digest_text(memory.v5_canonical_json_text(
    jsonb_build_object(
      'contract_version','memory_v1_retrieval_outcome_signal_v5_1',
      'owner_user_id',actor::text,
      'trace_id',p_trace_id::text,
      'target_binding_id',p_target_binding_id::text,
      'outcome',p_outcome::text,
      'source_evidence_id',
        CASE WHEN p_source_evidence_id IS NULL THEN NULL
             ELSE p_source_evidence_id::text END
    )
  ));
  IF calculated_sha<>p_expected_signal_sha256 THEN
    RAISE EXCEPTION 'retrieval outcome V5.1 hash mismatch'
      USING ERRCODE='23514';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    concat_ws('|','memory_retrieval_outcome_v5_1',actor::text,calculated_sha),0
  ));
  SELECT request.* INTO replayed
  FROM memory.epistemic_operation_request_v5_1 AS request
  WHERE request.owner_user_id=actor
    AND (request.request_id=p_request_id OR (
      request.operation='record_retrieval_outcome'
      AND request.manifest_sha256=calculated_sha
    ))
  ORDER BY (request.request_id=p_request_id) DESC
  LIMIT 1;
  IF FOUND THEN
    IF replayed.operation<>'record_retrieval_outcome'
       OR replayed.manifest_sha256<>calculated_sha THEN
      RAISE EXCEPTION 'retrieval outcome V5.1 request replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT (replayed.result->>'signal_id')::uuid,'replayed'::text;
    RETURN;
  END IF;
  IF NOT EXISTS (
       SELECT 1 FROM memory.retrieval_trace AS trace
       WHERE trace.owner_user_id=actor AND trace.trace_id=p_trace_id
     )
     OR NOT EXISTS (
       SELECT 1 FROM memory.epistemic_target_binding_v5_1 AS target
       WHERE target.owner_user_id=actor
         AND target.target_binding_id=p_target_binding_id
     )
     OR (p_source_evidence_id IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM memory.evidence AS evidence
       WHERE evidence.owner_user_id=actor
         AND evidence.evidence_id=p_source_evidence_id
         AND evidence.status='active'
     )) THEN
    RAISE EXCEPTION 'owner-scoped retrieval outcome authority is absent'
      USING ERRCODE='23514';
  END IF;
  signal_id_value:=gen_random_uuid();
  INSERT INTO memory.retrieval_outcome_signal_v5_1(
    signal_id,owner_user_id,trace_id,target_binding_id,outcome,
    source_evidence_id,signal_sha256
  ) VALUES (
    signal_id_value,actor,p_trace_id,p_target_binding_id,p_outcome,
    p_source_evidence_id,calculated_sha
  );
  INSERT INTO memory.epistemic_operation_request_v5_1(
    owner_user_id,request_id,operation,manifest_sha256,result,invoked_by_session
  ) VALUES (
    actor,p_request_id,'record_retrieval_outcome',calculated_sha,
    jsonb_build_object('signal_id',signal_id_value::text),session_user
  );
  RETURN QUERY SELECT signal_id_value,'applied'::text;
END
$function$;

GRANT USAGE ON SCHEMA memory TO memory_v5_epistemic_writer;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_epistemic_writer;
GRANT EXECUTE ON FUNCTION memory.v5_sha256_valid(text)
  TO memory_v5_epistemic_writer;
GRANT EXECUTE ON FUNCTION memory.v5_reason_codes_valid(jsonb,integer)
  TO memory_v5_epistemic_writer;
GRANT EXECUTE ON FUNCTION memory.v5_jsonb_exact_keys(jsonb,text[])
  TO memory_v5_epistemic_writer;
GRANT EXECUTE ON FUNCTION memory.v5_canonical_json_text(jsonb)
  TO memory_v5_epistemic_writer;
GRANT EXECUTE ON FUNCTION memory.v5_digest_text(text)
  TO memory_v5_epistemic_writer;
GRANT EXECUTE ON FUNCTION memory.require_v5_epistemic_writer_context()
  TO memory_v5_epistemic_writer;
GRANT EXECUTE ON FUNCTION memory.epistemic_packet_sha256_v5_1(jsonb)
  TO memory_v5_epistemic_writer;

GRANT SELECT ON
  memory.entity,
  memory.evidence,
  memory.claim,
  memory.claim_revision,
  memory.observation,
  memory.preference_head_v5,
  memory.preference_revision_v5,
  memory.project_knowledge_head_v5,
  memory.project_knowledge_revision_v5,
  memory.retrieval_trace,
  memory.pattern_hypothesis_v5_1,
  memory.pattern_hypothesis_revision_v5_1,
  memory.epistemic_target_binding_v5_1,
  memory.epistemic_assessment_snapshot_v5_1,
  memory.salience_feature_snapshot_v5_1,
  memory.epistemic_operation_request_v5_1
TO memory_v5_epistemic_writer;

GRANT INSERT ON
  memory.epistemic_target_binding_v5_1,
  memory.epistemic_assessment_snapshot_v5_1,
  memory.epistemic_assessment_observation_link_v5_1,
  memory.salience_feature_snapshot_v5_1,
  memory.retrieval_outcome_signal_v5_1,
  memory.epistemic_operation_request_v5_1
TO memory_v5_epistemic_writer;

ALTER FUNCTION memory.require_v5_epistemic_writer_context()
  OWNER TO memory_v5_epistemic_writer;
ALTER FUNCTION memory.epistemic_packet_sha256_v5_1(jsonb)
  OWNER TO memory_v5_epistemic_writer;
ALTER FUNCTION memory.persist_epistemic_snapshot_packet_v5_1(uuid,jsonb)
  OWNER TO memory_v5_epistemic_writer;
ALTER FUNCTION memory.record_retrieval_outcome_signal_v5_1(
  uuid,uuid,uuid,memory.retrieval_outcome_v5_1,uuid,text
) OWNER TO memory_v5_epistemic_writer;

REVOKE ALL ON FUNCTION memory.require_v5_epistemic_writer_context()
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.epistemic_packet_sha256_v5_1(jsonb)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.persist_epistemic_snapshot_packet_v5_1(uuid,jsonb)
  FROM PUBLIC,brains_app;
REVOKE ALL ON FUNCTION memory.record_retrieval_outcome_signal_v5_1(
  uuid,uuid,uuid,memory.retrieval_outcome_v5_1,uuid,text
) FROM PUBLIC,brains_app;

GRANT EXECUTE ON FUNCTION memory.persist_epistemic_snapshot_packet_v5_1(
  uuid,jsonb
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.record_retrieval_outcome_signal_v5_1(
  uuid,uuid,uuid,memory.retrieval_outcome_v5_1,uuid,text
) TO brains_app;

COMMIT;
