\set ON_ERROR_STOP on
BEGIN;

SELECT set_config(
  'app.user_id',
  :'owner_user_id',
  true
);
SELECT set_config('test.owner_user_id', :'owner_user_id', true);
SELECT set_config(
  'test.other_owner_user_id',
  :'other_owner_user_id',
  true
);
SELECT set_config('test.evidence_id', :'evidence_id', true);
SELECT set_config('test.prior_packet_id', :'prior_packet_id', true);
SELECT set_config(
  'test.replacement_packet_id',
  :'replacement_packet_id',
  true
);
SELECT set_config(
  'test.prior_packet_storage_sha256',
  :'prior_packet_storage_sha256',
  true
);
SELECT set_config(
  'test.replacement_packet_storage_sha256',
  :'replacement_packet_storage_sha256',
  true
);

DO $test$
DECLARE
  planned record;
  applied record;
  replayed record;
  authoritative uuid;
  routed record;
BEGIN
  SELECT * INTO planned
  FROM memory.plan_owner_v5_2_duplicate_packet_supersession_v1(
    current_setting('test.prior_packet_id')::uuid,
    current_setting('test.replacement_packet_id')::uuid
  );
  IF NOT FOUND
     OR planned.prior_packet_storage_sha256 <>
        current_setting('test.prior_packet_storage_sha256')
     OR planned.replacement_packet_storage_sha256 <>
        current_setting('test.replacement_packet_storage_sha256')
     OR planned.reason_code <>
        'duplicate_active_packet_reconciled' THEN
    RAISE EXCEPTION 'exact duplicate packet plan failed';
  END IF;

  SELECT * INTO applied
  FROM memory.finalize_owner_v5_2_duplicate_packet_supersession_v1(
    '28bc15e4-1220-59f7-bbb8-8b1a190d3a68'::uuid,
    '1a60c02f-d64a-5bc5-9934-54be7a413d8c'::uuid,
    current_setting('test.prior_packet_id')::uuid,
    current_setting('test.replacement_packet_id')::uuid,
    current_setting('test.prior_packet_storage_sha256'),
    current_setting('test.replacement_packet_storage_sha256'),
    'duplicate_active_packet_reconciled'
  );
  SELECT * INTO replayed
  FROM memory.finalize_owner_v5_2_duplicate_packet_supersession_v1(
    '28bc15e4-1220-59f7-bbb8-8b1a190d3a68'::uuid,
    '1a60c02f-d64a-5bc5-9934-54be7a413d8c'::uuid,
    current_setting('test.prior_packet_id')::uuid,
    current_setting('test.replacement_packet_id')::uuid,
    current_setting('test.prior_packet_storage_sha256'),
    current_setting('test.replacement_packet_storage_sha256'),
    'duplicate_active_packet_reconciled'
  );
  IF applied.apply_outcome <> 'applied'
     OR replayed.apply_outcome <> 'replayed' THEN
    RAISE EXCEPTION 'duplicate packet replay proof failed';
  END IF;

  authoritative :=
    memory.authoritative_owner_v5_2_packet_id_v1(
      current_setting('test.evidence_id')::uuid
    );
  IF authoritative <>
     current_setting('test.replacement_packet_id')::uuid THEN
    RAISE EXCEPTION 'replacement packet did not become authoritative';
  END IF;

  SELECT * INTO routed
  FROM memory.plan_owner_v5_2_exact_packet_route_v1(
    current_setting('test.replacement_packet_id')::uuid
  );
  IF NOT FOUND
     OR routed.route <> 'manual_review_artifact_ready' THEN
    RAISE EXCEPTION 'authoritative replacement did not become routable';
  END IF;
END
$test$;

SELECT set_config(
  'app.user_id',
  :'other_owner_user_id',
  true
);

DO $cross_owner$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.plan_owner_v5_2_duplicate_packet_supersession_v1(
      current_setting('test.prior_packet_id')::uuid,
      current_setting('test.replacement_packet_id')::uuid
    )
  ) THEN
    RAISE EXCEPTION 'cross-owner duplicate packet became visible';
  END IF;

  BEGIN
    PERFORM *
    FROM memory.finalize_owner_v5_2_duplicate_packet_supersession_v1(
      '28bc15e4-1220-59f7-bbb8-8b1a190d3a68'::uuid,
      '1a60c02f-d64a-5bc5-9934-54be7a413d8c'::uuid,
      current_setting('test.prior_packet_id')::uuid,
      current_setting('test.replacement_packet_id')::uuid,
      current_setting('test.prior_packet_storage_sha256'),
      current_setting('test.replacement_packet_storage_sha256'),
      'duplicate_active_packet_reconciled'
    );
    RAISE EXCEPTION 'cross-owner duplicate packet apply succeeded';
  EXCEPTION
    WHEN check_violation THEN NULL;
  END;
END
$cross_owner$;

ROLLBACK;
