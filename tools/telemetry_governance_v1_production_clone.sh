#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
port=${TELEMETRY_GOVERNANCE_CLONE_PORT:-55463}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p telemetrygovernancev1
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
migration=ops/sql/20260723_telemetry_rls_retention_v1.sql
rollback=ops/sql/20260723_telemetry_rls_retention_v1_rollback.sql
backup=$(mktemp /tmp/telemetry-governance-v1.XXXXXX.dump)
roles=$(mktemp /tmp/telemetry-governance-v1-roles.XXXXXX.sql)

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

app_scalar() {
  PGPASSWORD=clone_only_brains_password \
    psql -X -A -t -v ON_ERROR_STOP=1 \
      -h 127.0.0.1 -p "$port" -U brains_app -d memory -c "$1"
}

for required in "$migration" "$rollback"; do
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

run_sql <"$repo_root/$migration"
run_sql <"$repo_root/$migration"

run_sql <<'SQL'
INSERT INTO public.telemetry_event(
  event_id,event_type,subject_type,subject_id,payload,occurred_at,actor_user_id
) VALUES
(
  '81000000-0000-4000-8000-000000000001',
  'voice.turn.trace','voice_turn','clone-current','{}',
  clock_timestamp(),
  '10000000-0000-4000-8000-000000000001'
),
(
  '81000000-0000-4000-8000-000000000002',
  'voice.turn.trace','voice_turn','clone-expired','{}',
  clock_timestamp()-interval '31 days',
  '10000000-0000-4000-8000-000000000001'
),
(
  '81000000-0000-4000-8000-000000000003',
  'probe.response','probe','clone-expired','{}',
  clock_timestamp()-interval '91 days',
  '10000000-0000-4000-8000-000000000001'
),
(
  '81000000-0000-4000-8000-000000000004',
  'probe.response','probe','clone-other-owner','{}',
  clock_timestamp(),
  '20000000-0000-4000-8000-000000000002'
);
SQL

[[ "$(app_scalar "SELECT count(*) FROM public.telemetry_event")" == 0 ]]

[[ "$(app_scalar "
  SELECT set_config(
    'app.user_id',
    '10000000-0000-4000-8000-000000000001',
    false
  );
  SELECT count(*) FROM public.telemetry_event
  WHERE event_id IN (
    '81000000-0000-4000-8000-000000000001',
    '81000000-0000-4000-8000-000000000002',
    '81000000-0000-4000-8000-000000000003',
    '81000000-0000-4000-8000-000000000004'
  );
" | tail -n 1)" == 3 ]]

if app_scalar "
  SELECT set_config(
    'app.user_id',
    '10000000-0000-4000-8000-000000000001',
    false
  );
  INSERT INTO public.telemetry_event(
    event_id,event_type,subject_type,subject_id,payload,occurred_at,actor_user_id
  ) VALUES (
    '81000000-0000-4000-8000-000000000005',
    'probe.response','probe','wrong-owner','{}',clock_timestamp(),
    '20000000-0000-4000-8000-000000000002'
  );
" >/dev/null 2>&1; then
  echo "RLS owner mismatch insert unexpectedly succeeded" >&2
  exit 1
fi

retention_result=$(app_scalar \
  "SELECT memory.enforce_telemetry_retention_v1()")
[[ "$retention_result" == *'"expired_voice_deleted": 1'* ]]
[[ "$retention_result" == *'"expired_general_deleted": 1'* ]]

[[ "$(app_scalar "
  SELECT set_config(
    'app.user_id',
    '10000000-0000-4000-8000-000000000001',
    false
  );
  SELECT count(*) FROM public.telemetry_event
  WHERE event_id IN (
    '81000000-0000-4000-8000-000000000001',
    '81000000-0000-4000-8000-000000000002',
    '81000000-0000-4000-8000-000000000003'
  );
" | tail -n 1)" == 1 ]]

run_sql <"$repo_root/$rollback"
run_sql -c "
  DO \$\$
  BEGIN
    IF (
      SELECT relrowsecurity OR relforcerowsecurity
      FROM pg_class
      WHERE oid='public.telemetry_event'::regclass
    ) THEN
      RAISE EXCEPTION 'telemetry RLS rollback failed';
    END IF;
    IF to_regprocedure(
      'memory.enforce_telemetry_retention_v1()'
    ) IS NOT NULL THEN
      RAISE EXCEPTION 'retention function rollback failed';
    END IF;
  END
  \$\$;
"

echo "telemetry governance production-clone verification passed"
