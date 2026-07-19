BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $rollback$
DECLARE
  planner_definition text;
  finalizer_definition text;
  terminal_condition constant text :=
$old$                   AND packet.deferral_count>0$old$;
  old_terminal_condition constant text :=
$new$                   AND packet.deferral_count>0
                   AND NOT packet.manual_review_required$new$;
  reason_case constant text :=
$old$         THEN CASE WHEN packet.manual_review_required
              THEN 'deferral_only_review_unresolved'
              ELSE 'deferral_only_no_stage' END
         ELSE 'manual_review_required' END,$old$;
  old_reason_case constant text :=
$new$         THEN 'deferral_only_no_stage' ELSE 'manual_review_required' END,$new$;
  input_guard constant text :=
$old$     OR p_reason_code NOT IN (
       'deferral_only_no_stage','deferral_only_review_unresolved'
     ) THEN$old$;
  old_input_guard constant text :=
$new$     OR p_reason_code<>'deferral_only_no_stage' THEN$new$;
  review_guard constant text :=
$old$     OR (
       p_reason_code='deferral_only_no_stage'
       AND packet.manual_review_required
     )
     OR (
       p_reason_code='deferral_only_review_unresolved'
       AND (
         NOT packet.manual_review_required
         OR NOT (
           packet.normalized_packet
           @? '$.deferrals[*] ? (@.review_required == true)'
         )
       )
     )
     OR packet.entity_mention_count<>0 OR packet.observation_count<>0$old$;
  old_review_guard constant text :=
$new$     OR packet.manual_review_required
     OR packet.entity_mention_count<>0 OR packet.observation_count<>0$new$;
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'manual deferral terminal rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.v5_local_packet_disposition
    WHERE reason_code='deferral_only_review_unresolved'
  ) THEN
    RAISE EXCEPTION 'manual deferral terminal rows exist; rollback refused';
  END IF;

  SELECT pg_get_functiondef(
    'memory.plan_owner_v5_local_packet_disposition_v1(integer)'::regprocedure
  ) INTO planner_definition;
  IF strpos(planner_definition,'deferral_only_review_unresolved')>0 THEN
    IF strpos(planner_definition,reason_case)=0
       OR regexp_count(
            planner_definition,
            'AND packet\.deferral_count>0'
          )<>3 THEN
      RAISE EXCEPTION 'patched planner definition drifted';
    END IF;
    planner_definition:=replace(
      planner_definition,reason_case,old_reason_case
    );
    planner_definition:=replace(
      planner_definition,terminal_condition,old_terminal_condition
    );
    EXECUTE planner_definition;
  END IF;

  SELECT pg_get_functiondef(
    'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)'::regprocedure
  ) INTO finalizer_definition;
  IF strpos(finalizer_definition,'deferral_only_review_unresolved')>0 THEN
    IF strpos(finalizer_definition,input_guard)=0
       OR strpos(finalizer_definition,review_guard)=0 THEN
      RAISE EXCEPTION 'patched finalizer definition drifted';
    END IF;
    finalizer_definition:=replace(
      finalizer_definition,input_guard,old_input_guard
    );
    finalizer_definition:=replace(
      finalizer_definition,review_guard,old_review_guard
    );
    EXECUTE finalizer_definition;
  END IF;

  ALTER TABLE memory.v5_local_packet_disposition
    DROP CONSTRAINT v5_local_packet_disposition_reason_code_check;
  ALTER TABLE memory.v5_local_packet_disposition
    ADD CONSTRAINT v5_local_packet_disposition_reason_code_check
    CHECK (reason_code='deferral_only_no_stage');
END
$rollback$;

COMMIT;
