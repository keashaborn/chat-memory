#!/usr/bin/env bash
set -euo pipefail

live_container="brains-postgres-1"
candidate="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
clone_name="ai-operations-alert-delivery-v1-${$}"
clone_password="clone-only-alert-delivery-v1"
clone_image="$(docker inspect -f '{{.Config.Image}}' "$live_container")"
clone_tmp="$(mktemp -d /tmp/ai-operations-alert-delivery-v1.XXXXXX)"
schema_dump="$clone_tmp/trusted-web-schema.sql"

cleanup() {
  docker rm -f "$clone_name" >/dev/null 2>&1 || true
  rm -rf "$clone_tmp"
}
trap cleanup EXIT

production_before="$(
  docker exec "$live_container" psql -X -U sage -d memory -At -F '|' -c \
    "select count(*),coalesce(max(created_at)::text,'none'),
            coalesce(to_regclass('ai_operations.monitor_alert_delivery_v1')::text,'none')
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
docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260731_ai_operations_alert_delivery_v1.sql"

clone_port="$(docker port "$clone_name" 5432/tcp | sed -n '1s/.*://p')"
clone_dsn="postgresql://brains_app:${clone_password}@127.0.0.1:${clone_port}/memory"

POSTGRES_DSN="$clone_dsn" \
PYTHONPATH="$candidate" \
/opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import json
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg


WORKER = UUID("91000000-0000-4000-8000-000000000001")


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


