#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a production clone, installs the additive
# legacy-only reintake lane, applies exactly the currently reviewed test-owner
# plan on the clone, proves replay, rollback, and production isolation.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source .env
set +a

container=brains-postgres-1
production=memory
clone="memory_v5_legacy_reintake_${$}"
migration=ops/sql/20260719_memory_v1_v5_legacy_claim_reintake.sql
rollback=ops/sql/20260719_memory_v1_v5_legacy_claim_reintake_rollback.sql
test_sql=tests/memory_v1_v5_legacy_claim_reintake.sql
worker=scripts/memory_v1_v5_legacy_claim_reintake.py
worker_test=scripts/memory_v1_v5_legacy_claim_reintake_test.py
owner_a=557ea042-cb82-48f8-9429-472e96c957ef
owner_b=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
selector=20260719_v5_legacy_claim_reintake_v1
backup=$(mktemp /tmp/memory-v1-v5-legacy-reintake.XXXXXX.dump)
dry=$(mktemp /tmp/memory-v1-v5-legacy-reintake-dry.XXXXXX.json)
applied=$(mktemp /tmp/memory-v1-v5-legacy-reintake-applied.XXXXXX.json)
chmod 0600 "$backup" "$dry" "$applied"
phase=initialize

cleanup() {
  rc=$?
  trap - EXIT
  docker exec "$container" dropdb -U sage --if-exists "$clone" >/dev/null 2>&1 || true
  if (( rc != 0 )); then
    printf 'memory_v1_v5_legacy_claim_reintake_clone: FAIL phase=%s rc=%s\n' "$phase" "$rc" >&2
    for diagnostic in "$dry.stdout" "$applied.stdout"; do
      if [[ -s "$diagnostic" ]]; then
        jq -c '{apply,owner_count,totals,claim_writes,qdrant_writes,external_model_calls,local_model_calls,prompt_influence}' \
          "$diagnostic" >&2 || true
      fi
    done
  fi
  rm -f "$backup" "$dry" "$applied" "$dry.stdout" "$applied.stdout"
  exit "$rc"
}
trap cleanup EXIT

clone_sql() {
  docker exec -i "$container" psql -U sage -d "$clone" -X \
    -v ON_ERROR_STOP=1 "$@"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

[[ -z "$(git status --porcelain)" ]]
phase=offline_tests
PYTHONPATH="$repo_root" venv/bin/python "$worker_test" >/dev/null
python3 -m py_compile "$worker"
phase=production_baseline
production_jobs_before=$(docker exec "$container" psql -U sage -d "$production" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector'")
production_claims_before=$(docker exec "$container" psql -U sage -d "$production" -X -Atqc \
  'SELECT count(*) FROM memory.claim')
qdrant_before=$(qdrant_signature)

phase=clone_restore
docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" --exit-on-error <"$backup"

phase=migration_and_security
clone_sql <"$migration" >/dev/null
clone_sql <"$migration" >/dev/null
clone_sql -v owner_a="$owner_a" -v owner_b="$owner_b" <"$test_sql" >/dev/null

clone_dsn=$(venv/bin/python -c \
  'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
  "$POSTGRES_DSN" "$clone")
phase=dry_run
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner_a" --owner-user-id "$owner_b" --limit 100 \
  --report-path "$dry" >"$dry.stdout"
jq -e '.apply==false and .totals.planned==8 and .totals.applied==0 and
  .totals.after==8 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .local_model_calls==0 and .prompt_influence==0' \
  "$dry.stdout" >/dev/null

phase=transactional_apply
MEMORY_V1_V5_LEGACY_REINTAKE_APPLY=memory_v1_v5_legacy_claim_reintake_apply_v1 \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner_a" --owner-user-id "$owner_b" --limit 100 --apply \
  --report-path "$applied" >"$applied.stdout"
jq -e '.apply==true and .totals.planned==8 and .totals.applied==8 and
  .totals.replayed==8 and .totals.after==0 and .claim_writes==0 and
  .qdrant_writes==0 and .external_model_calls==0 and .local_model_calls==0 and
  .prompt_influence==0' "$applied.stdout" >/dev/null
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector'")" == 8 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_intake_terminal WHERE selector_version='$selector'")" == 8 ]]

phase=rollback_reinstall
clone_sql <"$rollback" >/dev/null
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT to_regprocedure('memory.plan_owner_v5_legacy_claim_reintake_v1(text,integer,uuid)') IS NULL")" == t ]]
clone_sql <"$migration" >/dev/null

phase=production_isolation
[[ "$(docker exec "$container" psql -U sage -d "$production" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector'")" == "$production_jobs_before" ]]
[[ "$(docker exec "$container" psql -U sage -d "$production" -X -Atqc \
  'SELECT count(*) FROM memory.claim')" == "$production_claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
phase=complete
rm -f "$dry.stdout" "$applied.stdout"
printf '%s\n' 'memory_v1_v5_legacy_claim_reintake_clone: PASS'
