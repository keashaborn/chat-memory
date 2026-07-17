#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Reads the exact V5-04 evidence and observation from a
# disposable production clone and proves the V5.1 entailment decision.

compose=(docker compose -p memoryv1predicateentailment -f docker-compose.ci.yml)
backup=$(mktemp /tmp/memory-v1-predicate-entailment.XXXXXX.dump)
fixture=$(mktemp /tmp/memory-v1-predicate-entailment.XXXXXX.json)
fixture_after=$(mktemp /tmp/memory-v1-predicate-entailment-after.XXXXXX.json)
report=$(mktemp /tmp/memory-v1-predicate-entailment-report.XXXXXX.json)

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$fixture" "$fixture_after" "$report"
}
trap cleanup EXIT
chmod 0600 "$backup" "$fixture" "$fixture_after" "$report"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory
}

capture_fixture() {
  "${compose[@]}" exec -T postgres psql -X -qAt -v ON_ERROR_STOP=1 \
    -U sage -d memory <<'SQL'
SELECT jsonb_build_object(
  'evidence', to_jsonb(evidence),
  'observation', to_jsonb(observation)
)
FROM memory.evidence AS evidence
JOIN memory.observation AS observation
  ON observation.owner_user_id=evidence.owner_user_id
 AND observation.evidence_id=evidence.evidence_id
WHERE evidence.owner_user_id='1240822d-ac9a-4096-95aa-e2b24d36ef50'::uuid
  AND evidence.evidence_id='fca9e5dc-83c2-4456-8db8-1fe6102eb74d'::uuid
  AND observation.observation_id='9bf1e6b2-1840-4524-98dc-142567ebe013'::uuid;
SQL
}

docker exec brains-postgres-1 pg_dump -U sage -d memory \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
"${compose[@]}" up -d --wait postgres
printf '%s\n' \
  'CREATE ROLE brains_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  'CREATE ROLE memory_v5_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;' \
  | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists --no-owner --no-privileges <"$backup"

capture_fixture >"$fixture"
[[ "$(wc -l <"$fixture" | tr -d ' ')" == "1" ]]
python3 -m scripts.memory_v1_predicate_entailment_v5_1_probe \
  --input "$fixture" \
  --output "$report"
capture_fixture >"$fixture_after"
cmp -s "$fixture" "$fixture_after"
python3 - "$report" <<'PY'
import json
import sys

value = json.load(open(sys.argv[1]))
assert value["mode"] == "production_clone_read_only"
assert value["decision"]["status"] == "defer"
assert value["decision"]["reason_code"] == "source_contradicts_predicate"
assert value["predicate_substitution"] is False
assert value["database_writes"] == 0
assert value["qdrant_writes"] == 0
assert value["external_model_calls"] == 0
PY

echo 'memory_v1_predicate_entailment_v5_1_production_clone: PASS'
