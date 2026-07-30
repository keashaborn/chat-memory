BEGIN;

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

DROP FUNCTION IF EXISTS
memory.enqueue_owner_v5_2_semantic_compiler_v9_reextract_v1(
  uuid,uuid,uuid,uuid,text,uuid,text,text,text
);

COMMIT;
