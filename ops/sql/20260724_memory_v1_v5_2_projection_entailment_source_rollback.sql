BEGIN;

REVOKE ALL ON FUNCTION
  memory.preflight_projection_entailment_source_v5_2(uuid)
  FROM PUBLIC,brains_app;
DROP FUNCTION IF EXISTS
  memory.preflight_projection_entailment_source_v5_2(uuid);

COMMIT;
