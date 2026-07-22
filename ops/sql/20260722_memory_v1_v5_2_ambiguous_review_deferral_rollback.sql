BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DO $preflight$
BEGIN
  IF current_user<>'sage' THEN
    RAISE EXCEPTION 'V5.2 ambiguous review deferral rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.v5_local_packet_disposition
    WHERE reason_code='ambiguous_transcription'
       OR review_decision IS NOT NULL
       OR review_basis_sha256 IS NOT NULL
  ) THEN
    RAISE EXCEPTION 'review deferral rows exist; rollback is unsafe';
  END IF;
END
$preflight$;

DROP TRIGGER IF EXISTS v5_local_disposition_stage_guard
  ON memory.relational_stage_batch;
DROP FUNCTION IF EXISTS memory.guard_disposed_evidence_from_stage_v1();
DROP FUNCTION IF EXISTS memory.finalize_owner_v5_2_review_deferral_v1(
  uuid,uuid,uuid,text,text,text
);

CREATE OR REPLACE FUNCTION memory.guard_v5_legacy_packet_lane_v1()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=''
AS $function$
BEGIN
  IF TG_OP<>'INSERT' OR NEW.owner_user_id IS NULL OR NEW.packet_id IS NULL THEN
    RAISE EXCEPTION 'legacy V5 packet lane accepts bounded inserts only'
      USING ERRCODE='23514';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM memory.evidence_extraction_packet_v5_local AS packet
    WHERE packet.owner_user_id=NEW.owner_user_id
      AND packet.packet_id=NEW.packet_id
      AND packet.normalized_packet->>'contract_version'
            ='memory_v1_relational_extraction_v5'
      AND packet.normalized_packet->>'predicate_registry_version'
            ='memory_predicate_registry_v5'
  ) THEN
    RAISE EXCEPTION 'non-V5 packet cannot enter the legacy packet lane'
      USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END
$function$;
ALTER FUNCTION memory.guard_v5_legacy_packet_lane_v1()
  OWNER TO memory_v5_local_disposition_maintainer;
REVOKE ALL ON FUNCTION memory.guard_v5_legacy_packet_lane_v1()
  FROM PUBLIC,brains_app,memory_v5_local_disposition_maintainer;

ALTER TABLE memory.v5_local_packet_disposition
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_review_contract_check,
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_promotion_eligible_check,
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_reason_code_check,
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_entity_mention_count_check,
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_observation_count_check,
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_comparison_hint_count_check,
  DROP CONSTRAINT IF EXISTS v5_local_packet_disposition_deferral_count_check;

ALTER TABLE memory.v5_local_packet_disposition
  DROP COLUMN review_decision,
  DROP COLUMN promotion_eligible,
  DROP COLUMN review_basis_sha256,
  ADD CONSTRAINT v5_local_packet_disposition_reason_code_check
    CHECK (reason_code IN (
      'deferral_only_no_stage','deferral_only_review_unresolved'
    )),
  ADD CONSTRAINT v5_local_packet_disposition_entity_mention_count_check
    CHECK (entity_mention_count=0),
  ADD CONSTRAINT v5_local_packet_disposition_observation_count_check
    CHECK (observation_count=0),
  ADD CONSTRAINT v5_local_packet_disposition_comparison_hint_count_check
    CHECK (comparison_hint_count=0),
  ADD CONSTRAINT v5_local_packet_disposition_deferral_count_check
    CHECK (deferral_count BETWEEN 1 AND 32);

DO $persistence$
DECLARE
  definition text;
  old_guard constant text :=
$old$OR p_policy_compiler_sha256 NOT IN (
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v1','UTF8'
       ),'sha256'),'hex'),
       encode(public.digest(convert_to(
         'memory_v1_semantic_policy_compiler_v2','UTF8'
       ),'sha256'),'hex')
     )$old$;
  new_guard constant text :=
$new$OR p_policy_compiler_sha256 <> encode(public.digest(convert_to(
       'memory_v1_semantic_policy_compiler_v1','UTF8'
     ),'sha256'),'hex')$new$;
BEGIN
  SELECT pg_get_functiondef(
    'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
  ) INTO definition;
  IF strpos(definition,'memory_v1_semantic_policy_compiler_v2')>0 THEN
    IF strpos(definition,old_guard)=0 THEN
      RAISE EXCEPTION 'V5.2 persistence rollback guard drifted';
    END IF;
    definition:=replace(definition,old_guard,new_guard);
    EXECUTE definition;
  END IF;
END
$persistence$;

COMMIT;
