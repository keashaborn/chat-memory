#!/usr/bin/env bash
set -euo pipefail

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8088}"
BASE="http://${HOST}:${PORT}"

SERVICE_HEADERS=()
if [[ -n "${VS_SERVICE_TOKEN:-}" ]]; then
  SERVICE_HEADERS=(-H "x-vs-service-token: ${VS_SERVICE_TOKEN}")
fi

PGHOST="${PGHOST:-localhost}"
PGUSER="${PGUSER:-sage}"
PGDATABASE="${PGDATABASE:-memory}"
PGPASSWORD="${PGPASSWORD:-ci_only_postgres_password}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1"; exit 2; }; }
need curl
need psql
need python3

RID="canary-$(date -u +%Y%m%d_%H%M%S)-$RANDOM"
ACTOR_UUID="11111111-1111-4111-8111-111111111111"
OTHER_UUID="22222222-2222-4222-8222-222222222222"
echo "RID=$RID"

echo
echo "== bootstrap schema (if needed) =="
# CI runs against an empty Postgres. Ensure the minimal schema exists.
if ! PGPASSWORD="${PGPASSWORD}" psql -qtAX -h "${PGHOST}" -U "${PGUSER}" -d "${PGDATABASE}" -c \
  "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='vantage_answer_trace';" \
  | grep -q '^1$'; then
  PGPASSWORD="${PGPASSWORD}" psql -h "${PGHOST}" -U "${PGUSER}" -d "${PGDATABASE}" -f "$(dirname "$0")/ci_bootstrap.sql"
fi


echo "== wait for healthz =="
for i in $(seq 1 120); do
  curl -sf "${SERVICE_HEADERS[@]}" "${BASE}/healthz" >/dev/null && break
  sleep 0.25
done

echo "== healthz echo =="
hdr="$(curl -sS -i "${SERVICE_HEADERS[@]}" "${BASE}/healthz" -H "x-request-id: ${RID}" | sed -n '1,30p')"
echo "$hdr" | rg -i '^x-request-id:' >/dev/null || { echo "FAIL: no x-request-id echo"; echo "$hdr"; exit 1; }
echo "$hdr" | rg -i "x-request-id:\s*${RID}\b" >/dev/null || { echo "FAIL: x-request-id mismatch"; echo "$hdr"; exit 1; }
echo "OK"

echo
echo "== memory routes enforce actor/owner equality =="
missing_status="$(curl -sS -o /dev/null -w '%{http_code}' \
  "${SERVICE_HEADERS[@]}" "${BASE}/vantage/query" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"${ACTOR_UUID}\",\"message\":\"missing actor\"}")"
[[ "${missing_status}" == "401" ]] || { echo "FAIL: vantage/query missing actor=${missing_status}"; exit 1; }

mismatch_status="$(curl -sS -o /dev/null -w '%{http_code}' \
  "${SERVICE_HEADERS[@]}" "${BASE}/vantage/query" \
  -H "Content-Type: application/json" \
  -H "x-vs-actor-user-id: ${OTHER_UUID}" \
  -d "{\"user_id\":\"${ACTOR_UUID}\",\"message\":\"mismatched actor\"}")"
[[ "${mismatch_status}" == "403" ]] || { echo "FAIL: vantage/query mismatch=${mismatch_status}"; exit 1; }

log_missing_status="$(curl -sS -o /dev/null -w '%{http_code}' \
  "${SERVICE_HEADERS[@]}" "${BASE}/log" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"${ACTOR_UUID}\",\"text\":\"missing actor\"}")"
[[ "${log_missing_status}" == "401" ]] || { echo "FAIL: log missing actor=${log_missing_status}"; exit 1; }

log_mismatch_status="$(curl -sS -o /dev/null -w '%{http_code}' \
  "${SERVICE_HEADERS[@]}" "${BASE}/log" \
  -H "Content-Type: application/json" \
  -H "x-vs-actor-user-id: ${OTHER_UUID}" \
  -d "{\"user_id\":\"${ACTOR_UUID}\",\"text\":\"mismatched actor\"}")"
[[ "${log_mismatch_status}" == "403" ]] || { echo "FAIL: log mismatch=${log_mismatch_status}"; exit 1; }
echo "OK"

echo
echo "== vantage/query writes request_id =="
curl -sfS "${BASE}/vantage/query" \
  "${SERVICE_HEADERS[@]}" \
  -H "Content-Type: application/json" \
  -H "x-request-id: ${RID}" \
  -H "x-vs-actor-user-id: ${ACTOR_UUID}" \
  -d "{\"user_id\":\"${ACTOR_UUID}\",\"message\":\"audit canary\",\"vantage_id\":\"default\",\"top_k\":1,\"debug\":false}" \
  >/dev/null

PGPASSWORD="${PGPASSWORD}" psql -P pager=off -h "${PGHOST}" -U "${PGUSER}" -d "${PGDATABASE}" -c \
"SELECT request_id, answer_id::text, created_at
 FROM public.vantage_answer_trace
 WHERE request_id='${RID}'
 ORDER BY created_at DESC
 LIMIT 1;" | rg "${RID}" >/dev/null || { echo "FAIL: no vantage_answer_trace row for request_id"; exit 1; }
echo "OK"

echo
echo "== telemetry/event stamps payload.request_id =="
EVENT_ID="$(python3 - <<'PY'
import uuid
print(uuid.uuid4())
PY
)"

echo "== wait for telemetry endpoint =="
for i in $(seq 1 120); do
  curl -sf "${SERVICE_HEADERS[@]}" "${BASE}/healthz" >/dev/null && break
  sleep 0.25
done

curl -sfS "${BASE}/telemetry/event" \
  "${SERVICE_HEADERS[@]}" \
  -H "Content-Type: application/json" \
  -H "x-request-id: ${RID}" \
  -H "x-vs-actor-user-id: ${ACTOR_UUID}" \
  -d "{\"events\":[{\"event_id\":\"${EVENT_ID}\",\"event_type\":\"audit.canary\",\"subject_type\":\"user\",\"subject_id\":\"audit_user\",\"payload\":{\"note\":\"canary\",\"request_id\":\"${RID}\"}}]}" \
  >/dev/null

PGPASSWORD="${PGPASSWORD}" psql -P pager=off -h "${PGHOST}" -U "${PGUSER}" -d "${PGDATABASE}" -c \
"SELECT event_id::text, payload->>'request_id' AS request_id
 FROM telemetry_event
 WHERE event_id='${EVENT_ID}';" | rg "${RID}" >/dev/null || { echo "FAIL: telemetry_event missing payload.request_id"; exit 1; }
echo "OK"

echo
echo "✅ audit canary passed"
