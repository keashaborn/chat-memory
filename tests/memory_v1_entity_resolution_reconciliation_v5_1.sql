BEGIN;
SET LOCAL ROLE brains_app;
SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

DO $test$
DECLARE
  checked record;
BEGIN
  SELECT * INTO STRICT checked
  FROM memory.preflight_entity_resolution_reconciliation_v5_1(
    'f3d091e2-0fb9-4889-89c9-918903a82570'::uuid,
    '227c428d-aacf-5a8b-b938-28efed348860'::uuid,
    '3cf07024-5b6b-4ae0-b453-b6cae4860720'::uuid,
    'The unique active owner entity has prior governed role history for the exact family:father relationship role.'
  );
  IF checked.match_basis <> 'unique_owner_role_history'
     OR checked.source_action::text <> 'create_new'
     OR checked.source_decision_state::text <> 'manual_review_required' THEN
    RAISE EXCEPTION 'valid V5.1 reconciliation preflight drifted';
  END IF;

  BEGIN
    PERFORM * FROM memory.preflight_entity_resolution_apply_v5_1(
      '76f10e7e-b370-4edf-ba73-3148d0a6fb81'::uuid,
      NULL
    );
    RAISE EXCEPTION 'legacy V5 plan crossed the V5.1 apply boundary';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;

  BEGIN
    INSERT INTO memory.entity_resolution_reconciliation_v5_1(
      owner_user_id, reconciliation_id, request_id,
      source_resolution_id, successor_resolution_id, target_entity_id,
      match_basis, source_decision_sha256, mention_sha256,
      target_state_sha256, candidate_set_sha256,
      successor_decision_sha256, reconciliation_manifest_sha256, reason
    ) VALUES (
      '1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid,
      gen_random_uuid(), gen_random_uuid(),
      'f3d091e2-0fb9-4889-89c9-918903a82570'::uuid,
      '227c428d-aacf-5a8b-b938-28efed348860'::uuid,
      '3cf07024-5b6b-4ae0-b453-b6cae4860720'::uuid,
      'unique_owner_role_history', repeat('a',64), repeat('b',64),
      repeat('c',64), repeat('d',64), repeat('e',64), repeat('f',64),
      'direct table writes are forbidden'
    );
    RAISE EXCEPTION 'brains_app directly inserted reconciliation data';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END
$test$;

SELECT set_config(
  'app.user_id',
  '557ea042-cb82-48f8-9429-472e96c957ef',
  true
);
DO $cross_owner$
BEGIN
  BEGIN
    PERFORM *
    FROM memory.preflight_entity_resolution_reconciliation_v5_1(
      'f3d091e2-0fb9-4889-89c9-918903a82570'::uuid,
      '227c428d-aacf-5a8b-b938-28efed348860'::uuid,
      '3cf07024-5b6b-4ae0-b453-b6cae4860720'::uuid,
      'Cross-owner access must fail.'
    );
    RAISE EXCEPTION 'cross-owner reconciliation preflight passed';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$cross_owner$;

ROLLBACK;
