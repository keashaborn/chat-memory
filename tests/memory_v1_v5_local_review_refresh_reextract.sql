BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='120s';
SELECT set_config('test.prior_packet',:'prior_packet',true);
SELECT set_config('test.review_report_sha256',:'review_report_sha256',true);
SELECT set_config('test.stage_bundle_sha256',:'stage_bundle_sha256',true);
SELECT set_config('test.content_sha256',:'content_sha256',true);
SELECT set_config('test.packet_storage_sha256',:'packet_storage_sha256',true);

DO $acl$
BEGIN
  IF NOT has_function_privilege(
       'brains_app',
       'memory.plan_owner_v5_local_review_refresh_reextract_v1(uuid,text,text)',
       'EXECUTE'
     ) OR NOT has_function_privilege(
       'brains_app',
       'memory.enqueue_owner_v5_local_review_refresh_reextract_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text)',
       'EXECUTE'
     ) THEN
    RAISE EXCEPTION 'brains_app review refresh function ACL is absent';
  END IF;
END
$acl$;

SELECT set_config('app.user_id',:'target_owner',true);

DO $plan$
BEGIN
  IF (
    SELECT count(*)
    FROM memory.plan_owner_v5_local_review_refresh_reextract_v1(
      current_setting('test.prior_packet')::uuid,
      current_setting('test.review_report_sha256'),
      current_setting('test.stage_bundle_sha256')
    )
  )<>1 THEN
    RAISE EXCEPTION 'target-owner review refresh plan is not exact';
  END IF;
END
$plan$;

SELECT * FROM memory.enqueue_owner_v5_local_review_refresh_reextract_v1(
  :'operation_id'::uuid,:'new_job_id'::uuid,:'new_terminal_id'::uuid,
  :'prior_packet'::uuid,:'content_sha256',:'packet_storage_sha256',
  :'review_report_sha256',:'stage_bundle_sha256',
  '20260719_v5_self_bootstrap_review_refresh_v1',
  'memory_v1_local_policy_compiler_v6'
);

SELECT * FROM memory.enqueue_owner_v5_local_review_refresh_reextract_v1(
  :'operation_id'::uuid,:'new_job_id'::uuid,:'new_terminal_id'::uuid,
  :'prior_packet'::uuid,:'content_sha256',:'packet_storage_sha256',
  :'review_report_sha256',:'stage_bundle_sha256',
  '20260719_v5_self_bootstrap_review_refresh_v1',
  'memory_v1_local_policy_compiler_v6'
);

DO $post_apply$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_local_review_refresh_reextract_v1(
      current_setting('test.prior_packet')::uuid,
      current_setting('test.review_report_sha256'),
      current_setting('test.stage_bundle_sha256')
    )
  ) THEN
    RAISE EXCEPTION 'applied review refresh remains plannable';
  END IF;
END
$post_apply$;

SELECT set_config('app.user_id',:'other_owner',true);

DO $cross_owner$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_local_review_refresh_reextract_v1(
      current_setting('test.prior_packet')::uuid,
      current_setting('test.review_report_sha256'),
      current_setting('test.stage_bundle_sha256')
    )
  ) THEN
    RAISE EXCEPTION 'cross-owner review refresh plan leaked';
  END IF;
  BEGIN
    PERFORM * FROM memory.enqueue_owner_v5_local_review_refresh_reextract_v1(
      gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),
      current_setting('test.prior_packet')::uuid,
      current_setting('test.content_sha256'),
      current_setting('test.packet_storage_sha256'),
      current_setting('test.review_report_sha256'),
      current_setting('test.stage_bundle_sha256'),
      '20260719_v5_self_bootstrap_review_refresh_v1',
      'memory_v1_local_policy_compiler_v6'
    );
    RAISE EXCEPTION 'cross-owner review refresh unexpectedly succeeded';
  EXCEPTION
    WHEN check_violation THEN NULL;
  END;
END
$cross_owner$;

ROLLBACK;
SELECT 'memory_v1_v5_local_review_refresh_reextract: PASS' AS result;
