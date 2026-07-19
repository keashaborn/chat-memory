BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='120s';
SELECT set_config('test.other_owner',:'other_owner',true);
SELECT set_config('test.prior_packet',:'prior_packet',true);
SELECT set_config('test.replacement_packet',:'replacement_packet',true);
SELECT set_config('test.prior_storage_sha256',:'prior_storage_sha256',true);
SELECT set_config(
  'test.replacement_storage_sha256',:'replacement_storage_sha256',true
);

DO $acl$
BEGIN
  IF has_table_privilege(
       'brains_app','memory.v5_local_packet_supersession','SELECT'
     ) OR has_table_privilege(
       'brains_app','memory.v5_local_packet_supersession','INSERT'
     ) THEN
    RAISE EXCEPTION 'brains_app received direct supersession table access';
  END IF;
  IF NOT has_function_privilege(
       'brains_app',
       'memory.plan_owner_v5_local_packet_supersession_v1(uuid,uuid)',
       'EXECUTE'
     ) OR NOT has_function_privilege(
       'brains_app',
       'memory.finalize_owner_v5_local_packet_supersession_v1(uuid,uuid,uuid,uuid,text,text,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'brains_app supersession function ACL is absent';
  END IF;
END
$acl$;

SELECT set_config('app.user_id',:'target_owner',true);

DO $plan$
BEGIN
  IF (
    SELECT count(*) FROM memory.plan_owner_v5_local_packet_supersession_v1(
      current_setting('test.prior_packet')::uuid,
      current_setting('test.replacement_packet')::uuid
    )
  )<>1 THEN
    RAISE EXCEPTION 'target-owner supersession plan is not exact';
  END IF;
END
$plan$;

SELECT * FROM memory.finalize_owner_v5_local_packet_supersession_v1(
  :'operation_id'::uuid,
  :'supersession_id'::uuid,
  :'prior_packet'::uuid,
  :'replacement_packet'::uuid,
  :'prior_storage_sha256',
  :'replacement_storage_sha256',
  'temporal_persistence_matrix_reextracted'
);

SELECT * FROM memory.finalize_owner_v5_local_packet_supersession_v1(
  :'operation_id'::uuid,
  :'supersession_id'::uuid,
  :'prior_packet'::uuid,
  :'replacement_packet'::uuid,
  :'prior_storage_sha256',
  :'replacement_storage_sha256',
  'temporal_persistence_matrix_reextracted'
);

DO $post_apply$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.plan_owner_v5_local_packet_supersession_v1(
      current_setting('test.prior_packet')::uuid,
      current_setting('test.replacement_packet')::uuid
    )
  ) THEN
    RAISE EXCEPTION 'supersession replay remains plannable';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
    WHERE packet_id=current_setting('test.prior_packet')::uuid
  ) THEN
    RAISE EXCEPTION 'superseded packet remains router-visible';
  END IF;
END
$post_apply$;

SELECT set_config('app.user_id',:'other_owner',true);

DO $cross_owner$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.plan_owner_v5_local_packet_supersession_v1(
      current_setting('test.prior_packet')::uuid,
      current_setting('test.replacement_packet')::uuid
    )
  ) THEN
    RAISE EXCEPTION 'cross-owner supersession plan leaked';
  END IF;
  BEGIN
    PERFORM * FROM memory.finalize_owner_v5_local_packet_supersession_v1(
      gen_random_uuid(),gen_random_uuid(),
      current_setting('test.prior_packet')::uuid,
      current_setting('test.replacement_packet')::uuid,
      current_setting('test.prior_storage_sha256'),
      current_setting('test.replacement_storage_sha256'),
      'temporal_persistence_matrix_reextracted'
    );
    RAISE EXCEPTION 'cross-owner supersession apply unexpectedly succeeded';
  EXCEPTION
    WHEN check_violation THEN NULL;
  END;
END
$cross_owner$;

ROLLBACK;
SELECT 'memory_v1_v5_local_packet_supersession: PASS' AS result;
