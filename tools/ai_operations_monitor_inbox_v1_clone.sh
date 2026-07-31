#!/usr/bin/env bash
set -euo pipefail

live_container="brains-postgres-1"
candidate="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
clone_name="ai-operations-monitor-inbox-v1-${$}"
clone_password="clone-only-ai-operations-v1"
clone_image="$(docker inspect -f '{{.Config.Image}}' "$live_container")"
clone_tmp="$(mktemp -d /tmp/ai-operations-monitor-inbox-v1.XXXXXX)"
schema_dump="$clone_tmp/trusted-web-schema.sql"

cleanup() {
  docker rm -f "$clone_name" >/dev/null 2>&1 || true
  rm -rf "$clone_tmp"
}
trap cleanup EXIT

production_before="$(
  docker exec "$live_container" psql -X -U sage -d memory -At -F '|' -c \
    "select count(*),coalesce(max(created_at)::text,'none')
     from trusted_web.retrieval_audit"
)"

docker exec "$live_container" pg_dump \
  -U sage \
  -d memory \
  --schema-only \
  --no-owner \
  --no-privileges \
  --schema=trusted_web \
  > "$schema_dump"

docker run -d \
  --name "$clone_name" \
  -e POSTGRES_USER=sage \
  -e POSTGRES_PASSWORD="$clone_password" \
  -e POSTGRES_DB=memory \
  -p 127.0.0.1::5432 \
  "$clone_image" >/dev/null

for _attempt in $(seq 1 30); do
  if docker exec "$clone_name" pg_isready -U sage -d memory \
      >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$clone_name" pg_isready -U sage -d memory >/dev/null

docker exec "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  -c "create extension if not exists pgcrypto with schema public;
      create role brains_app login nosuperuser nocreatedb nocreaterole
        noinherit nobypassrls password '$clone_password';
      create role anon nologin nosuperuser nocreatedb nocreaterole
        noinherit nobypassrls;
      create role authenticated nologin nosuperuser nocreatedb nocreaterole
        noinherit nobypassrls;
      create role service_role nologin nosuperuser nocreatedb nocreaterole
        noinherit nobypassrls;
      create table public.chat_log (id uuid primary key)"

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory < "$schema_dump"

docker exec "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  -c "grant usage on schema trusted_web to brains_app;
      grant select on trusted_web.retrieval_audit to brains_app;
      grant select on trusted_web.retrieval_monitor_hourly_v1 to brains_app"

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260731_ai_operations_monitor_inbox_v1.sql"

clone_port="$(docker port "$clone_name" 5432/tcp | sed -n '1s/.*://p')"
clone_dsn="postgresql://brains_app:${clone_password}@127.0.0.1:${clone_port}/memory"

POSTGRES_DSN="$clone_dsn" \
PYTHONPATH="$candidate" \
/opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import json
import os
from datetime import datetime, timezone
from uuid import UUID

import asyncpg


ACTOR = UUID("90000000-0000-4000-8000-000000000001")


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


async def record(connection, status, severity, reasons, *, drill=False):
    return decode(
        await connection.fetchval(
            """
            select ai_operations.record_monitor_observation_v1(
              $1,$2,$3,$4,$5::text[],$6,$7,$8,$9,$10,$11,$12,$13
            )
            """,
            "trusted_web_retrieval",
            status,
            severity,
            drill,
            reasons,
            24,
            4,
            2,
            2,
            1,
            1,
            0.5,
            datetime.now(timezone.utc),
        )
    )


