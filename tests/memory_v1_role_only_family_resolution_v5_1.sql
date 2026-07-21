BEGIN;
SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

DO $test$
DECLARE
  source_resolution constant uuid :=
    '62fcb028-ee13-4b0c-94ae-81b915ad283b'::uuid;
  successor_resolution constant uuid :=
    'eaf253ec-b34f-46a5-8f8c-a543d2679d4b'::uuid;
  reconcile_request constant uuid :=
    '58099a0c-409d-4c56-b3e5-3ff400ae4f6c'::uuid;
  review_request constant uuid :=
    'a2918c60-bf1f-4f73-92c0-52d9d724e90e'::uuid;
  apply_request constant uuid :=
    '733e33f2-fea3-4850-a64f-b1324b4b0e45'::uuid;
  reconcile_reason constant text :=
    'Create the unique reviewed owner-scoped family:mother role entity.';
  review_reason constant text :=
    'Approved because family:mother is a closed non-repeatable owner role and no owner-local mother entity exists.';
  checked record;
  reconciled record;
  reviewed record;
  applied record;
  review_preflight record;
  apply_preflight record;
  baseline jsonb;
  after_apply jsonb;
  after_replay jsonb;
BEGIN
  baseline := memory.clone_test_role_family_state(
    '1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
  );
  SELECT * INTO STRICT checked
  FROM memory.preflight_role_only_family_resolution_v5_1(
    source_resolution, successor_resolution, reconcile_reason
  );
  IF checked.relationship_role <> 'family:mother'
     OR checked.successor_action::text <> 'create_new'
     OR checked.target_entity_id IS NOT NULL
     OR checked.proposed_entity->>'identity_state' <> 'role_only'
     OR checked.proposed_entity->'canonical_name' <> 'null'::jsonb
     OR checked.proposed_entity ? 'relationship_role' THEN
    RAISE EXCEPTION 'family-role resolution preflight drifted';
  END IF;

  SELECT * INTO STRICT reconciled
  FROM memory.reconcile_role_only_family_resolution_v5_1(
    reconcile_request, source_resolution, successor_resolution,
    reconcile_reason, checked.reconciliation_manifest_sha256
  );
  IF reconciled.outcome <> 'applied'
     OR reconciled.successor_action::text <> 'create_new' THEN
    RAISE EXCEPTION 'family-role reconciliation was not applied';
  END IF;

  SELECT * INTO STRICT review_preflight
  FROM memory.preflight_entity_resolution_review_v5_1(
    successor_resolution, 'approved', review_reason
  );
  SELECT * INTO STRICT reviewed
  FROM memory.review_entity_resolution_v5_1(
    review_request, successor_resolution, 'approved', review_reason,
    review_preflight.authorization_manifest_sha256
  );
  IF reviewed.outcome <> 'applied' THEN
    RAISE EXCEPTION 'family-role review was not applied';
  END IF;

  SELECT * INTO STRICT apply_preflight
  FROM memory.preflight_role_only_family_apply_v5_1(
    successor_resolution, reviewed.review_id
  );
  SELECT * INTO STRICT applied
  FROM memory.apply_role_only_family_resolution_v5_1(
    apply_request, successor_resolution, reviewed.review_id,
    apply_preflight.apply_manifest_sha256
  );
  IF applied.outcome <> 'applied' OR applied.bindings_created <> 4 THEN
    RAISE EXCEPTION 'family-role apply count drifted: %', row_to_json(applied);
  END IF;
  after_apply := memory.clone_test_role_family_state(
    '1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
  );
  IF (after_apply->>'entity_count')::bigint
        <> (baseline->>'entity_count')::bigint + 1
     OR (after_apply->>'plan_count')::bigint
        <> (baseline->>'plan_count')::bigint + 1
     OR (after_apply->>'review_count')::bigint
        <> (baseline->>'review_count')::bigint + 1
     OR (after_apply->>'apply_count')::bigint
        <> (baseline->>'apply_count')::bigint + 1
     OR (after_apply->>'binding_count')::bigint
        <> (baseline->>'binding_count')::bigint + 4
     OR (after_apply->>'request_count')::bigint
        <> (baseline->>'request_count')::bigint + 2
     OR (after_apply->>'reconciliation_count')::bigint
        <> (baseline->>'reconciliation_count')::bigint + 1
     OR (after_apply->>'mother_count')::bigint <> 1
     OR (after_apply->>'death_binding_count')::bigint <> 1 THEN
    RAISE EXCEPTION 'bounded family-role state drifted: % -> %',
      baseline, after_apply;
  END IF;
  SELECT * INTO STRICT reconciled
  FROM memory.reconcile_role_only_family_resolution_v5_1(
    reconcile_request, source_resolution, successor_resolution,
    reconcile_reason, checked.reconciliation_manifest_sha256
  );
  IF reconciled.outcome <> 'replayed' THEN
    RAISE EXCEPTION 'family-role reconciliation replay wrote again';
  END IF;
  SELECT * INTO STRICT reviewed
  FROM memory.review_entity_resolution_v5_1(
    review_request, successor_resolution, 'approved', review_reason,
    review_preflight.authorization_manifest_sha256
  );
  IF reviewed.outcome <> 'replayed' THEN
    RAISE EXCEPTION 'family-role review replay wrote again';
  END IF;
  SELECT * INTO STRICT applied
  FROM memory.apply_role_only_family_resolution_v5_1(
    apply_request, successor_resolution, reviewed.review_id,
    apply_preflight.apply_manifest_sha256
  );
  IF applied.outcome <> 'replayed' OR applied.bindings_created <> 0 THEN
    RAISE EXCEPTION 'family-role apply replay wrote again';
  END IF;
  after_replay := memory.clone_test_role_family_state(
    '1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
  );
  IF after_replay <> after_apply THEN
    RAISE EXCEPTION 'family-role replay changed canonical state';
  END IF;
  BEGIN
    INSERT INTO memory.entity_role_resolution_v5_1(
      owner_user_id, request_id, source_resolution_id,
      successor_resolution_id, relationship_role, successor_action,
      source_decision_sha256, mention_sha256, candidate_set_sha256,
      successor_decision_sha256, reconciliation_manifest_sha256, reason
    ) VALUES (
      memory.current_actor_user_id(), gen_random_uuid(),
      source_resolution, gen_random_uuid(), 'family:mother', 'create_new',
      repeat('a',64), repeat('b',64), repeat('c',64),
      repeat('d',64), repeat('e',64), 'direct write must fail'
    );
    RAISE EXCEPTION 'brains_app directly inserted family-role data';
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
    FROM memory.preflight_role_only_family_resolution_v5_1(
      '62fcb028-ee13-4b0c-94ae-81b915ad283b'::uuid,
      'eaf253ec-b34f-46a5-8f8c-a543d2679d4b'::uuid,
      'Cross-owner access must fail.'
    );
    RAISE EXCEPTION 'cross-owner family-role preflight passed';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$cross_owner$;

ROLLBACK;
