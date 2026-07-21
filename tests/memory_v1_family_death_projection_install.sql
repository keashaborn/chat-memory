BEGIN;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

DO $owner_checks$
DECLARE
  checked record;
BEGIN
  SELECT * INTO STRICT checked
  FROM memory.preflight_role_only_family_resolution_v5_1(
    '62fcb028-ee13-4b0c-94ae-81b915ad283b'::uuid,
    'eaf253ec-b34f-46a5-8f8c-a543d2679d4b'::uuid,
    'Rollback-only installation preflight for the owner family:mother role.'
  );
  IF checked.relationship_role <> 'family:mother'
     OR checked.successor_action::text <> 'create_new' THEN
    RAISE EXCEPTION 'owner family-role installation preflight drifted';
  END IF;
  BEGIN
    PERFORM * FROM memory.preflight_claim_projection_source_v5_1(
      'ab9d3c93-533f-4486-b831-67e9cc82fe54'::uuid
    );
    RAISE EXCEPTION 'unbound death observation entered claim projection';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
  BEGIN
    INSERT INTO memory.entity_role_resolution_v5_1(
      owner_user_id,request_id,source_resolution_id,
      successor_resolution_id,relationship_role,successor_action,
      source_decision_sha256,mention_sha256,candidate_set_sha256,
      successor_decision_sha256,reconciliation_manifest_sha256,reason
    ) VALUES (
      memory.current_actor_user_id(),gen_random_uuid(),
      '62fcb028-ee13-4b0c-94ae-81b915ad283b'::uuid,
      gen_random_uuid(),'family:mother','create_new',
      repeat('a',64),repeat('b',64),repeat('c',64),
      repeat('d',64),repeat('e',64),'direct write must fail'
    );
    RAISE EXCEPTION 'brains_app directly inserted family-role data';
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;
  END;
END
$owner_checks$;

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
DO $cross_owner$
BEGIN
  BEGIN
    PERFORM * FROM memory.preflight_role_only_family_resolution_v5_1(
      '62fcb028-ee13-4b0c-94ae-81b915ad283b'::uuid,
      'eaf253ec-b34f-46a5-8f8c-a543d2679d4b'::uuid,
      'Cross-owner installation preflight must fail.'
    );
    RAISE EXCEPTION 'cross-owner family-role preflight passed';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN
    NULL;
  END;
END
$cross_owner$;

ROLLBACK;
