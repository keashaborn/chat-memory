BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $guard$
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'compiler-v10 exact-14 rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.v5_local_packet_supersession
    WHERE reason_code='semantic_compiler_v10_reextracted'
  ) THEN
    RAISE EXCEPTION
      'rollback refuses to remove compatibility with durable supersessions';
  END IF;
END
$guard$;

DROP FUNCTION IF EXISTS
memory.finalize_owner_v5_2_semantic_compiler_v10_supersession_v1(
  uuid,uuid,uuid,uuid,text,text,text,text
);
DROP FUNCTION IF EXISTS
memory.plan_owner_v5_2_semantic_compiler_v10_supersession_v1(uuid,uuid);
DROP FUNCTION IF EXISTS
memory.enqueue_owner_v5_2_semantic_compiler_v10_exact_14_v1(
  jsonb,text,text
);
REVOKE SELECT ON memory.v5_local_packet_supersession
  FROM memory_v5_local_reextract_maintainer;

DO $router_rollback$
DECLARE
  source text;
  needle text :=
    'WHEN value.manual_review_required'
    || E'\n          THEN ''manual_review_artifact_ready'''
    || E'\n        WHEN value.entity_mention_count+value.observation_count'
    || E'\n               +value.comparison_hint_count>=1'
    || E'\n          THEN ''manual_review_artifact_ready''';
  replacement text :=
    'WHEN value.entity_mention_count+value.observation_count'
    || E'\n               +value.comparison_hint_count>=1'
    || E'\n          THEN ''manual_review_artifact_ready''';
BEGIN
  source:=pg_get_functiondef(
    'memory.plan_owner_v5_2_exact_packet_route_v1(uuid)'::regprocedure
  );
  IF (length(source)-length(replace(source,needle,'')))/length(needle)<>1 THEN
    RAISE EXCEPTION 'exact route planner rollback point changed';
  END IF;
  EXECUTE replace(source,needle,replacement);
END
$router_rollback$;

ALTER FUNCTION memory.plan_owner_v5_2_exact_packet_route_v1(uuid)
  OWNER TO memory_v5_2_local_router_maintainer;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_exact_packet_route_v1(uuid)
  FROM PUBLIC,brains_app,memory_v5_2_local_router_maintainer;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_2_exact_packet_route_v1(uuid)
  TO brains_app,memory_v5_2_local_router_maintainer;

ALTER TABLE memory.v5_local_packet_supersession
  DROP CONSTRAINT v5_local_packet_supersession_reason_code_check;
ALTER TABLE memory.v5_local_packet_supersession
  ADD CONSTRAINT v5_local_packet_supersession_reason_code_check
  CHECK (reason_code IN (
    'temporal_persistence_matrix_reextracted',
    'pet_identity_semantics_reextracted',
    'duplicate_active_packet_reconciled'
  ));

COMMIT;
