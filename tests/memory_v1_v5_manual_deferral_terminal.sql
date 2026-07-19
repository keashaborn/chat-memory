\set ON_ERROR_STOP on
BEGIN;
SET LOCAL statement_timeout='60s';
SET LOCAL lock_timeout='5s';
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id', :'owner_user_id', true);
SELECT set_config('app.test_packet_id', :'packet_id', true);
SELECT set_config(
  'app.test_packet_storage_sha256', :'packet_storage_sha256', true
);

DO $test$
DECLARE
  planned record;
  applied record;
  replayed record;
BEGIN
  SELECT * INTO planned
  FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
  WHERE packet_id=current_setting('app.test_packet_id')::uuid;
  IF NOT FOUND
     OR planned.disposition_route<>'terminal_deferral'
     OR planned.reason_code<>'deferral_only_review_unresolved'
     OR planned.entity_mention_count<>0
     OR planned.observation_count<>0
     OR planned.comparison_hint_count<>0
     OR planned.deferral_count<1 THEN
    RAISE EXCEPTION 'manual deferral packet did not route to terminal review-unresolved';
  END IF;

  BEGIN
    PERFORM * FROM memory.finalize_owner_v5_local_deferral_v1(
      '9b1f68f0-a557-5d14-bdf1-a8abfb2bdd21'::uuid,
      '37658ef4-01a5-59ce-a865-196b2ecc0da7'::uuid,
      current_setting('app.test_packet_id')::uuid,
      current_setting('app.test_packet_storage_sha256'),
      'deferral_only_no_stage'
    );
    RAISE EXCEPTION 'manual-review packet accepted the non-review reason';
  EXCEPTION WHEN check_violation THEN NULL;
  END;

  SELECT * INTO applied
  FROM memory.finalize_owner_v5_local_deferral_v1(
    '7f799bf4-c0be-5434-a9f7-a220e34f8f45'::uuid,
    '0f63c218-d885-5dbc-9b42-e8fb91f7bf19'::uuid,
    current_setting('app.test_packet_id')::uuid,
    current_setting('app.test_packet_storage_sha256'),
    'deferral_only_review_unresolved'
  );
  SELECT * INTO replayed
  FROM memory.finalize_owner_v5_local_deferral_v1(
    '7f799bf4-c0be-5434-a9f7-a220e34f8f45'::uuid,
    '0f63c218-d885-5dbc-9b42-e8fb91f7bf19'::uuid,
    current_setting('app.test_packet_id')::uuid,
    current_setting('app.test_packet_storage_sha256'),
    'deferral_only_review_unresolved'
  );
  IF applied.apply_outcome<>'applied'
     OR applied.reason_code<>'deferral_only_review_unresolved'
     OR replayed.apply_outcome<>'replayed'
     OR replayed.disposition_id<>applied.disposition_id THEN
    RAISE EXCEPTION 'manual deferral apply/replay invariant failed';
  END IF;
END
$test$;

SELECT set_config('app.user_id', :'other_owner_user_id', true);
DO $isolation$
BEGIN
  BEGIN
    PERFORM * FROM memory.finalize_owner_v5_local_deferral_v1(
      '1b1b2d00-c8f8-5ce2-88db-0fbb20da61f0'::uuid,
      '69c42a47-4a62-5e21-9163-0819eb8f969d'::uuid,
      current_setting('app.test_packet_id')::uuid,
      current_setting('app.test_packet_storage_sha256'),
      'deferral_only_review_unresolved'
    );
    RAISE EXCEPTION 'cross-owner manual deferral unexpectedly succeeded';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
END
$isolation$;

RESET SESSION AUTHORIZATION;
ROLLBACK;