async def main():
    connection = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        try:
            await connection.fetchval(
                "select count(*) from ai_operations.monitor_incident_v1"
            )
        except asyncpg.InsufficientPrivilegeError:
            pass
        else:
            raise AssertionError("brains_app received direct table access")

        opened = await record(
            connection,
            "violated",
            "critical",
            ["dependency_failure_count", "fail_closed_rate"],
        )
        assert opened["action"] == "opened", opened
        repeated = await record(
            connection,
            "violated",
            "critical",
            ["fail_closed_rate", "dependency_failure_count"],
        )
        assert repeated["action"] == "updated", repeated
        assert repeated["observation_count"] == 2, repeated

        try:
            await connection.fetchval(
                "select ai_operations.list_monitor_incidents_v1(null,50)"
            )
        except asyncpg.InsufficientPrivilegeError:
            pass
        else:
            raise AssertionError("inspection succeeded without actor context")

        async with connection.transaction():
            await connection.execute(
                "select set_config('app.user_id',$1,true)", str(ACTOR)
            )
            await connection.execute(
                "select set_config('app.ai_operations_capability',$1,true)",
                "inspector.view",
            )
            inbox = decode(
                await connection.fetchval(
                    "select ai_operations.list_monitor_incidents_v1('open',50)"
                )
            )
        assert len(inbox["items"]) == 1, inbox
        incident_id = UUID(inbox["items"][0]["incident_id"])
        assert inbox["items"][0]["observation_count"] == 2, inbox

        async with connection.transaction():
            await connection.execute(
                "select set_config('app.user_id',$1,true)", str(ACTOR)
            )
            await connection.execute(
                "select set_config('app.ai_operations_capability',$1,true)",
                "incident.manage",
            )
            acknowledged = decode(
                await connection.fetchval(
                    "select ai_operations.acknowledge_monitor_incident_v1($1,$2)",
                    incident_id,
                    ACTOR,
                )
            )
        assert acknowledged["state"] == "acknowledged", acknowledged

        async with connection.transaction():
            await connection.execute(
                "select set_config('app.user_id',$1,true)", str(ACTOR)
            )
            await connection.execute(
                "select set_config('app.ai_operations_capability',$1,true)",
                "incident.manage",
            )
            resolved = decode(
                await connection.fetchval(
                    "select ai_operations.resolve_monitor_incident_v1($1,$2)",
                    incident_id,
                    ACTOR,
                )
            )
        assert resolved["state"] == "resolved", resolved

        unavailable = await record(
            connection,
            "unavailable",
            "critical",
            ["monitor_query_failed"],
        )
        assert unavailable["action"] == "opened", unavailable
        recovery = await record(connection, "pass", "info", [])
        assert recovery["action"] == "resolved", recovery
        assert recovery["resolved_count"] == 1, recovery
        print(
            "AI_OPERATIONS_APP_ROLE_ASSERTIONS "
            "direct_table=denied record=pass inspect=pass "
            "acknowledge=pass resolve=pass recovery=pass"
        )
    finally:
        await connection.close()


asyncio.run(main())
PY

drill_output="$(
  TRUSTED_WEB_MONITOR_ENABLED=authorized \
  TRUSTED_WEB_MONITOR_DRILL_ENABLED=authorized \
  POSTGRES_DSN="$clone_dsn" \
  PYTHONPATH="$candidate" \
  /opt/chat-memory/venv/bin/python \
    "$candidate/scripts/trusted_web_monitor_job_v1.py" --drill
)"
DRILL_OUTPUT="$drill_output" \
/opt/chat-memory/venv/bin/python - <<'PY'
import json
import os

payload = json.loads(os.environ["DRILL_OUTPUT"])
assert payload["status"] == "drill", payload
assert payload["alert_store"] == "recorded", payload
assert payload["alert_delivery"] == "unconfigured", payload
assert payload["request_count"] == 0, payload
print("AI_OPERATIONS_SAFE_DRILL inbox=pass search_rows=untouched")
PY

docker exec "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  -c "select 1 / case when (
        select relrowsecurity and relforcerowsecurity
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where n.nspname='ai_operations'
          and c.relname='monitor_incident_v1'
      ) then 1 else 0 end;
      select 1 / case when (
        select relrowsecurity and relforcerowsecurity
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where n.nspname='ai_operations'
          and c.relname='monitor_incident_event_v1'
      ) then 1 else 0 end;
      select 1 / case when not has_table_privilege(
        'brains_app','ai_operations.monitor_incident_v1','select'
      ) then 1 else 0 end;
      select 1 / case when not has_schema_privilege(
        'anon','ai_operations','usage'
      ) and not has_schema_privilege(
        'authenticated','ai_operations','usage'
      ) and not has_schema_privilege(
        'service_role','ai_operations','usage'
      ) then 1 else 0 end;
      select 1 / case when (
        select not rolcanlogin and not rolsuper and not rolcreatedb
          and not rolcreaterole and not rolinherit and not rolbypassrls
        from pg_roles where rolname='ai_operations_store_v1'
      ) then 1 else 0 end;
      select 1 / case when (
        select count(*)=3
        from ai_operations.monitor_incident_v1
        where state='resolved'
      ) then 1 else 0 end;
      select 1 / case when (
        select count(*)=7
        from ai_operations.monitor_incident_event_v1
      ) then 1 else 0 end"

if docker exec "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  -c "set session authorization ai_operations_store_v1;
      update ai_operations.monitor_incident_event_v1
      set observed_at=clock_timestamp()" >/dev/null 2>&1
then
  printf 'append-only event mutation unexpectedly succeeded\n' >&2
  exit 1
fi

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260731_ai_operations_monitor_inbox_v1_rollback.sql"
docker exec "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  -c "select 1 / case when to_regnamespace('ai_operations') is null
      and to_regrole('ai_operations_store_v1') is null
      then 1 else 0 end"
docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260731_ai_operations_monitor_inbox_v1.sql"

production_after="$(
  docker exec "$live_container" psql -X -U sage -d memory -At -F '|' -c \
    "select count(*),coalesce(max(created_at)::text,'none')
     from trusted_web.retrieval_audit"
)"
if test "$production_before" != "$production_after"; then
  printf 'production trusted-web fingerprint changed during clone gate\n' >&2
  exit 1
fi

printf 'AI_OPERATIONS_CLONE_GATE apply=pass acl=pass rls=pass append_only=pass rollback=pass reapply=pass production_unchanged=pass\n'
