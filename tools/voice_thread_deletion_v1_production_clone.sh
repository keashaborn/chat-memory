#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${VOICE_THREAD_DELETE_CLONE_PORT:-55461}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p voicethreaddeletev1
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260722_voice_thread_deletion_v1.sql
rollback=ops/sql/20260722_voice_thread_deletion_v1_rollback.sql
backup=$(mktemp /tmp/voice-thread-delete-v1.XXXXXX.dump)
roles=$(mktemp /tmp/voice-thread-delete-v1-roles.XXXXXX.sql)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$roles"
}
trap cleanup EXIT
chmod 0600 "$backup" "$roles"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

for required in "$migration" "$rollback" rag_engine/thread_deletion_v1.py; do
  [[ -f "$repo_root/$required" ]]
done

docker exec brains-postgres-1 pg_dump -U sage -d memory -Fc >"$backup"
[[ -s "$backup" ]]

docker exec brains-postgres-1 psql -X -A -t -U sage -d memory \
  -v ON_ERROR_STOP=1 -c "
    SELECT format(
      'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;',
      rolname
    )
    FROM pg_roles
    WHERE rolname NOT LIKE 'pg\\_%' ESCAPE '\\'
      AND rolname NOT IN ('sage','postgres')
    ORDER BY rolname
  " >"$roles"

"${compose[@]}" up -d --wait postgres
run_sql <"$roles"
run_sql -c "
  ALTER ROLE brains_app LOGIN PASSWORD 'clone_only_brains_password';
"
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists <"$backup"

run_sql <<'SQL'
GRANT USAGE ON SCHEMA memory TO brains_app;
GRANT SELECT ON
  memory.evidence,
  memory.claim_evidence,
  memory.project_thread_binding_event
TO brains_app;
GRANT SELECT,UPDATE ON memory.consolidation_job TO brains_app;
GRANT DELETE ON memory.retrieval_trace TO brains_app;
GRANT SELECT,DELETE ON public.threads,public.chat_log TO brains_app;
GRANT DELETE ON public.telemetry_event TO brains_app;
GRANT EXECUTE ON FUNCTION memory.transition_evidence_lifecycle(
  uuid,uuid,text,text,text,text,jsonb
) TO brains_app;
SQL

run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"

[[ "$(scalar "
  SELECT (
    has_table_privilege(
      'brains_app',
      'memory.assistant_transcript_attestation_v1',
      'DELETE'
    )
    AND has_table_privilege(
      'brains_app',
      'memory.final_answer_memory_binding_v1',
      'DELETE'
    )
    AND to_regclass('public.telemetry_event_actor_thread_idx') IS NOT NULL
  )::int
")" == 1 ]]

run_sql <<'SQL'
SELECT set_config(
  'app.user_id',
  '10000000-0000-4000-8000-000000000001',
  false
);

INSERT INTO public.threads(
  id,owner_user_id,user_id,title
) VALUES (
  '20000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  'thread deletion clone fixture'
);

INSERT INTO public.chat_log(
  id,owner_user_id,user_id,user_id_alias,source,text,thread_id
) VALUES
(
  '30000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  'frontend/chat:user',
  'clone-only deletion fixture',
  '20000000-0000-4000-8000-000000000001'
),
(
  '30000000-0000-4000-8000-000000000002',
  '10000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  'backend/response:assistant',
  'clone-only deletion fixture response',
  '20000000-0000-4000-8000-000000000001'
);

INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,
  content,content_sha256,status
) VALUES (
  '40000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  'user_statement',
  'public.chat_log',
  '30000000-0000-4000-8000-000000000001',
  'clone-only deletion fixture',
  encode(public.digest('clone-only deletion fixture','sha256'),'hex'),
  'active'
);

INSERT INTO memory.assistant_transcript_attestation_v1(
  answer_id,owner_user_id,thread_id,chat_log_id,
  request_id_sha256,conversation_snapshot_sha256,trusted_plan_sha256,
  provider_request_sha256,provider_response_sha256,provider_response_id,
  output_kind,assistant_text_sha256,attestation_sha256,created_at
) VALUES (
  '30000000-0000-4000-8000-000000000002',
  '10000000-0000-4000-8000-000000000001',
  '20000000-0000-4000-8000-000000000001',
  '30000000-0000-4000-8000-000000000002',
  repeat('1',64),repeat('2',64),repeat('3',64),repeat('4',64),
  repeat('5',64),'clone-provider-response','content',
  repeat('6',64),repeat('7',64),clock_timestamp()
);

