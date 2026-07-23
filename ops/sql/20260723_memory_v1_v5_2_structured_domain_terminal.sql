BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $migration$
DECLARE
  planner_definition text;
  guard_definition text;
  planner_sha256 text;
  guard_sha256 text;
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
  IF session_user<>'sage'
     OR to_regrole('memory_v5_local_disposition_maintainer') IS NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_local_packet_disposition_v1(integer)'
     ) IS NULL
     OR to_regprocedure(
       'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)'
     ) IS NULL
     OR to_regprocedure('memory.guard_v5_legacy_packet_lane_v1()') IS NULL
     OR to_regclass('memory.v5_local_packet_disposition') IS NULL
     OR to_regclass('memory.v5_local_packet_review_artifact') IS NULL THEN
    RAISE EXCEPTION
      'V5.2 structured-domain terminal prerequisites are absent';
  END IF;

  SELECT pg_get_functiondef(
    'memory.plan_owner_v5_local_packet_disposition_v1(integer)'::regprocedure
  ) INTO STRICT planner_definition;
  SELECT encode(public.digest(convert_to(
    planner_definition,'UTF8'
  ),'sha256'),'hex') INTO planner_sha256;

  SELECT pg_get_functiondef(
    'memory.guard_v5_legacy_packet_lane_v1()'::regprocedure
  ) INTO STRICT guard_definition;
  SELECT encode(public.digest(convert_to(
    guard_definition,'UTF8'
  ),'sha256'),'hex') INTO guard_sha256;

  IF planner_sha256=
       '65135a8cd93ed45d626535dd37cf7877c617908f4f84c332eef0d653dc1d7be3'
     AND guard_sha256=
       'cdb57c7daaabc71c297fd40ab57a85569303a5b6da447001fd8bd409d747ab70'
  THEN
    IF strpos(planner_definition,old_planner_gate)=0
       OR strpos(guard_definition,old_guard_branch)=0 THEN
      RAISE EXCEPTION 'hash-locked function source fragments are absent';
    END IF;
    planner_definition:=replace(
      planner_definition,old_planner_gate,new_planner_gate
    );
    guard_definition:=replace(
      guard_definition,old_guard_branch,new_guard_branch
    );
    EXECUTE planner_definition;
    EXECUTE guard_definition;
    ALTER FUNCTION
      memory.plan_owner_v5_local_packet_disposition_v1(integer)
      OWNER TO memory_v5_local_disposition_maintainer;
    ALTER FUNCTION memory.guard_v5_legacy_packet_lane_v1()
      OWNER TO memory_v5_local_disposition_maintainer;
  ELSIF strpos(planner_definition,new_planner_gate)=0
        OR strpos(guard_definition,new_guard_branch)=0 THEN
    RAISE EXCEPTION
      'V5.2 structured-domain terminal function definitions drifted';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_proc
    WHERE oid=
      'memory.plan_owner_v5_local_packet_disposition_v1(integer)'::regprocedure
      AND prosecdef
      AND provolatile='s'
      AND proowner='memory_v5_local_disposition_maintainer'::regrole
  ) THEN
    RAISE EXCEPTION
      'V5.2 structured-domain planner ownership or metadata is unsafe';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_proc
    WHERE oid='memory.guard_v5_legacy_packet_lane_v1()'::regprocedure
      AND prosecdef
      AND proowner='memory_v5_local_disposition_maintainer'::regrole
  ) THEN
    RAISE EXCEPTION
      'V5.2 structured-domain guard ownership or metadata is unsafe';
  END IF;

  IF NOT has_function_privilege(
    'brains_app',
    'memory.plan_owner_v5_local_packet_disposition_v1(integer)',
    'EXECUTE'
  ) OR has_function_privilege(
    'brains_app','memory.guard_v5_legacy_packet_lane_v1()','EXECUTE'
  ) THEN
    RAISE EXCEPTION
      'V5.2 structured-domain terminal ACL is unsafe';
  END IF;
END
$migration$;

COMMENT ON FUNCTION
  memory.plan_owner_v5_local_packet_disposition_v1(integer)
IS 'Owner-scoped planner for legacy V5 deferrals plus exact V5.2 content-free structured-domain terminal deferrals. V5.2 relational content remains isolated.';

COMMENT ON FUNCTION memory.guard_v5_legacy_packet_lane_v1()
IS 'Guards legacy review artifacts, reviewed V5.2 ambiguous deferrals, and exact V5.2 content-free structured-domain terminal dispositions. Other V5.2 downstream entry remains prohibited.';

COMMIT;
