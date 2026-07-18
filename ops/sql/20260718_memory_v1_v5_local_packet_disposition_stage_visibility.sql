BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DO $migration$
DECLARE
  policy_roles oid[];
  writer_oid oid;
  disposition_oid oid;
BEGIN
  IF session_user<>'sage' THEN
    RAISE EXCEPTION 'local disposition stage visibility migration requires sage';
  END IF;
  IF to_regclass('memory.relational_stage_batch') IS NULL
     OR to_regrole('memory_v5_writer') IS NULL
     OR to_regrole('memory_v5_local_disposition_maintainer') IS NULL
     OR to_regprocedure(
       'memory.plan_owner_v5_local_packet_disposition_v1(integer)'
     ) IS NULL THEN
    RAISE EXCEPTION 'local disposition stage visibility prerequisites are absent';
  END IF;
  SELECT oid INTO writer_oid FROM pg_roles WHERE rolname='memory_v5_writer';
  SELECT oid INTO disposition_oid FROM pg_roles
  WHERE rolname='memory_v5_local_disposition_maintainer';
  SELECT polroles INTO policy_roles
  FROM pg_policy
  WHERE polrelid='memory.relational_stage_batch'::regclass
    AND polname='owner_isolation';
  IF policy_roles IS NULL
     OR cardinality(policy_roles) NOT IN (1,2)
     OR NOT writer_oid=ANY(policy_roles)
     OR (cardinality(policy_roles)=2 AND NOT disposition_oid=ANY(policy_roles)) THEN
    RAISE EXCEPTION 'relational stage owner policy has unexpected roles';
  END IF;
END
$migration$;

ALTER POLICY owner_isolation ON memory.relational_stage_batch
  TO memory_v5_writer,memory_v5_local_disposition_maintainer;

DO $postflight$
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
    RAISE EXCEPTION 'local disposition stage visibility was not installed';
  END IF;
END
$postflight$;

COMMIT;
