\set ON_ERROR_STOP on
BEGIN;

SELECT set_config(
  'app.user_id',
  '557ea042-cb82-48f8-9429-472e96c957ef',
  true
);

DO $block$
DECLARE
  enqueued integer;
  existing integer;
  sources jsonb := $json$
  [
    {"source_external_id":"2efbb10d-f07a-4936-beab-9e7155e433dd","source_sha256":"2de0989137c018f67b3386726d5222c604dba2ffae4a302bc57f2e55fece4681"},
    {"source_external_id":"387c34d3-e4a3-4d47-845f-f6a349fb37ae","source_sha256":"2bb5304cb478a28bf754b5aeb7c8380dc158e2b8882113deef7acc8e16a3ac40"},
    {"source_external_id":"b2b2f1c7-19da-4667-a93c-5eb7fe6aaa20","source_sha256":"b19c251bed3a6ef7c2e903a3eff2d863aee2ceda579a39653c86d7719d923e5a"},
    {"source_external_id":"8e22f07c-71cb-49b9-9ac9-e9898668eadb","source_sha256":"2beca45164773ebeb06b4dd4dacf233b3382234f607e473aa395be06cc384ab0"},
    {"source_external_id":"8e3e5fba-054c-4013-a317-3ca06ef53958","source_sha256":"0bdc0fa31eb8dcad80ce35a2ac92a9057ffb145e2f0147a141b6de5c6d1d6fbc"},
    {"source_external_id":"3cb6072d-70f4-455e-b2ec-0bd9cb24aa51","source_sha256":"263ec864fee06043e98e9208d697dd913e2722cc61d5d806fcad3d524b6e5ff7"}
  ]
  $json$::jsonb;
BEGIN
  SELECT result.enqueued_count, result.existing_count
  INTO enqueued, existing
  FROM memory.enqueue_consolidation_reextract('20260714_v4', sources) AS result;
  IF enqueued <> 6 OR existing <> 0 THEN
    RAISE EXCEPTION 'first replay enqueue mismatch: %, %', enqueued, existing;
  END IF;

  SELECT result.enqueued_count, result.existing_count
  INTO enqueued, existing
  FROM memory.enqueue_consolidation_reextract('20260714_v4', sources) AS result;
  IF enqueued <> 0 OR existing <> 6 THEN
    RAISE EXCEPTION 'idempotent replay mismatch: %, %', enqueued, existing;
  END IF;

  IF (SELECT count(*)
      FROM memory.consolidation_job
      WHERE owner_user_id='557ea042-cb82-48f8-9429-472e96c957ef'
        AND pipeline_version='20260714_v4'
        AND source_external_id IN (
          '2efbb10d-f07a-4936-beab-9e7155e433dd',
          '387c34d3-e4a3-4d47-845f-f6a349fb37ae',
          'b2b2f1c7-19da-4667-a93c-5eb7fe6aaa20',
          '8e22f07c-71cb-49b9-9ac9-e9898668eadb',
          '8e3e5fba-054c-4013-a317-3ca06ef53958',
          '3cb6072d-70f4-455e-b2ec-0bd9cb24aa51'
        )) <> 6 THEN
    RAISE EXCEPTION 'expected six extraction-v4 jobs';
  END IF;
END
$block$;

SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

DO $block$
DECLARE
  blocked boolean := false;
BEGIN
  BEGIN
    PERFORM *
    FROM memory.enqueue_consolidation_reextract(
      '20260714_v4',
      '[{"source_external_id":"2efbb10d-f07a-4936-beab-9e7155e433dd","source_sha256":"2de0989137c018f67b3386726d5222c604dba2ffae4a302bc57f2e55fece4681"}]'::jsonb
    );
  EXCEPTION WHEN no_data_found THEN
    blocked := true;
  END;
  IF NOT blocked THEN
    RAISE EXCEPTION 'cross-owner re-extraction enqueue was accepted';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM memory.consolidation_job
    WHERE pipeline_version='20260714_v4'
      AND source_external_id='2efbb10d-f07a-4936-beab-9e7155e433dd'
  ) THEN
    RAISE EXCEPTION 'cross-owner extraction-v4 job was visible';
  END IF;
END
$block$;

ROLLBACK;
