\set ON_ERROR_STOP on

BEGIN;

DO $schema$
BEGIN
  IF NOT (
    SELECT relrowsecurity AND relforcerowsecurity
    FROM pg_class
    WHERE oid=
      'memory.claim_temporal_reconciliation_review_v5_2'::regclass
  ) OR NOT (
    SELECT relrowsecurity AND relforcerowsecurity
    FROM pg_class
    WHERE oid=
      'memory.claim_temporal_reconciliation_apply_v5_2'::regclass
  ) THEN
    RAISE EXCEPTION 'temporal reconciliation RLS is not forced';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid=
      'memory.claim_temporal_reconciliation_review_v5_2'::regclass
      AND tgname='claim_temporal_reconciliation_review_v5_2_append_only'
      AND tgenabled='O'
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid=
      'memory.claim_temporal_reconciliation_apply_v5_2'::regclass
      AND tgname='claim_temporal_reconciliation_apply_v5_2_append_only'
      AND tgenabled='O'
  ) THEN
    RAISE EXCEPTION 'temporal reconciliation append-only guards are absent';
  END IF;
  IF has_table_privilege(
       'brains_app',
       'memory.claim_temporal_reconciliation_review_v5_2',
       'SELECT,INSERT,UPDATE,DELETE'
     )
     OR has_table_privilege(
       'brains_app',
       'memory.claim_temporal_reconciliation_apply_v5_2',
       'SELECT,INSERT,UPDATE,DELETE'
     ) THEN
    RAISE EXCEPTION 'brains_app has direct temporal reconciliation access';
  END IF;
  IF NOT has_function_privilege(
       'brains_app',
       'memory.preflight_claim_temporal_reconciliation_v5_2(uuid,uuid)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.review_claim_temporal_reconciliation_v5_2(uuid,uuid,uuid,jsonb,text,text,text,text)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.preflight_claim_temporal_reconciliation_apply_v5_2(uuid,uuid)',
       'EXECUTE'
     )
     OR NOT has_function_privilege(
       'brains_app',
       'memory.apply_claim_temporal_reconciliation_v5_2(uuid,uuid,uuid,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'restricted temporal reconciliation entrypoints missing';
  END IF;
END
$schema$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

DO $owner_preflight$
DECLARE
  value record;
BEGIN
  SELECT * INTO STRICT value
  FROM memory.preflight_claim_temporal_reconciliation_v5_2(
    'bd20dd0a-9fa0-4a21-8a93-e828c8044150'::uuid,
    'bc8866ad-95e8-4413-832e-813f601eece6'::uuid
  );
  IF value.current_status<>'supported'
     OR value.from_temporal_state<>'current'
     OR value.target_temporal_state<>'historical'
     OR value.current_revision_number<>2
     OR value.corroborating_claim_id
          <>'186335e4-ef19-43fb-ad05-f350c18461c1'::uuid
     OR value.desired_canonical_text
          <>'The user formerly had a pet named Neko.'
     OR value.desired_valid_to
          <>'2026-07-28 04:04:34.272603+00'::timestamptz
     OR value.authorization_manifest_sha256
          !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'exact owner preflight changed';
  END IF;
END
$owner_preflight$;

SELECT set_config(
  'app.user_id',
  '557ea042-cb82-48f8-9429-472e96c957ef',
  true
);
DO $cross_owner$
BEGIN
  BEGIN
    PERFORM *
    FROM memory.preflight_claim_temporal_reconciliation_v5_2(
      'bd20dd0a-9fa0-4a21-8a93-e828c8044150'::uuid,
      'bc8866ad-95e8-4413-832e-813f601eece6'::uuid
    );
    RAISE EXCEPTION 'cross-owner temporal preflight unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$cross_owner$;

RESET SESSION AUTHORIZATION;
ROLLBACK;
