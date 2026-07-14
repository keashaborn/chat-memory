BEGIN;

DROP TRIGGER IF EXISTS project_review_enqueue_governance
  ON memory.project_knowledge_candidate_review;
DROP TRIGGER IF EXISTS preference_review_enqueue_governance
  ON memory.preference_candidate_review;
DROP TRIGGER IF EXISTS candidate_enqueue_governance ON memory.candidate;

DROP FUNCTION IF EXISTS memory.enqueue_claim_salience_governance(uuid,text,text);
DROP FUNCTION IF EXISTS memory.enqueue_salience_governance(date,text);
DROP FUNCTION IF EXISTS memory.enqueue_project_review_governance();
DROP FUNCTION IF EXISTS memory.enqueue_preference_review_governance();
DROP FUNCTION IF EXISTS memory.enqueue_claim_candidate_governance();
DROP FUNCTION IF EXISTS memory.queue_governance_job(uuid,text,text,uuid,text,text,jsonb);

DROP TRIGGER IF EXISTS governance_review_append_only_guard
  ON memory.governance_review_event;
DROP TRIGGER IF EXISTS governance_event_append_only_guard
  ON memory.governance_event;
DROP TRIGGER IF EXISTS governance_job_update_guard
  ON memory.governance_job;

DROP FUNCTION IF EXISTS memory.guard_governance_append_only();
DROP FUNCTION IF EXISTS memory.guard_governance_job_update();

DROP TABLE IF EXISTS memory.governance_review_event;
DROP TABLE IF EXISTS memory.governance_event;
DROP TABLE IF EXISTS memory.governance_job;
DROP TYPE IF EXISTS memory.governance_job_status;

COMMIT;
