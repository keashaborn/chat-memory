BEGIN;

DO $role$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles
    WHERE rolname='memory_v5_manual_packet_review_reader'
  ) THEN
    CREATE ROLE memory_v5_manual_packet_review_reader
      NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS;
  END IF;
END
$role$;

GRANT memory_v5_manual_packet_review_reader TO sage
  WITH INHERIT FALSE, SET TRUE;
GRANT USAGE ON SCHEMA memory TO memory_v5_manual_packet_review_reader;
GRANT EXECUTE ON FUNCTION memory.current_actor_user_id()
  TO memory_v5_manual_packet_review_reader;
GRANT EXECUTE ON FUNCTION memory.authoritative_owner_v5_2_packet_id_v1(uuid)
  TO memory_v5_manual_packet_review_reader;
GRANT SELECT ON
  memory.evidence,
  memory.evidence_extraction_packet_v5_local,
  memory.v5_2_local_packet_route_event,
  memory.entity_resolution_plan,
  memory.entity_mention,
  memory.observation
TO memory_v5_manual_packet_review_reader;

DROP POLICY IF EXISTS manual_packet_review_owner_select_v1
  ON memory.evidence;
CREATE POLICY manual_packet_review_owner_select_v1 ON memory.evidence
  FOR SELECT TO memory_v5_manual_packet_review_reader
  USING (owner_user_id=(SELECT memory.current_actor_user_id()));

DROP POLICY IF EXISTS manual_packet_review_owner_select_v1
  ON memory.evidence_extraction_packet_v5_local;
CREATE POLICY manual_packet_review_owner_select_v1
  ON memory.evidence_extraction_packet_v5_local
  FOR SELECT TO memory_v5_manual_packet_review_reader
  USING (owner_user_id=(SELECT memory.current_actor_user_id()));

DROP POLICY IF EXISTS manual_packet_review_owner_select_v1
  ON memory.v5_2_local_packet_route_event;
CREATE POLICY manual_packet_review_owner_select_v1
  ON memory.v5_2_local_packet_route_event
  FOR SELECT TO memory_v5_manual_packet_review_reader
  USING (owner_user_id=(SELECT memory.current_actor_user_id()));

DROP POLICY IF EXISTS manual_packet_review_owner_select_v1
  ON memory.entity_resolution_plan;
CREATE POLICY manual_packet_review_owner_select_v1
  ON memory.entity_resolution_plan
  FOR SELECT TO memory_v5_manual_packet_review_reader
  USING (owner_user_id=(SELECT memory.current_actor_user_id()));

DROP POLICY IF EXISTS manual_packet_review_owner_select_v1
  ON memory.entity_mention;
CREATE POLICY manual_packet_review_owner_select_v1
  ON memory.entity_mention
  FOR SELECT TO memory_v5_manual_packet_review_reader
  USING (owner_user_id=(SELECT memory.current_actor_user_id()));

DROP POLICY IF EXISTS manual_packet_review_owner_select_v1
  ON memory.observation;
CREATE POLICY manual_packet_review_owner_select_v1
  ON memory.observation
  FOR SELECT TO memory_v5_manual_packet_review_reader
  USING (owner_user_id=(SELECT memory.current_actor_user_id()));