INSERT INTO memory.final_answer_memory_binding_v1(
  answer_id,owner_user_id,thread_id,binding_manifest_sha256,binding,created_at
) VALUES (
  '30000000-0000-4000-8000-000000000002',
  '10000000-0000-4000-8000-000000000001',
  '20000000-0000-4000-8000-000000000001',
  repeat('8',64),'{}'::jsonb,clock_timestamp()
);

INSERT INTO memory.retrieval_trace(
  trace_id,owner_user_id,request_id,answer_id,thread_id,query_hash,
  query_preview,intent,domain,token_budget,selected_count,metadata
) VALUES (
  '60000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  'clone-request',
  '30000000-0000-4000-8000-000000000002',
  '20000000-0000-4000-8000-000000000001',
  repeat('9',64),'clone-only preview','ordinary','ordinary',100,0,'{}'::jsonb
);

INSERT INTO public.telemetry_event(
  event_id,event_type,subject_type,subject_id,thread_id,turn_id,
  payload,occurred_at,actor_user_id
) VALUES (
  '70000000-0000-4000-8000-000000000001',
  'voice.turn.trace','user',
  '10000000-0000-4000-8000-000000000001',
  '20000000-0000-4000-8000-000000000001',
  '80000000-0000-4000-8000-000000000001',
  '{}'::jsonb,clock_timestamp(),
  '10000000-0000-4000-8000-000000000001'
);
SQL

POSTGRES_DSN="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory" \
PYTHONPATH="$repo_root" \
/opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import os
import uuid

import asyncpg

from rag_engine.thread_deletion_v1 import delete_thread_v1
from scripts.memory_v1_consolidation_worker import (
    acquire_source_erasure_lock,
    release_source_erasure_lock,
)


class VerifiedEmptyQdrant:
    def delete(self, **kwargs):
        return None

    def scroll(self, **kwargs):
        return [], None


async def main():
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    worker_conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    worker_lock_held = False
    try:
        owner = uuid.UUID("10000000-0000-4000-8000-000000000001")
        thread = uuid.UUID("20000000-0000-4000-8000-000000000001")
        source = "30000000-0000-4000-8000-000000000001"
        await acquire_source_erasure_lock(
            worker_conn,
            owner=owner,
            source_system="public.chat_log",
            source_external_id=source,
        )
        worker_lock_held = True
        deletion = asyncio.create_task(
            delete_thread_v1(
                conn,
                VerifiedEmptyQdrant(),
                owner_user_id=owner,
                thread_id=thread,
            )
        )
        await asyncio.sleep(0.1)
        assert deletion.done() is False
        await release_source_erasure_lock(
            worker_conn,
            owner=owner,
            source_system="public.chat_log",
            source_external_id=source,
        )
        worker_lock_held = False
        result = await deletion
        assert result.status == "completed"
        assert result.qdrant_verified is True
        assert result.counts.governed_evidence_tombstoned == 1
        assert result.counts.retrieval_traces_deleted == 1
        assert result.counts.answer_bindings_deleted == 1
        assert result.counts.attestations_deleted == 1
        assert result.counts.telemetry_events_deleted == 1
        assert result.counts.transcript_rows_deleted == 2
        assert result.counts.threads_deleted == 1

        async with conn.transaction(readonly=True):
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)",
                "10000000-0000-4000-8000-000000000001",
            )
            remaining = await conn.fetchrow(
                """
                SELECT
                  (SELECT count(*) FROM public.threads
                   WHERE id='20000000-0000-4000-8000-000000000001') AS threads,
                  (SELECT count(*) FROM public.chat_log
                   WHERE thread_id='20000000-0000-4000-8000-000000000001') AS chat,
                  (SELECT count(*) FROM memory.assistant_transcript_attestation_v1
                   WHERE thread_id='20000000-0000-4000-8000-000000000001') AS attestations,
                  (SELECT count(*) FROM memory.final_answer_memory_binding_v1
                   WHERE thread_id='20000000-0000-4000-8000-000000000001') AS bindings,
                  (SELECT count(*) FROM memory.retrieval_trace
                   WHERE thread_id='20000000-0000-4000-8000-000000000001') AS traces,
                  (SELECT count(*) FROM public.telemetry_event
                   WHERE thread_id='20000000-0000-4000-8000-000000000001') AS telemetry,
                  (SELECT count(*) FROM memory.evidence
                   WHERE evidence_id='40000000-0000-4000-8000-000000000001'
                     AND status='deleted' AND content IS NULL) AS tombstones,
                  (SELECT count(*) FROM memory.evidence_lifecycle_event
                   WHERE evidence_id='40000000-0000-4000-8000-000000000001'
                     AND action='delete_tombstone') AS lifecycle_events
                """
            )
        assert dict(remaining) == {
            "threads": 0,
            "chat": 0,
            "attestations": 0,
            "bindings": 0,
            "traces": 0,
            "telemetry": 0,
            "tombstones": 1,
            "lifecycle_events": 1,
        }
    finally:
        if worker_lock_held:
            await release_source_erasure_lock(
                worker_conn,
                owner=uuid.UUID(
                    "10000000-0000-4000-8000-000000000001"
                ),
                source_system="public.chat_log",
                source_external_id=(
                    "30000000-0000-4000-8000-000000000001"
                ),
            )
        await worker_conn.close()
        await conn.close()


