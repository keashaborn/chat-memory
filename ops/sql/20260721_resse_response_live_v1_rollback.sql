BEGIN;
DROP TABLE IF EXISTS memory.final_answer_memory_binding_v1;
DROP TABLE IF EXISTS memory.assistant_transcript_attestation_v1;
DROP FUNCTION IF EXISTS memory.read_governed_claims_v1(uuid[]);
COMMIT;
