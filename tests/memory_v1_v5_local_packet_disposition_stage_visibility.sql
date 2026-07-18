\set ON_ERROR_STOP on

DO $catalog$
DECLARE
  policy_roles oid[];
  writer_oid oid;
  disposition_oid oid;
BEGIN
  SELECT oid INTO writer_oid FROM pg_roles WHERE rolname='memory_v5_writer';
  SELECT oid INTO disposition_oid FROM pg_roles
  WHERE rolname='memory_v5_local_disposition_maintainer';
  SELECT polroles INTO policy_roles
  FROM pg_policy
  WHERE polrelid='memory.relational_stage_batch'::regclass
    AND polname='owner_isolation';
  IF cardinality(policy_roles)<>2
     OR NOT writer_oid=ANY(policy_roles)
     OR NOT disposition_oid=ANY(policy_roles) THEN
    RAISE EXCEPTION 'stage owner policy does not include exact restricted roles';
  END IF;
  IF NOT has_table_privilege(
       'memory_v5_local_disposition_maintainer',
       'memory.relational_stage_batch','SELECT'
     )
     OR has_table_privilege(
       'memory_v5_local_disposition_maintainer',
       'memory.relational_stage_batch','INSERT'
     )
     OR has_table_privilege(
       'memory_v5_local_disposition_maintainer',
       'memory.relational_stage_batch','UPDATE'
     )
     OR has_table_privilege(
       'memory_v5_local_disposition_maintainer',
       'memory.relational_stage_batch','DELETE'
     ) THEN
    RAISE EXCEPTION 'local disposition stage ACL is not read-only';
  END IF;
END
$catalog$;

BEGIN;
SET LOCAL statement_timeout='30s';
SELECT set_config('test.staged_packet_id',:'staged_packet_id',true);
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id',:'owner_user_id',true);
DO $owner$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
    WHERE packet_id=current_setting('test.staged_packet_id')::uuid
  ) THEN
    RAISE EXCEPTION 'already-staged packet remained in owner disposition plan';
  END IF;
END
$owner$;
SELECT set_config('app.user_id',:'other_owner_user_id',true);
DO $other$
BEGIN
  IF EXISTS (
    SELECT 1 FROM memory.plan_owner_v5_local_packet_disposition_v1(20)
    WHERE packet_id=current_setting('test.staged_packet_id')::uuid
  ) THEN
    RAISE EXCEPTION 'already-staged packet crossed owner boundary';
  END IF;
END
$other$;
RESET SESSION AUTHORIZATION;
ROLLBACK;

SELECT 'memory_v1_v5_local_packet_disposition_stage_visibility: PASS' AS result;