asyncio.run(main())
PY

run_sql <<'SQL'
SELECT set_config(
  'app.user_id',
  '10000000-0000-4000-8000-000000000001',
  false
);

INSERT INTO public.threads(
  id,owner_user_id,user_id,title
) VALUES (
  '20000000-0000-4000-8000-000000000002',
  '10000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  'orphan repair clone fixture'
);

INSERT INTO public.chat_log(
  id,owner_user_id,user_id,user_id_alias,source,text,thread_id
) VALUES (
  '30000000-0000-4000-8000-000000000003',
  '10000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  'frontend/chat:user',
  'clone-only orphan repair fixture',
  '20000000-0000-4000-8000-000000000002'
),(
  '30000000-0000-4000-8000-000000000004',
  '10000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  'backend/response:assistant',
  'clone-only orphan repair response',
  '20000000-0000-4000-8000-000000000002'
);

INSERT INTO memory.evidence(
  evidence_id,owner_user_id,kind,source_system,external_id,
  content,content_sha256,status,metadata
) VALUES (
  '40000000-0000-4000-8000-000000000002',
  '10000000-0000-4000-8000-000000000001',
  'user_statement',
  'public.chat_log',
  '30000000-0000-4000-8000-000000000003',
  'clone-only orphan repair fixture',
  encode(public.digest('clone-only orphan repair fixture','sha256'),'hex'),
  'active',
  '{"thread_id":"20000000-0000-4000-8000-000000000002"}'::jsonb
);

INSERT INTO memory.assistant_transcript_attestation_v1(
  answer_id,owner_user_id,thread_id,chat_log_id,
  request_id_sha256,conversation_snapshot_sha256,trusted_plan_sha256,
  provider_request_sha256,provider_response_sha256,provider_response_id,
  output_kind,assistant_text_sha256,attestation_sha256,created_at
) VALUES (
  '30000000-0000-4000-8000-000000000004',
  '10000000-0000-4000-8000-000000000001',
  '20000000-0000-4000-8000-000000000002',
  '30000000-0000-4000-8000-000000000004',
  repeat('a',64),repeat('b',64),repeat('c',64),repeat('d',64),
  repeat('e',64),'clone-orphan-provider-response','content',
  repeat('f',64),repeat('1',64),clock_timestamp()
);

INSERT INTO memory.final_answer_memory_binding_v1(
  answer_id,owner_user_id,thread_id,binding_manifest_sha256,binding,created_at
) VALUES (
  '30000000-0000-4000-8000-000000000004',
  '10000000-0000-4000-8000-000000000001',
  '20000000-0000-4000-8000-000000000002',
  repeat('2',64),'{}'::jsonb,clock_timestamp()
);

