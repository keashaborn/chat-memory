BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $migration$
DECLARE
  constraint_definition text;
  planner_definition text;
  finalizer_definition text;
  old_terminal_condition constant text :=
$old$                   AND packet.deferral_count>0
                   AND NOT packet.manual_review_required$old$;
  terminal_condition constant text :=
$new$                   AND packet.deferral_count>0$new$;
  old_reason_case constant text :=
$old$         THEN 'deferral_only_no_stage' ELSE 'manual_review_required' END,$old$;
  reason_case constant text :=
$new$         THEN CASE WHEN packet.manual_review_required
              THEN 'deferral_only_review_unresolved'
              ELSE 'deferral_only_no_stage' END
         ELSE 'manual_review_required' END,$new$;
  old_input_guard constant text :=
$old$     OR p_reason_code<>'deferral_only_no_stage' THEN$old$;
  input_guard constant text :=
$new$     OR p_reason_code NOT IN (
       'deferral_only_no_stage','deferral_only_review_unresolved'
     ) THEN$new$;
  old_review_guard constant text :=
$old$     OR packet.manual_review_required
     OR packet.entity_mention_count<>0 OR packet.observation_count<>0$old$;
  review_guard constant text :=
$new$     OR (
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
     OR packet.entity_mention_count<>0 OR packet.observation_count<>0$new$;
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'manual deferral terminal compatibility requires sage';
  END IF;
  IF to_regclass('memory.v5_local_packet_disposition') IS NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_local_packet_disposition_v1(integer)'
     ) IS NULL
     OR to_regprocedure(
       'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'manual deferral terminal prerequisites are absent';
  END IF;

  SELECT pg_get_constraintdef(oid) INTO constraint_definition
  FROM pg_constraint
  WHERE conrelid='memory.v5_local_packet_disposition'::regclass
    AND conname='v5_local_packet_disposition_reason_code_check';
  IF constraint_definition='CHECK ((reason_code = ''deferral_only_no_stage''::text))' THEN
    ALTER TABLE memory.v5_local_packet_disposition
      DROP CONSTRAINT v5_local_packet_disposition_reason_code_check;
    ALTER TABLE memory.v5_local_packet_disposition
      ADD CONSTRAINT v5_local_packet_disposition_reason_code_check
      CHECK (reason_code IN (
        'deferral_only_no_stage','deferral_only_review_unresolved'
      )) NOT VALID;
    ALTER TABLE memory.v5_local_packet_disposition
      VALIDATE CONSTRAINT v5_local_packet_disposition_reason_code_check;
  ELSIF constraint_definition NOT LIKE '%deferral_only_no_stage%'
        OR constraint_definition NOT LIKE '%deferral_only_review_unresolved%' THEN
    RAISE EXCEPTION 'unexpected disposition reason constraint: %',
      constraint_definition;
  END IF;

  SELECT pg_get_functiondef(
    'memory.plan_owner_v5_local_packet_disposition_v1(integer)'::regprocedure
  ) INTO planner_definition;
  IF strpos(planner_definition,'deferral_only_review_unresolved')=0 THEN
    IF regexp_count(
         planner_definition,
         'AND NOT packet\.manual_review_required'
       )<>3
       OR strpos(planner_definition,old_reason_case)=0 THEN
      RAISE EXCEPTION 'packet disposition planner definition drifted';
    END IF;
    planner_definition:=replace(
      planner_definition,old_terminal_condition,terminal_condition
    );
    planner_definition:=replace(
      planner_definition,old_reason_case,reason_case
    );
    EXECUTE planner_definition;
  ELSIF regexp_count(
          planner_definition,
          'AND NOT packet\.manual_review_required'
        )<>0
        OR strpos(planner_definition,reason_case)=0 THEN
    RAISE EXCEPTION 'patched packet disposition planner is inconsistent';
  END IF;

  SELECT pg_get_functiondef(
    'memory.finalize_owner_v5_local_deferral_v1(uuid,uuid,uuid,text,text)'::regprocedure
  ) INTO finalizer_definition;
  IF strpos(finalizer_definition,'deferral_only_review_unresolved')=0 THEN
    IF strpos(finalizer_definition,old_input_guard)=0
       OR strpos(finalizer_definition,old_review_guard)=0 THEN
      RAISE EXCEPTION 'packet disposition finalizer definition drifted';
    END IF;
    finalizer_definition:=replace(
      finalizer_definition,old_input_guard,input_guard
    );
    finalizer_definition:=replace(
      finalizer_definition,old_review_guard,review_guard
    );
    EXECUTE finalizer_definition;
  ELSIF strpos(finalizer_definition,input_guard)=0
        OR strpos(finalizer_definition,review_guard)=0 THEN
    RAISE EXCEPTION 'patched packet disposition finalizer is inconsistent';
  END IF;
END
$migration$;

COMMENT ON FUNCTION memory.plan_owner_v5_local_packet_disposition_v1(integer)
IS 'Routes content-free deferral packets to an audited terminal disposition, retaining whether unresolved review was requested.';
COMMENT ON FUNCTION memory.finalize_owner_v5_local_deferral_v1(
  uuid,uuid,uuid,text,text
)
IS 'Append-only terminal disposition for zero-stage deferral packets, including review-requested but structurally unstageable packets.';

COMMIT;
