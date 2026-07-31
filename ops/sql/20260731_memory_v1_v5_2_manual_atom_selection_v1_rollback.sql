BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';

DROP FUNCTION IF EXISTS memory.plan_owner_v5_2_atom_admission_v2(uuid);
ALTER FUNCTION
  memory.plan_owner_v5_2_atom_admission_v2_before_manual_selection_v1(uuid)
  RENAME TO plan_owner_v5_2_atom_admission_v2;
DROP FUNCTION IF EXISTS memory.register_owner_v5_2_manual_atom_selection_v1(
  uuid,uuid,uuid,text,text,text,jsonb,jsonb,text
);
DROP FUNCTION IF EXISTS memory.v5_2_manual_atom_selection_manifest_sha_v1(
  uuid,uuid,uuid,uuid,text,text,text,jsonb,jsonb
);
DROP TABLE IF EXISTS memory.v5_2_manual_atom_selection_v1;

ALTER FUNCTION memory.plan_owner_v5_2_atom_admission_v2(uuid)
  OWNER TO memory_v5_2_atom_admission_maintainer;
REVOKE ALL ON FUNCTION memory.plan_owner_v5_2_atom_admission_v2(uuid)
  FROM PUBLIC,memory_v5_writer,memory_v5_2_local_router_maintainer;
GRANT EXECUTE ON FUNCTION memory.plan_owner_v5_2_atom_admission_v2(uuid)
  TO brains_app;

COMMIT;