INSERT INTO memory.retrieval_trace(
  trace_id,owner_user_id,request_id,answer_id,thread_id,query_hash,
  query_preview,intent,domain,token_budget,selected_count,metadata
) VALUES (
  '60000000-0000-4000-8000-000000000002',
  '10000000-0000-4000-8000-000000000001',
  'clone-orphan-request',
  '30000000-0000-4000-8000-000000000004',
  '20000000-0000-4000-8000-000000000002',
  repeat('3',64),'clone-only orphan preview',
  'ordinary','ordinary',100,0,'{}'::jsonb
);

INSERT INTO public.telemetry_event(
  event_id,event_type,subject_type,subject_id,thread_id,turn_id,
  payload,occurred_at,actor_user_id
) VALUES (
  '70000000-0000-4000-8000-000000000002',
  'voice.turn.trace','user',
  '10000000-0000-4000-8000-000000000001',
  '20000000-0000-4000-8000-000000000002',
  '80000000-0000-4000-8000-000000000002',
  '{}'::jsonb,clock_timestamp(),
  '10000000-0000-4000-8000-000000000001'
);

DELETE FROM public.chat_log
WHERE thread_id='20000000-0000-4000-8000-000000000002';
DELETE FROM public.threads
WHERE id='20000000-0000-4000-8000-000000000002';
SQL

POSTGRES_DSN="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory" \
PYTHONPATH="$repo_root" \
/opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import os
import uuid

import asyncpg

from rag_engine.thread_orphan_repair_v1 import repair_orphan_thread_v1


class VerifiedEmptyQdrant:
    def delete(self, **kwargs):
        return None

    def scroll(self, **kwargs):
        return [], None


async def main():
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        owner = uuid.UUID("10000000-0000-4000-8000-000000000001")
        thread = uuid.UUID("20000000-0000-4000-8000-000000000002")
        result = await repair_orphan_thread_v1(
            conn,
            VerifiedEmptyQdrant(),
            owner_user_id=owner,
            thread_id=thread,
        )
        assert result.governed_evidence_tombstoned == 1
        assert result.retrieval_traces_deleted == 1
        assert result.answer_bindings_deleted == 1
        assert result.attestations_deleted == 1
        assert result.telemetry_events_deleted == 1

        async with conn.transaction(readonly=True):
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)",
                str(owner),
            )
            remaining = await conn.fetchrow(
                """
                SELECT
                  (SELECT count(*)
                   FROM memory.assistant_transcript_attestation_v1
                   WHERE thread_id=$1) AS attestations,
                  (SELECT count(*)
                   FROM memory.final_answer_memory_binding_v1
                   WHERE thread_id=$1) AS bindings,
                  (SELECT count(*)
                   FROM memory.retrieval_trace
                   WHERE thread_id=$1) AS traces,
                  (SELECT count(*)
                   FROM public.telemetry_event
                   WHERE thread_id=$2) AS telemetry,
                  (SELECT count(*)
                   FROM memory.evidence
                   WHERE evidence_id=
                     '40000000-0000-4000-8000-000000000002'
                     AND status='deleted' AND content IS NULL) AS tombstones
                """,
                thread,
                str(thread),
            )
        assert dict(remaining) == {
            "attestations": 0,
            "bindings": 0,
            "traces": 0,
            "telemetry": 0,
            "tombstones": 1,
        }
    finally:
        await conn.close()


asyncio.run(main())
PY

run_sql <"$repo_root/$rollback"
[[ "$(scalar "
  SELECT (
    NOT has_table_privilege(
      'brains_app',
      'memory.assistant_transcript_attestation_v1',
      'DELETE'
    )
    AND NOT has_table_privilege(
      'brains_app',
      'memory.final_answer_memory_binding_v1',
      'DELETE'
    )
    AND to_regclass('public.telemetry_event_actor_thread_idx') IS NULL
  )::int
")" == 1 ]]

echo 'voice_thread_deletion_v1_production_clone: PASS'
