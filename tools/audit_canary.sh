#!/usr/bin/env bash
set -euo pipefail

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8088}"
BASE="http://${HOST}:${PORT}"

PGHOST="${PGHOST:-localhost}"
PGUSER="${PGUSER:-sage}"
PGDATABASE="${PGDATABASE:-memory}"
PGPASSWORD="${PGPASSWORD:-strongpassword}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1"; exit 2; }; }
need curl
need psql
need python3

RID="canary-$(date -u +%Y%m%d_%H%M%S)-$RANDOM"
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
  curl -sf "${BASE}/healthz" >/dev/null && break
  sleep 0.25
done

echo "== healthz echo =="
hdr="$(curl -sS -i "${BASE}/healthz" -H "x-request-id: ${RID}" | sed -n '1,30p')"
echo "$hdr" | rg -i '^x-request-id:' >/dev/null || { echo "FAIL: no x-request-id echo"; echo "$hdr"; exit 1; }
echo "$hdr" | rg -i "x-request-id:\s*${RID}\b" >/dev/null || { echo "FAIL: x-request-id mismatch"; echo "$hdr"; exit 1; }
echo "OK"

echo
echo "== vantage/query writes request_id =="
curl -sfS "${BASE}/vantage/query" \
  -H "Content-Type: application/json" \
  -H "x-request-id: ${RID}" \
  -d '{"user_id":"audit_user","message":"audit canary","vantage_id":"default","top_k":1,"debug":false}' \
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
  curl -sf "${BASE}/healthz" >/dev/null && break
  sleep 0.25
done

curl -sfS "${BASE}/telemetry/event" \
  -H "Content-Type: application/json" \
  -H "x-request-id: ${RID}" \
  -d "{\"events\":[{\"event_id\":\"${EVENT_ID}\",\"event_type\":\"audit.canary\",\"subject_type\":\"user\",\"subject_id\":\"audit_user\",\"payload\":{\"note\":\"canary\",\"request_id\":\"${RID}\"}}]}" \
  >/dev/null

PGPASSWORD="${PGPASSWORD}" psql -P pager=off -h "${PGHOST}" -U "${PGUSER}" -d "${PGDATABASE}" -c \
"SELECT event_id::text, payload->>'request_id' AS request_id
 FROM telemetry_event
 WHERE event_id='${EVENT_ID}';" | rg "${RID}" >/dev/null || { echo "FAIL: telemetry_event missing payload.request_id"; exit 1; }
echo "OK"

echo
echo "✅ audit canary passed"
