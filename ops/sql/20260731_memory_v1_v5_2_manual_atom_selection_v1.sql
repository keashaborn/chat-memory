BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF session_user<>'sage'
     OR to_regrole('memory_v5_2_atom_admission_maintainer') IS NULL
     OR to_regprocedure('memory.plan_owner_v5_2_atom_admission_v2(uuid)') IS NULL
     OR to_regprocedure('memory.v5_2_atom_proposal_sha256_v1(jsonb)') IS NULL
     OR to_regprocedure('memory.authoritative_owner_v5_2_packet_id_v1(uuid)') IS NULL
     OR to_regclass('memory.evidence_extraction_packet_v5_local') IS NULL
     OR to_regclass('memory.v5_2_local_packet_route_event') IS NULL THEN
    RAISE EXCEPTION 'manual atom selection prerequisites are absent';
  END IF;
  IF to_regprocedure(
       'memory.plan_owner_v5_2_atom_admission_v2_before_manual_selection_v1(uuid)'
     ) IS NOT NULL
     OR to_regclass('memory.v5_2_manual_atom_selection_v1') IS NOT NULL THEN
    RAISE EXCEPTION 'manual atom selection is already installed';
  END IF;
END
$preflight$;

ALTER FUNCTION memory.plan_owner_v5_2_atom_admission_v2(uuid)
  RENAME TO plan_owner_v5_2_atom_admission_v2_before_manual_selection_v1;