async def record(connection, monitor, status, severity, reasons, *, drill=False):
    return decode(
        await connection.fetchval(
            """
            select ai_operations.record_monitor_observation_v1(
              $1,$2,$3,$4,$5::text[],$6,$7,$8,$9,$10,$11,$12,$13
            )
            """,
            monitor,
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


async def expect_denied(awaitable, label):
    try:
        await awaitable
    except asyncpg.InsufficientPrivilegeError:
        return
    raise AssertionError(f"{label} unexpectedly succeeded")


async def main():
    connection = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        await expect_denied(
            connection.fetchval(
                "select count(*) from ai_operations.monitor_alert_delivery_v1"
            ),
            "direct outbox read",
        )
        await expect_denied(
            connection.fetchval(
                "select ai_operations.claim_monitor_alert_delivery_v1($1)",
                WORKER,
            ),
            "unauthorized claim",
        )
        await expect_denied(
            connection.fetchval(
                """
                select ai_operations.complete_monitor_alert_delivery_v1(
                  $1,$2,'permanent_failed',null,'authorization_test'
                )
                """,
                uuid4(),
                WORKER,
            ),
            "unauthorized completion",
        )

        opened = await record(
            connection,
            "trusted_web_retrieval",
            "violated",
            "critical",
            ["dependency_failure_count", "fail_closed_rate"],
        )
        assert opened["action"] == "opened", opened
        repeated = await record(
            connection,
            "trusted_web_retrieval",
            "violated",
            "critical",
            ["fail_closed_rate", "dependency_failure_count"],
        )
        assert repeated["action"] == "updated", repeated
        assert repeated["observation_count"] == 2, repeated
        warning = await record(
            connection,
            "trusted_web_warning",
            "violated",
            "warning",
            ["fail_closed_rate"],
        )
        assert warning["action"] == "opened", warning
        drill = await record(
            connection,
            "trusted_web_retrieval",
            "drill",
            "test",
            ["synthetic_failure_drill"],
            drill=True,
        )
        assert drill["action"] == "drill_recorded", drill

        async with connection.transaction():
            await connection.execute(
                "select set_config('app.ai_operations_alert_delivery',$1,true)",
                "authorized",
            )
            await connection.execute(
                "select set_config('app.ai_operations_alert_worker_id',$1,true)",
                str(WORKER),
            )
            claim = decode(
                await connection.fetchval(
                    "select ai_operations.claim_monitor_alert_delivery_v1($1)",
                    WORKER,
                )
            )
            assert claim["status"] == "claimed", claim
            assert claim["attempt_count"] == 1, claim
            assert claim["monitor_name"] == "trusted_web_retrieval", claim
            completion = decode(
                await connection.fetchval(
                    """
                    select ai_operations.complete_monitor_alert_delivery_v1(
                      $1,$2,'retryable_failed',null,'provider_timeout'
                    )
                    """,
                    UUID(claim["delivery_id"]),
                    WORKER,
                )
            )
            assert completion["outcome"] == "retry_scheduled", completion
            empty = decode(
                await connection.fetchval(
                    "select ai_operations.claim_monitor_alert_delivery_v1($1)",
                    WORKER,
                )
            )
            assert empty["status"] == "empty", empty
        print(
            "AI_OPERATIONS_ALERT_APP_PHASE1 "
            "direct_table=denied unauthorized=denied enqueue=pass "
            "dedupe=pass warning_excluded=pass drill_excluded=pass retry=pass"
        )
    finally:
        await connection.close()


asyncio.run(main())
PY

docker exec "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  -c "select 1 / case when (
        select count(*)=1
        from ai_operations.monitor_alert_delivery_v1
        where state='retryable_failed'
          and attempt_count=1
          and last_error_code='provider_timeout'
          and next_attempt_at > clock_timestamp()
      ) then 1 else 0 end;
      select 1 / case when (
        select count(*)=3
        from ai_operations.monitor_alert_delivery_event_v1
      ) then 1 else 0 end;
      select 1 / case when not exists (
        select 1
        from ai_operations.monitor_alert_delivery_v1 delivery
        join ai_operations.monitor_incident_v1 incident
          on incident.incident_id=delivery.incident_id
        where incident.is_drill
      ) then 1 else 0 end;
      select 1 / case when not exists (
        select 1
        from ai_operations.monitor_alert_delivery_v1 delivery
        join ai_operations.monitor_incident_v1 incident
          on incident.incident_id=delivery.incident_id
        where incident.severity='warning'
      ) then 1 else 0 end;
      update ai_operations.monitor_alert_delivery_v1
      set next_attempt_at=clock_timestamp()-interval '1 second',
          updated_at=clock_timestamp()
      where state='retryable_failed'"

POSTGRES_DSN="$clone_dsn" \
PYTHONPATH="$candidate" \
/opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import json
import os
from datetime import datetime, timezone
from uuid import UUID

import asyncpg


WORKER = UUID("91000000-0000-4000-8000-000000000001")
OTHER_WORKER = UUID("91000000-0000-4000-8000-000000000002")


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


async def record(connection):
    return decode(
        await connection.fetchval(
            """
            select ai_operations.record_monitor_observation_v1(
              $1,$2,$3,$4,$5::text[],$6,$7,$8,$9,$10,$11,$12,$13
            )
            """,
            "trusted_web_dependency",
            "unavailable",
            "critical",
            False,
            ["monitor_query_failed"],
            24,
            0,
            0,
            0,
            0,
            1,
            0,
            datetime.now(timezone.utc),
        )
    )


async def main():
    connection = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        async with connection.transaction():
            await connection.execute(
                "select set_config('app.ai_operations_alert_delivery',$1,true)",
                "authorized",
            )
            await connection.execute(
                "select set_config('app.ai_operations_alert_worker_id',$1,true)",
                str(WORKER),
            )
            try:
                async with connection.transaction():
                    await connection.fetchval(
                        "select ai_operations.claim_monitor_alert_delivery_v1($1)",
                        OTHER_WORKER,
                    )
            except asyncpg.InsufficientPrivilegeError:
                pass
            else:
                raise AssertionError("mismatched worker claim unexpectedly succeeded")
            claim = decode(
                await connection.fetchval(
                    "select ai_operations.claim_monitor_alert_delivery_v1($1)",
                    WORKER,
                )
            )
            assert claim["status"] == "claimed", claim
            assert claim["attempt_count"] == 2, claim
            delivered = decode(
                await connection.fetchval(
                    """
                    select ai_operations.complete_monitor_alert_delivery_v1(
                      $1,$2,'delivered','test_message_2',null
                    )
                    """,
                    UUID(claim["delivery_id"]),
                    WORKER,
                )
            )
            assert delivered["outcome"] == "delivered", delivered

        second = await record(connection)
        assert second["action"] == "opened", second
        async with connection.transaction():
            await connection.execute(
                "select set_config('app.ai_operations_alert_delivery',$1,true)",
                "authorized",
            )
            await connection.execute(
                "select set_config('app.ai_operations_alert_worker_id',$1,true)",
                str(WORKER),
            )
            claim = decode(
                await connection.fetchval(
                    "select ai_operations.claim_monitor_alert_delivery_v1($1)",
                    WORKER,
                )
            )
            assert claim["status"] == "claimed", claim
            failed = decode(
                await connection.fetchval(
                    """
                    select ai_operations.complete_monitor_alert_delivery_v1(
                      $1,$2,'permanent_failed',null,'invalid_recipient'
                    )
                    """,
                    UUID(claim["delivery_id"]),
                    WORKER,
                )
            )
            assert failed["outcome"] == "permanent_failed", failed
        print(
            "AI_OPERATIONS_ALERT_APP_PHASE2 "
            "worker_binding=pass retry_reclaim=pass delivered=pass "
            "permanent_failure=pass"
        )
    finally:
        await connection.close()


asyncio.run(main())
PY

docker exec "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  -c "select 1 / case when (
        select relrowsecurity and relforcerowsecurity
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where n.nspname='ai_operations'
          and c.relname='monitor_alert_delivery_v1'
      ) then 1 else 0 end;
      select 1 / case when (
        select relrowsecurity and relforcerowsecurity
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where n.nspname='ai_operations'
          and c.relname='monitor_alert_delivery_event_v1'
      ) then 1 else 0 end;
      select 1 / case when not has_table_privilege(
        'brains_app','ai_operations.monitor_alert_delivery_v1','select'
      ) and not has_table_privilege(
        'brains_app','ai_operations.monitor_alert_delivery_event_v1','select'
      ) then 1 else 0 end;
      select 1 / case when not has_schema_privilege(
        'anon','ai_operations','usage'
      ) and not has_schema_privilege(
        'authenticated','ai_operations','usage'
      ) and not has_schema_privilege(
        'service_role','ai_operations','usage'
      ) then 1 else 0 end;
      select 1 / case when has_function_privilege(
        'brains_app','ai_operations.claim_monitor_alert_delivery_v1(uuid)',
        'execute'
      ) and has_function_privilege(
        'brains_app',
        'ai_operations.complete_monitor_alert_delivery_v1(uuid,uuid,text,text,text)',
        'execute'
      ) and not has_function_privilege(
        'anon','ai_operations.claim_monitor_alert_delivery_v1(uuid)','execute'
      ) and not has_function_privilege(
        'authenticated','ai_operations.claim_monitor_alert_delivery_v1(uuid)',
        'execute'
      ) and not has_function_privilege(
        'service_role','ai_operations.claim_monitor_alert_delivery_v1(uuid)',
        'execute'
      ) then 1 else 0 end;
      select 1 / case when (
        select count(*)=2
        from ai_operations.monitor_alert_delivery_v1
      ) then 1 else 0 end;
      select 1 / case when (
        select count(*)=1
        from ai_operations.monitor_alert_delivery_v1
        where state='delivered'
          and attempt_count=2
          and provider_message_id='test_message_2'
      ) then 1 else 0 end;
      select 1 / case when (
        select count(*)=1
        from ai_operations.monitor_alert_delivery_v1
        where state='permanent_failed'
          and attempt_count=1
          and last_error_code='invalid_recipient'
      ) then 1 else 0 end;
      select 1 / case when (
        select count(*)=8
        from ai_operations.monitor_alert_delivery_event_v1
      ) then 1 else 0 end;
      select 1 / case when not exists (
        select 1 from information_schema.columns
        where table_schema='ai_operations'
          and table_name in (
            'monitor_alert_delivery_v1',
            'monitor_alert_delivery_event_v1'
          )
          and column_name ~ '(recipient|subject|body|email_address)'
      ) then 1 else 0 end;
      select 1 / case when (
        select bool_and(proconfig @> array['search_path=pg_catalog'])
        from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname='ai_operations'
          and p.proname in (
            'enqueue_monitor_alert_delivery_v1',
            'claim_monitor_alert_delivery_v1',
            'complete_monitor_alert_delivery_v1'
          )
      ) then 1 else 0 end"

if docker exec "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  -c "set session authorization ai_operations_store_v1;
      update ai_operations.monitor_alert_delivery_event_v1
      set occurred_at=clock_timestamp()" >/dev/null 2>&1
then
  printf 'append-only delivery event mutation unexpectedly succeeded\n' >&2
  exit 1
fi

docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260731_ai_operations_alert_delivery_v1_rollback.sql"
docker exec "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  -c "select 1 / case when
        to_regclass('ai_operations.monitor_incident_v1') is not null
        and to_regclass('ai_operations.monitor_incident_event_v1') is not null
        and to_regclass('ai_operations.monitor_alert_delivery_v1') is null
        and to_regclass('ai_operations.monitor_alert_delivery_event_v1') is null
        and to_regprocedure(
          'ai_operations.claim_monitor_alert_delivery_v1(uuid)'
        ) is null
      then 1 else 0 end"
docker exec -i "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  < "$candidate/ops/sql/20260731_ai_operations_alert_delivery_v1.sql"
docker exec "$clone_name" psql \
  -X -v ON_ERROR_STOP=1 -U sage -d memory \
  -c "select 1 / case when
        to_regclass('ai_operations.monitor_alert_delivery_v1') is not null
        and to_regclass('ai_operations.monitor_alert_delivery_event_v1')
          is not null
        and (select count(*)=0
             from ai_operations.monitor_alert_delivery_v1)
        and (select count(*)=0
             from ai_operations.monitor_alert_delivery_event_v1)
      then 1 else 0 end"

production_after="$(
  docker exec "$live_container" psql -X -U sage -d memory -At -F '|' -c \
    "select count(*),coalesce(max(created_at)::text,'none'),
            coalesce(to_regclass('ai_operations.monitor_alert_delivery_v1')::text,'none')
     from trusted_web.retrieval_audit"
)"
if test "$production_before" != "$production_after"; then
  printf 'production fingerprint changed during alert delivery clone gate\n' >&2
  exit 1
fi

printf '%s\n' \
  'AI_OPERATIONS_ALERT_CLONE_GATE apply=pass acl=pass rls=pass' \
  'dedupe=pass warning_excluded=pass drill_excluded=pass' \
  'worker_binding=pass retry=pass' \
  'append_only=pass rollback=pass reapply=pass production_unchanged=pass'
