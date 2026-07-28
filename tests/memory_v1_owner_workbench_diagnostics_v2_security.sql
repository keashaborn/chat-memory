\set ON_ERROR_STOP on
BEGIN;

DO $preflight$
BEGIN
  IF session_user<>'brains_app' THEN
    RAISE EXCEPTION 'workbench diagnostics security test requires brains_app';
  END IF;
  IF to_regprocedure(
       'memory.list_owner_memory_workbench_v2(text,integer,timestamptz,uuid)'
     ) IS NULL
     OR to_regprocedure(
       'memory.record_owner_memory_workbench_feedback_v2(uuid,uuid,text,text,text,text)'
     ) IS NULL THEN
    RAISE EXCEPTION 'workbench diagnostics v2 functions are absent';
  END IF;
  IF has_table_privilege(
       'brains_app','memory.owner_packet_feedback_v1','SELECT'
     )
     OR has_table_privilege(
       'brains_app','memory.owner_packet_feedback_v1','INSERT'
     )
     OR has_table_privilege(
       'brains_app','memory.owner_packet_feedback_v1','UPDATE'
     )
     OR has_table_privilege(
       'brains_app','memory.owner_packet_feedback_v1','DELETE'
     ) THEN
    RAISE EXCEPTION 'brains_app has direct feedback table privileges';
  END IF;
END
$preflight$;

SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

CREATE TEMP TABLE workbench_v2_fixture AS
SELECT packet_id,packet_storage_sha256,source_content,source_context_content
FROM memory.list_owner_memory_workbench_v2('all',25,NULL,NULL)
ORDER BY packet_id
LIMIT 1;

DO $fixture$
BEGIN
  IF (SELECT count(*) FROM workbench_v2_fixture)<>1 THEN
    RAISE EXCEPTION 'workbench diagnostics fixture is unavailable';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM memory.list_owner_memory_workbench_v2('all',25,NULL,NULL)
    WHERE source_context_content IS NOT NULL
      AND length(source_context_content)>=length(source_content)
  ) THEN
    RAISE EXCEPTION 'owner-scoped source context is unavailable';
  END IF;
END
$fixture$;

DO $category_required$
BEGIN
  BEGIN
    PERFORM *
    FROM memory.record_owner_memory_workbench_feedback_v2(
      '00000000-0000-4000-8000-000000000200',
      (SELECT packet_id FROM workbench_v2_fixture),
      (SELECT packet_storage_sha256 FROM workbench_v2_fixture),
      'not_correct',
      NULL,
      'missing category must fail'
    );
    RAISE EXCEPTION 'not-correct feedback without a category succeeded';
  EXCEPTION
    WHEN SQLSTATE '22023' THEN NULL;
  END;
END
$category_required$;

CREATE TEMP TABLE workbench_v2_apply AS
SELECT *
FROM memory.record_owner_memory_workbench_feedback_v2(
  '00000000-0000-4000-8000-000000000201',
  (SELECT packet_id FROM workbench_v2_fixture),
  (SELECT packet_storage_sha256 FROM workbench_v2_fixture),
  'not_correct',
  'context_missing',
  'bounded diagnostic'
);

DO $applied$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM workbench_v2_apply
    WHERE decision='not_correct'
      AND diagnostic_category='context_missing'
      AND apply_outcome='applied'
  ) THEN
    RAISE EXCEPTION 'categorized workbench feedback was not applied';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM memory.record_owner_memory_workbench_feedback_v2(
      '00000000-0000-4000-8000-000000000201',
      (SELECT packet_id FROM workbench_v2_fixture),
      (SELECT packet_storage_sha256 FROM workbench_v2_fixture),
      'not_correct',
      'context_missing',
      'bounded diagnostic'
    )
    WHERE apply_outcome='replayed'
  ) THEN
    RAISE EXCEPTION 'categorized feedback replay was not zero-write';
  END IF;
END
$applied$;

SELECT set_config(
  'app.user_id',
  '673d64a3-c4ba-4d1c-89e3-e0c579022fad',
  true
);

DO $cross_owner$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.list_owner_memory_workbench_v2('all',25,NULL,NULL)
    WHERE packet_id=(SELECT packet_id FROM workbench_v2_fixture)
  ) THEN
    RAISE EXCEPTION 'cross-owner workbench context read succeeded';
  END IF;
  BEGIN
    PERFORM *
    FROM memory.record_owner_memory_workbench_feedback_v2(
      '00000000-0000-4000-8000-000000000202',
      (SELECT packet_id FROM workbench_v2_fixture),
      (SELECT packet_storage_sha256 FROM workbench_v2_fixture),
      'not_correct',
      'other',
      'cross-owner write must fail'
    );
    RAISE EXCEPTION 'cross-owner workbench feedback succeeded';
  EXCEPTION
    WHEN SQLSTATE '22023' THEN NULL;
  END;
END
$cross_owner$;

ROLLBACK;