CREATE TABLE memory.v5_2_manual_atom_selection_v1 (
  selection_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  packet_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  packet_storage_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(packet_storage_sha256)),
  baseline_proposal_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(baseline_proposal_sha256)),
  manual_review_report_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(manual_review_report_sha256)),
  approved_entity_refs jsonb NOT NULL,
  approved_observation_refs jsonb NOT NULL,
  selection_manifest_sha256 text NOT NULL
    CHECK (memory.v5_sha256_valid(selection_manifest_sha256)),
  policy_version text NOT NULL CHECK (
    policy_version='memory_v1_v5_2_manual_atom_selection_policy_v1'
  ),
  decision text NOT NULL CHECK (decision='authorized'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(owner_user_id,selection_id),
  UNIQUE(owner_user_id,operation_id),
  UNIQUE(owner_user_id,packet_id),
  UNIQUE(owner_user_id,selection_manifest_sha256),
  FOREIGN KEY(owner_user_id,packet_id)
    REFERENCES memory.evidence_extraction_packet_v5_local(
      owner_user_id,packet_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY(owner_user_id,evidence_id)
    REFERENCES memory.evidence(owner_user_id,evidence_id)
    ON DELETE RESTRICT,
  CHECK (
    jsonb_typeof(approved_entity_refs)='array'
    AND jsonb_array_length(approved_entity_refs) BETWEEN 1 AND 24
  ),
  CHECK (
    jsonb_typeof(approved_observation_refs)='array'
    AND jsonb_array_length(approved_observation_refs) BETWEEN 1 AND 32
  )
);
ALTER TABLE memory.v5_2_manual_atom_selection_v1
  OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER TABLE memory.v5_2_manual_atom_selection_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.v5_2_manual_atom_selection_v1 FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_isolation
  ON memory.v5_2_manual_atom_selection_v1
  TO memory_v5_2_atom_admission_maintainer
  USING (owner_user_id=memory.current_actor_user_id())
  WITH CHECK (owner_user_id=memory.current_actor_user_id());
CREATE TRIGGER v5_2_manual_atom_selection_v1_append_only
BEFORE UPDATE OR DELETE ON memory.v5_2_manual_atom_selection_v1
FOR EACH ROW EXECUTE FUNCTION memory.guard_v5_local_inference_append_only();
GRANT SELECT,INSERT ON memory.v5_2_manual_atom_selection_v1
  TO memory_v5_2_atom_admission_maintainer;

CREATE OR REPLACE FUNCTION memory.v5_2_manual_atom_selection_manifest_sha_v1(
  p_owner_user_id uuid,
  p_selection_id uuid,
  p_packet_id uuid,
  p_evidence_id uuid,
  p_packet_storage_sha256 text,
  p_baseline_proposal_sha256 text,
  p_manual_review_report_sha256 text,
  p_approved_entity_refs jsonb,
  p_approved_observation_refs jsonb
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY DEFINER
SET search_path=''
AS $function$
  SELECT memory.v5_digest_text(concat_ws('|',
    'memory_v1_v5_2_manual_atom_selection_v1',p_owner_user_id::text,
    p_selection_id::text,p_packet_id::text,p_evidence_id::text,
    p_packet_storage_sha256,p_baseline_proposal_sha256,
    p_manual_review_report_sha256,
    memory.v5_canonical_json_text(p_approved_entity_refs),
    memory.v5_canonical_json_text(p_approved_observation_refs)
  ))
$function$;

CREATE OR REPLACE FUNCTION memory.register_owner_v5_2_manual_atom_selection_v1(
  p_selection_id uuid,
  p_operation_id uuid,
  p_packet_id uuid,
  p_expected_packet_storage_sha256 text,
  p_expected_baseline_proposal_sha256 text,
  p_manual_review_report_sha256 text,
  p_approved_entity_refs jsonb,
  p_approved_observation_refs jsonb,
  p_expected_selection_manifest_sha256 text
)
RETURNS TABLE(
  selection_id uuid,
  outcome text,
  selection_manifest_sha256 text,
  approved_entity_count integer,
  approved_observation_count integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  baseline jsonb;
  existing memory.v5_2_manual_atom_selection_v1%ROWTYPE;
  manifest text;
BEGIN
  actor:=memory.require_v5_writer_context();
  IF p_selection_id IS NULL OR p_operation_id IS NULL OR p_packet_id IS NULL
     OR NOT memory.v5_sha256_valid(p_expected_packet_storage_sha256)
     OR NOT memory.v5_sha256_valid(p_expected_baseline_proposal_sha256)
     OR NOT memory.v5_sha256_valid(p_manual_review_report_sha256)
     OR NOT memory.v5_sha256_valid(p_expected_selection_manifest_sha256)
     OR jsonb_typeof(p_approved_entity_refs)<>'array'
     OR jsonb_typeof(p_approved_observation_refs)<>'array'
     OR jsonb_array_length(p_approved_entity_refs) NOT BETWEEN 1 AND 24
     OR jsonb_array_length(p_approved_observation_refs) NOT BETWEEN 1 AND 32
     OR EXISTS (
       SELECT 1 FROM jsonb_array_elements(p_approved_entity_refs) AS item
       WHERE jsonb_typeof(item)<>'string' OR btrim(item#>>'{}')=''
     )
     OR EXISTS (
       SELECT 1 FROM jsonb_array_elements(p_approved_observation_refs) AS item
       WHERE jsonb_typeof(item)<>'string' OR btrim(item#>>'{}')=''
     )
     OR (SELECT count(*) FROM jsonb_array_elements_text(p_approved_entity_refs))
        <>(SELECT count(DISTINCT value)
           FROM jsonb_array_elements_text(p_approved_entity_refs) AS value)
     OR (SELECT count(*) FROM jsonb_array_elements_text(p_approved_observation_refs))
        <>(SELECT count(DISTINCT value)
           FROM jsonb_array_elements_text(p_approved_observation_refs) AS value)
  THEN
    RAISE EXCEPTION 'manual atom selection inputs are invalid'
      USING ERRCODE='22023';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(concat_ws('|',
    actor::text,'v5_2_manual_atom_selection',p_packet_id::text
  ),0));
  SELECT source.* INTO packet
  FROM memory.evidence_extraction_packet_v5_local AS source
  WHERE source.owner_user_id=actor AND source.packet_id=p_packet_id;
  IF NOT FOUND
     OR packet.packet_id<>memory.authoritative_owner_v5_2_packet_id_v1(
       packet.evidence_id
     )
     OR packet.packet_storage_sha256<>p_expected_packet_storage_sha256
     OR jsonb_typeof(packet.normalized_packet->'entity_mentions')<>'array'
     OR jsonb_typeof(packet.normalized_packet->'observations')<>'array'
     OR jsonb_typeof(packet.normalized_packet->'comparison_hints')<>'array'
     OR jsonb_typeof(packet.normalized_packet->'deferrals')<>'array'
     OR jsonb_typeof(packet.normalized_packet->'packet_findings')<>'array'
     OR jsonb_array_length(packet.normalized_packet->'comparison_hints')<>0
     OR jsonb_array_length(packet.normalized_packet->'packet_findings')<>0
     OR NOT EXISTS (
       SELECT 1 FROM memory.v5_2_local_packet_route_event AS route
       WHERE route.owner_user_id=actor
         AND route.packet_id=packet.packet_id
         AND route.evidence_id=packet.evidence_id
         AND route.route='manual_review_artifact_ready'
         AND route.reason_code='reviewable_relational_packet_v5_2'
     )
     OR EXISTS (
       SELECT 1 FROM memory.v5_2_local_packet_route_event AS route
       WHERE route.owner_user_id=actor
         AND route.packet_id=packet.packet_id
         AND route.evidence_id=packet.evidence_id
         AND route.route='terminal_no_stage'
     ) THEN
    RAISE EXCEPTION 'owner-authoritative reviewable packet is unavailable'
      USING ERRCODE='P0002';
  END IF;

  baseline:=memory.plan_owner_v5_2_atom_admission_v2_before_manual_selection_v1(
    p_packet_id
  );
  IF baseline->>'proposal_sha256'<>p_expected_baseline_proposal_sha256
     OR baseline->>'source_packet_storage_sha256'
          <>p_expected_packet_storage_sha256 THEN
    RAISE EXCEPTION 'manual atom selection baseline drifted'
      USING ERRCODE='23514';
  END IF;

  IF (SELECT count(*)
      FROM jsonb_array_elements(packet.normalized_packet->'entity_mentions') item
      JOIN jsonb_array_elements_text(p_approved_entity_refs) approved
        ON approved.value=item->>'entity_ref')
       <>jsonb_array_length(p_approved_entity_refs)
     OR (SELECT count(*)
         FROM jsonb_array_elements(packet.normalized_packet->'observations') item
         JOIN jsonb_array_elements_text(p_approved_observation_refs) approved
           ON approved.value=item->>'observation_ref')
       <>jsonb_array_length(p_approved_observation_refs)
     OR EXISTS (
       SELECT 1
       FROM jsonb_array_elements(packet.normalized_packet->'observations') item
       JOIN jsonb_array_elements_text(p_approved_observation_refs) approved
         ON approved.value=item->>'observation_ref'
       WHERE item->>'subject_entity_ref' IS NOT NULL
         AND NOT (p_approved_entity_refs ? (item->>'subject_entity_ref'))
     )
  THEN
    RAISE EXCEPTION 'manual atom selection references are not dependency-closed'
      USING ERRCODE='23514';
  END IF;

  manifest:=memory.v5_2_manual_atom_selection_manifest_sha_v1(
    actor,p_selection_id,p_packet_id,packet.evidence_id,
    p_expected_packet_storage_sha256,p_expected_baseline_proposal_sha256,
    p_manual_review_report_sha256,p_approved_entity_refs,
    p_approved_observation_refs
  );
  IF manifest<>p_expected_selection_manifest_sha256 THEN
    RAISE EXCEPTION 'manual atom selection manifest mismatch'
      USING ERRCODE='23514';
  END IF;

  SELECT value.* INTO existing
  FROM memory.v5_2_manual_atom_selection_v1 AS value
  WHERE value.owner_user_id=actor
    AND (value.operation_id=p_operation_id OR value.packet_id=p_packet_id);
  IF FOUND THEN
    IF existing.selection_id<>p_selection_id
       OR existing.packet_id<>p_packet_id
       OR existing.packet_storage_sha256<>p_expected_packet_storage_sha256
       OR existing.baseline_proposal_sha256<>p_expected_baseline_proposal_sha256
       OR existing.manual_review_report_sha256<>p_manual_review_report_sha256
       OR existing.approved_entity_refs<>p_approved_entity_refs
       OR existing.approved_observation_refs<>p_approved_observation_refs
       OR existing.selection_manifest_sha256<>manifest THEN
      RAISE EXCEPTION 'manual atom selection replay conflicts'
        USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT existing.selection_id,'replayed',manifest,
      jsonb_array_length(existing.approved_entity_refs),
      jsonb_array_length(existing.approved_observation_refs);
    RETURN;
  END IF;

  INSERT INTO memory.v5_2_manual_atom_selection_v1(
    selection_id,owner_user_id,operation_id,packet_id,evidence_id,
    packet_storage_sha256,baseline_proposal_sha256,
    manual_review_report_sha256,approved_entity_refs,
    approved_observation_refs,selection_manifest_sha256,
    policy_version,decision
  ) VALUES (
    p_selection_id,actor,p_operation_id,p_packet_id,packet.evidence_id,
    p_expected_packet_storage_sha256,p_expected_baseline_proposal_sha256,
    p_manual_review_report_sha256,p_approved_entity_refs,
    p_approved_observation_refs,manifest,
    'memory_v1_v5_2_manual_atom_selection_policy_v1','authorized'
  );
  RETURN QUERY SELECT p_selection_id,'applied',manifest,
    jsonb_array_length(p_approved_entity_refs),
    jsonb_array_length(p_approved_observation_refs);
END
$function$;

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_2_atom_admission_v2(
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
  selection memory.v5_2_manual_atom_selection_v1%ROWTYPE;
  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  projection jsonb;
  decisions jsonb;
  counts jsonb;
  proposal jsonb;
  proposal_sha text;
  atom_manifest_sha text;
BEGIN
  actor:=memory.require_v5_writer_context();
  SELECT value.* INTO selection
  FROM memory.v5_2_manual_atom_selection_v1 AS value
  WHERE value.owner_user_id=actor
    AND value.packet_id=p_packet_id
    AND value.decision='authorized';
  IF NOT FOUND THEN
    RETURN memory.plan_owner_v5_2_atom_admission_v2_before_manual_selection_v1(
      p_packet_id
    );
  END IF;

  SELECT source.* INTO STRICT packet
  FROM memory.evidence_extraction_packet_v5_local AS source
  WHERE source.owner_user_id=actor AND source.packet_id=p_packet_id;
  IF packet.packet_storage_sha256<>selection.packet_storage_sha256
     OR packet.packet_id<>memory.authoritative_owner_v5_2_packet_id_v1(
       packet.evidence_id
     ) THEN
    RAISE EXCEPTION 'manual atom selection source is no longer authoritative'
      USING ERRCODE='23514';
  END IF;

  projection:=jsonb_build_object(
    'contract_version','memory_v1_relational_extraction_v5_2',
    'source_envelope',packet.normalized_packet->'source_envelope',
    'predicate_registry_version','memory_predicate_registry_v5_2',
    'entity_mentions',COALESCE((
      SELECT jsonb_agg(item ORDER BY ordinality)
      FROM jsonb_array_elements(packet.normalized_packet->'entity_mentions')
        WITH ORDINALITY AS source(item,ordinality)
      WHERE selection.approved_entity_refs ? (item->>'entity_ref')
    ),'[]'::jsonb),
    'observations',COALESCE((
      SELECT jsonb_agg(item ORDER BY ordinality)
      FROM jsonb_array_elements(packet.normalized_packet->'observations')
        WITH ORDINALITY AS source(item,ordinality)
      WHERE selection.approved_observation_refs ? (item->>'observation_ref')
    ),'[]'::jsonb),
    'comparison_hints','[]'::jsonb,
    'deferrals','[]'::jsonb,
    'packet_findings','[]'::jsonb
  );

  WITH atoms AS (
    SELECT 1 AS kind_order,ordinality::integer AS ordinal,
      item->>'entity_ref' AS atom_ref,'entity_mention'::text AS atom_kind,
      item AS atom,
      CASE WHEN selection.approved_entity_refs ? (item->>'entity_ref')
        THEN 'admit' ELSE 'retain_source_only' END AS disposition,
      CASE WHEN selection.approved_entity_refs ? (item->>'entity_ref')
        THEN jsonb_build_array('manual_review_selected_entity')
        ELSE jsonb_build_array('manual_review_unselected_entity') END AS reasons
    FROM jsonb_array_elements(packet.normalized_packet->'entity_mentions')
      WITH ORDINALITY AS source(item,ordinality)
    UNION ALL
    SELECT 2,ordinality::integer,item->>'observation_ref','observation',item,
      CASE WHEN selection.approved_observation_refs ? (item->>'observation_ref')
        THEN 'admit' ELSE 'defer' END,
      CASE WHEN selection.approved_observation_refs ? (item->>'observation_ref')
        THEN jsonb_build_array(
          'manual_review_selected_observation','entity_dependencies_admissible'
        )
        ELSE jsonb_build_array('manual_review_unselected_observation') END
    FROM jsonb_array_elements(packet.normalized_packet->'observations')
      WITH ORDINALITY AS source(item,ordinality)
    UNION ALL
    SELECT 3,ordinality::integer,
      format('deferral_%s',lpad(ordinality::text,2,'0')),
      'deferral',item,'defer',jsonb_build_array(
        'source_deferral','source_deferral:'||(item->>'reason_code')
      )
    FROM jsonb_array_elements(packet.normalized_packet->'deferrals')
      WITH ORDINALITY AS source(item,ordinality)
  )
  SELECT jsonb_agg(jsonb_build_object(
    'ordinal',ordinal,'atom_ref',atom_ref,'atom_kind',atom_kind,
    'atom_sha256',memory.v5_digest_text(memory.v5_canonical_json_text(atom)),
    'reason_codes',reasons,
    'source_spans_sha256',memory.v5_digest_text(
      memory.v5_canonical_json_text(COALESCE(atom->'source_spans','[]'::jsonb))
    ),
    'proposed_disposition',disposition
  ) ORDER BY kind_order,ordinal) INTO decisions FROM atoms;

  counts:=jsonb_build_object(
    'source_atom_count',
      jsonb_array_length(packet.normalized_packet->'entity_mentions')+
      jsonb_array_length(packet.normalized_packet->'observations')+
      jsonb_array_length(packet.normalized_packet->'deferrals'),
    'admitted_entity_mention_count',
      jsonb_array_length(selection.approved_entity_refs),
    'admitted_observation_count',
      jsonb_array_length(selection.approved_observation_refs),
    'admitted_comparison_hint_count',0,
    'deferred_atom_count',
      jsonb_array_length(packet.normalized_packet->'deferrals')+
      jsonb_array_length(packet.normalized_packet->'observations')-
      jsonb_array_length(selection.approved_observation_refs),
    'retained_source_only_count',
      jsonb_array_length(packet.normalized_packet->'entity_mentions')-
      jsonb_array_length(selection.approved_entity_refs),
    'rejected_atom_count',0
  );
  atom_manifest_sha:=memory.v5_digest_text(
    memory.v5_canonical_json_text(decisions)
  );
  proposal:=jsonb_build_object(
    'contract_version','memory_v1_v5_2_atom_admission_proposal_v2',
    'policy_version','memory_v1_v5_2_atom_admission_policy_v2',
    'source_packet_id',packet.packet_id::text,
    'source_job_id',packet.job_id::text,
    'source_evidence_id',packet.evidence_id::text,
    'source_validator_packet_sha256',packet.validator_packet_sha256,
    'source_packet_storage_sha256',packet.packet_storage_sha256,
    'source_atom_manifest_sha256',atom_manifest_sha,
    'atom_decisions',decisions,
    'stage_projection',projection,
    'stage_projection_sha256',memory.v5_digest_text(
      memory.v5_canonical_json_text(projection)
    ),
    'counts',counts,
    'proposal_sha256',''
  );
  proposal_sha:=memory.v5_2_atom_proposal_sha256_v1(proposal);
  RETURN jsonb_set(proposal,'{proposal_sha256}',to_jsonb(proposal_sha),false);
END
$function$;

ALTER FUNCTION memory.register_owner_v5_2_manual_atom_selection_v1(
  uuid,uuid,uuid,text,text,text,jsonb,jsonb,text
) OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.v5_2_manual_atom_selection_manifest_sha_v1(
  uuid,uuid,uuid,uuid,text,text,text,jsonb,jsonb
) OWNER TO memory_v5_2_atom_admission_maintainer;
ALTER FUNCTION memory.plan_owner_v5_2_atom_admission_v2(uuid)
  OWNER TO memory_v5_2_atom_admission_maintainer;
REVOKE ALL ON FUNCTION memory.register_owner_v5_2_manual_atom_selection_v1(
  uuid,uuid,uuid,text,text,text,jsonb,jsonb,text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.v5_2_manual_atom_selection_manifest_sha_v1(
  uuid,uuid,uuid,uuid,text,text,text,jsonb,jsonb
) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_atom_admission_v2(uuid)
  FROM PUBLIC,memory_v5_writer,memory_v5_2_local_router_maintainer;
GRANT EXECUTE ON FUNCTION memory.register_owner_v5_2_manual_atom_selection_v1(
  uuid,uuid,uuid,text,text,text,jsonb,jsonb,text
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.v5_2_manual_atom_selection_manifest_sha_v1(
  uuid,uuid,uuid,uuid,text,text,text,jsonb,jsonb
) TO brains_app;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_2_atom_admission_v2(uuid)
  TO brains_app;

COMMENT ON TABLE memory.v5_2_manual_atom_selection_v1 IS
  'Append-only owner-reviewed atom selection that can admit an exact dependency-closed subset from a hard-deferred authoritative V5.2 packet.';
COMMENT ON FUNCTION memory.plan_owner_v5_2_atom_admission_v2(uuid) IS
  'Returns the unchanged automatic V2 atom plan unless an exact append-only manual selection exists; manual selections preserve all unselected source material outside the stage projection.';

COMMIT;
