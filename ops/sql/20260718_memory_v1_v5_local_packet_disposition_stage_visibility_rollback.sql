BEGIN;
SET LOCAL lock_timeout='5s';
ALTER POLICY owner_isolation ON memory.relational_stage_batch
  TO memory_v5_writer;
COMMIT;
