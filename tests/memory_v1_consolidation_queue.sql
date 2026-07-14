\set ON_ERROR_STOP on
BEGIN;

SET LOCAL ROLE brains_app;
SELECT set_config('app.user_id', '557ea042-cb82-48f8-9429-472e96c957ef', true);

INSERT INTO public.chat_log(
  id, owner_user_id, user_id, source, text, created_at
) VALUES (
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc1',
  '557ea042-cb82-48f8-9429-472e96c957ef',
  '557ea042-cb82-48f8-9429-472e96c957ef',
  'frontend/chat:user',
  'Synthetic consolidation queue test.',
  clock_timestamp()
);

DO $block$
DECLARE
  job uuid;
  token uuid := 'cccccccc-cccc-4ccc-8ccc-ccccccccccc2';
  failed boolean;
BEGIN
  SELECT job_id INTO job
  FROM memory.consolidation_job
  WHERE owner_user_id='557ea042-cb82-48f8-9429-472e96c957ef'
    AND source_external_id='cccccccc-cccc-4ccc-8ccc-ccccccccccc1';
  IF job IS NULL THEN
    RAISE EXCEPTION 'chat_log insert did not enqueue consolidation';
  END IF;

  IF (SELECT count(*) FROM memory.consolidation_event
      WHERE owner_user_id='557ea042-cb82-48f8-9429-472e96c957ef'
        AND job_id=job AND event_type='queued') <> 1 THEN
    RAISE EXCEPTION 'queue event missing';
  END IF;

  UPDATE memory.consolidation_job
  SET status='processing', attempts=attempts+1,
      lease_token=token, lease_expires_at=clock_timestamp()+interval '5 minutes',
      worker_id='sql-test'
  WHERE owner_user_id='557ea042-cb82-48f8-9429-472e96c957ef'
    AND job_id=job;

  UPDATE memory.consolidation_job
  SET status='review_required', lease_token=NULL, lease_expires_at=NULL,
      result='{"route":"candidate_review"}'::jsonb
  WHERE owner_user_id='557ea042-cb82-48f8-9429-472e96c957ef'
    AND job_id=job AND lease_token=token;

  failed := false;
  BEGIN
    INSERT INTO memory.consolidation_job(
      owner_user_id, source_system, source_external_id,
      source_sha256, source_recorded_at
    ) VALUES (
      '557ea042-cb82-48f8-9429-472e96c957ef',
      'test', 'forbidden-direct-insert', repeat('a',64), clock_timestamp()
    );
  EXCEPTION WHEN insufficient_privilege THEN
    failed := true;
  END;
  IF NOT failed THEN
    RAISE EXCEPTION 'brains_app direct queue insert was accepted';
  END IF;

  failed := false;
  BEGIN
    UPDATE memory.consolidation_event
    SET details='{"tampered":true}'::jsonb
    WHERE owner_user_id='557ea042-cb82-48f8-9429-472e96c957ef'
      AND job_id=job;
  EXCEPTION WHEN insufficient_privilege THEN
    failed := true;
  END;
  IF NOT failed THEN
    RAISE EXCEPTION 'append-only consolidation event was mutable';
  END IF;
END
$block$;

SELECT set_config('app.user_id', '1240822d-ac9a-4096-95aa-e2b24d36ef50', true);
DO $block$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.consolidation_job
    WHERE source_external_id='cccccccc-cccc-4ccc-8ccc-ccccccccccc1'
  ) THEN
    RAISE EXCEPTION 'cross-owner queue row was visible';
  END IF;
END
$block$;

RESET ROLE;
ROLLBACK;
