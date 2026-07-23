BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $rollback$
DECLARE
  planner_definition text;
  guard_definition text;
  old_planner_gate constant text :=
$old$    AND packet.normalized_packet->>'contract_version'
          ='memory_v1_relational_extraction_v5'
    AND packet.normalized_packet->>'predicate_registry_version'
          ='memory_predicate_registry_v5'$old$;
  new_planner_gate constant text :=
$new$    AND (
      (
        packet.normalized_packet->>'contract_version'
          ='memory_v1_relational_extraction_v5'
        AND packet.normalized_packet->>'predicate_registry_version'
          ='memory_predicate_registry_v5'
      ) OR (
        packet.normalized_packet->>'contract_version'
          ='memory_v1_relational_extraction_v5_2'
        AND packet.normalized_packet->>'predicate_registry_version'
          ='memory_predicate_registry_v5_2'
        AND packet.entity_mention_count=0
        AND packet.observation_count=0
        AND packet.comparison_hint_count=0
        AND packet.deferral_count>0
        AND NOT packet.manual_review_required
        AND jsonb_typeof(packet.normalized_packet->'deferrals')='array'
        AND jsonb_array_length(packet.normalized_packet->'deferrals')
              =packet.deferral_count
        AND packet.normalized_packet @?
          '$.deferrals[*] ? (@.reason_code == "structured_domain" && @.memory_shape == "none" && @.review_required == false)'
        AND NOT packet.normalized_packet @?
          '$.deferrals[*] ? (@.memory_shape != "none" || @.review_required != false || (@.reason_code != "structured_domain" && @.reason_code != "question_only"))'
      )
    )$new$;
  old_guard_branch constant text :=
$old$          ) OR (
            packet.normalized_packet->>'contract_version'
              ='memory_v1_relational_extraction_v5_2'
            AND packet.normalized_packet->>'predicate_registry_version'
              ='memory_predicate_registry_v5_2'
            AND NEW.reason_code='ambiguous_transcription'
            AND NEW.review_decision='deferred'
            AND NOT NEW.promotion_eligible
            AND NEW.review_basis_sha256 ~ '^[0-9a-f]{64}$'
          )
$old$;
  new_guard_branch constant text :=
$new$          ) OR (
            packet.normalized_packet->>'contract_version'
              ='memory_v1_relational_extraction_v5_2'
            AND packet.normalized_packet->>'predicate_registry_version'
              ='memory_predicate_registry_v5_2'
            AND NEW.reason_code='ambiguous_transcription'
            AND NEW.review_decision='deferred'
            AND NOT NEW.promotion_eligible
            AND NEW.review_basis_sha256 ~ '^[0-9a-f]{64}$'
          ) OR (
            packet.normalized_packet->>'contract_version'
              ='memory_v1_relational_extraction_v5_2'
            AND packet.normalized_packet->>'predicate_registry_version'
              ='memory_predicate_registry_v5_2'
            AND NEW.reason_code='deferral_only_no_stage'
            AND NEW.review_decision IS NULL
            AND NOT NEW.promotion_eligible
            AND NEW.review_basis_sha256 IS NULL
            AND packet.entity_mention_count=0
            AND packet.observation_count=0
            AND packet.comparison_hint_count=0
            AND packet.deferral_count>0
            AND NOT packet.manual_review_required
            AND jsonb_typeof(packet.normalized_packet->'deferrals')='array'
            AND jsonb_array_length(packet.normalized_packet->'deferrals')
                  =packet.deferral_count
            AND packet.normalized_packet @?
              '$.deferrals[*] ? (@.reason_code == "structured_domain" && @.memory_shape == "none" && @.review_required == false)'
            AND NOT packet.normalized_packet @?
              '$.deferrals[*] ? (@.memory_shape != "none" || @.review_required != false || (@.reason_code != "structured_domain" && @.reason_code != "question_only"))'
          )
$new$;
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION
      'V5.2 structured-domain terminal rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.v5_local_packet_disposition AS disposition
    JOIN memory.evidence_extraction_packet_v5_local AS packet
      ON packet.owner_user_id=disposition.owner_user_id
     AND packet.packet_id=disposition.packet_id
    WHERE packet.normalized_packet->>'contract_version'
            ='memory_v1_relational_extraction_v5_2'
      AND packet.normalized_packet->>'predicate_registry_version'
            ='memory_predicate_registry_v5_2'
      AND disposition.reason_code='deferral_only_no_stage'
  ) THEN
    RAISE EXCEPTION
      'V5.2 structured-domain terminal rows exist; rollback is unsafe';
  END IF;

  SELECT pg_get_functiondef(
    'memory.plan_owner_v5_local_packet_disposition_v1(integer)'::regprocedure
  ) INTO STRICT planner_definition;
  SELECT pg_get_functiondef(
    'memory.guard_v5_legacy_packet_lane_v1()'::regprocedure
  ) INTO STRICT guard_definition;

  IF strpos(planner_definition,new_planner_gate)=0
     OR strpos(guard_definition,new_guard_branch)=0 THEN
    RAISE EXCEPTION
      'V5.2 structured-domain terminal rollback source drifted';
  END IF;
  planner_definition:=replace(
    planner_definition,new_planner_gate,old_planner_gate
  );
  guard_definition:=replace(
    guard_definition,new_guard_branch,old_guard_branch
  );
  EXECUTE planner_definition;
  EXECUTE guard_definition;
END
$rollback$;

COMMENT ON FUNCTION
  memory.plan_owner_v5_local_packet_disposition_v1(integer)
IS 'Routes content-free legacy V5 deferral packets to an audited terminal disposition, retaining whether unresolved review was requested.';

COMMENT ON FUNCTION memory.guard_v5_legacy_packet_lane_v1()
IS 'Guards legacy V5 packet lanes and the reviewed V5.2 ambiguous-transcription disposition exception.';

COMMIT;