CREATE OR REPLACE FUNCTION memory.plan_owner_v5_2_manual_packet_review_v1(
  p_packet_id uuid,
  p_prior_role_resolution_id uuid,
  p_prior_duration_observation_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
DECLARE
  actor uuid;
  packet memory.evidence_extraction_packet_v5_local%ROWTYPE;
  route memory.v5_2_local_packet_route_event%ROWTYPE;
  prior_plan memory.entity_resolution_plan%ROWTYPE;
  prior_mention memory.entity_mention%ROWTYPE;
  prior_duration memory.observation%ROWTYPE;
  prior_duration_evidence memory.evidence%ROWTYPE;
BEGIN
  actor := memory.current_actor_user_id();
  IF p_packet_id IS NULL
     OR p_prior_role_resolution_id IS NULL
     OR p_prior_duration_observation_id IS NULL THEN
    RAISE EXCEPTION 'manual packet review identifiers are required'
      USING ERRCODE='22004';
  END IF;

  SELECT source.* INTO packet
  FROM memory.evidence_extraction_packet_v5_local AS source
  WHERE source.owner_user_id=actor AND source.packet_id=p_packet_id;
  IF NOT FOUND
     OR packet.packet_id IS DISTINCT FROM
        memory.authoritative_owner_v5_2_packet_id_v1(packet.evidence_id)
     OR NOT packet.manual_review_required THEN
    RAISE EXCEPTION 'owner-scoped authoritative manual packet unavailable'
      USING ERRCODE='P0002';
  END IF;

  SELECT source.* INTO route
  FROM memory.v5_2_local_packet_route_event AS source
  WHERE source.owner_user_id=actor
    AND source.packet_id=packet.packet_id
    AND source.evidence_id=packet.evidence_id
    AND source.route='manual_review_artifact_ready'
    AND source.reason_code='reviewable_relational_packet_v5_2';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped manual packet route unavailable'
      USING ERRCODE='P0002';
  END IF;

  SELECT source.* INTO prior_plan
  FROM memory.entity_resolution_plan AS source
  WHERE source.owner_user_id=actor
    AND source.resolution_id=p_prior_role_resolution_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped prior role resolution unavailable'
      USING ERRCODE='P0002';
  END IF;
  SELECT source.* INTO STRICT prior_mention
  FROM memory.entity_mention AS source
  WHERE source.owner_user_id=actor
    AND source.mention_id=prior_plan.mention_id
    AND source.evidence_id=prior_plan.evidence_id;

  SELECT source.* INTO prior_duration
  FROM memory.observation AS source
  WHERE source.owner_user_id=actor
    AND source.observation_id=p_prior_duration_observation_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'owner-scoped prior duration observation unavailable'
      USING ERRCODE='P0002';
  END IF;
  SELECT source.* INTO STRICT prior_duration_evidence
  FROM memory.evidence AS source
  WHERE source.owner_user_id=actor
    AND source.evidence_id=prior_duration.evidence_id;

  RETURN jsonb_build_object(
    'contract_version','memory_v1_v5_2_manual_packet_review_plan_v1',
    'owner_user_id',actor,
    'packet_id',packet.packet_id,
    'evidence_id',packet.evidence_id,
    'packet_storage_sha256',packet.packet_storage_sha256,
    'normalized_packet',packet.normalized_packet,
    'manual_review_required',packet.manual_review_required,
    'route_event_id',route.route_event_id,
    'route',route.route,
    'route_reason_code',route.reason_code,
    'manual_review_count',route.manual_review_count,
    'blocking_code_count',route.blocking_code_count,
    'prior_role',jsonb_build_object(
      'resolution_id',prior_plan.resolution_id,
      'mention_id',prior_plan.mention_id,
      'action',prior_plan.action,
      'decision_state',prior_plan.decision_state,
      'relationship_role',prior_mention.relationship_role,
      'mention_kind',prior_mention.mention_kind,
      'name_text',prior_mention.name_text
    ),
    'prior_duration',jsonb_build_object(
      'observation_id',prior_duration.observation_id,
      'evidence_id',prior_duration.evidence_id,
      'object_literal',prior_duration.object_literal,
      'observed_at',prior_duration_evidence.observed_at
    )
  );
END
$function$;

ALTER FUNCTION memory.plan_owner_v5_2_manual_packet_review_v1(uuid,uuid,uuid)
  OWNER TO memory_v5_manual_packet_review_reader;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_manual_packet_review_v1(uuid,uuid,uuid)
  FROM PUBLIC,memory_v5_manual_packet_review_reader;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_2_manual_packet_review_v1(uuid,uuid,uuid)
  TO brains_app;

COMMIT;
