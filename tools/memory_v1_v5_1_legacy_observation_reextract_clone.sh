#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores a production clone, installs the additive
# V5-to-V5.1 re-extraction lane, queues exactly three reviewed owner records,
# proves replay and account isolation, and leaves production unchanged.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source /opt/chat-memory/.env
set +a

container=brains-postgres-1
production=memory
clone="memory_v5_1_observation_reextract_${$}"
python=/opt/chat-memory/venv/bin/python
migration=ops/sql/20260721_memory_v1_v5_1_legacy_observation_reextract.sql
rollback=ops/sql/20260721_memory_v1_v5_1_legacy_observation_reextract_rollback.sql
test_sql=tests/memory_v1_v5_1_legacy_observation_reextract.sql
worker=scripts/memory_v1_v5_1_legacy_observation_reextract.py
worker_test=scripts/memory_v1_v5_1_legacy_observation_reextract_test.py
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
selector=20260721_v5_1_legacy_observation_reextract_v1
evidence_a=04e5a4c2-8742-5f1f-ace2-d4de9f5e94da
evidence_b=7877ebf3-1c5a-4bd2-8b43-9514a88a0e12
evidence_c=aaaa0d55-0563-4197-8c8e-a657240e6cc1
backup=$(mktemp /tmp/memory-v1-v5-1-observation-reextract.XXXXXX.dump)
dry=$(mktemp /tmp/memory-v1-v5-1-observation-reextract-dry.XXXXXX.json)
applied=$(mktemp /tmp/memory-v1-v5-1-observation-reextract-applied.XXXXXX.json)
chmod 0600 "$backup" "$dry" "$applied"
phase=initialize

cleanup() {
  rc=$?
  trap - EXIT
  docker exec "$container" dropdb -U sage --if-exists "$clone" >/dev/null 2>&1 || true
  if (( rc != 0 )); then
    printf 'memory_v1_v5_1_legacy_observation_reextract_clone: FAIL phase=%s rc=%s\n' "$phase" "$rc" >&2
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
PYTHONPATH="$repo_root" "$python" "$worker_test" >/dev/null
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
clone_sql -v owner_a="$owner" -v owner_b="$other" \
  -v target_evidence="$evidence_a" <"$test_sql" >/dev/null

clone_dsn=$("$python" -c \
  'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
  "$POSTGRES_DSN" "$clone")
common=(
  --owner-user-id "$owner"
  --evidence-id "$evidence_a"
  --evidence-id "$evidence_b"
  --evidence-id "$evidence_c"
)
phase=dry_run
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" "$python" "$worker" \
  "${common[@]}" --report-path "$dry" >"$dry.stdout"
jq -e '.apply==false and .plan_count==3 and .applied==0 and .replayed==0 and
  .claim_writes==0 and .qdrant_writes==0 and .external_model_calls==0 and
  .local_model_calls==0 and .prompt_influence==0' "$dry.stdout" >/dev/null
plan_sha=$(jq -r '.plan_sha256' "$dry.stdout")

phase=transactional_apply
MEMORY_V1_V5_1_LEGACY_OBSERVATION_REEXTRACT_APPLY=memory_v1_v5_1_legacy_observation_reextract_apply_v1 \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" "$python" "$worker" \
  "${common[@]}" --expected-plan-sha256 "$plan_sha" --apply \
  --report-path "$applied" >"$applied.stdout"
jq -e '.apply==true and .plan_count==3 and .applied==3 and .replayed==3 and
  .claim_writes==0 and .qdrant_writes==0 and .external_model_calls==0 and
  .local_model_calls==0 and .prompt_influence==0' "$applied.stdout" >/dev/null
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector' AND owner_user_id='$owner'::uuid")" == 3 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_intake_terminal WHERE selector_version='$selector' AND owner_user_id='$owner'::uuid")" == 3 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector' AND owner_user_id='$other'::uuid")" == 0 ]]

phase=rollback_reinstall
clone_sql <"$rollback" >/dev/null
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT to_regprocedure('memory.plan_owner_v5_1_legacy_observation_reextract_v1(text,integer,uuid)') IS NULL")" == t ]]
clone_sql <"$migration" >/dev/null

phase=production_isolation
[[ "$(docker exec "$container" psql -U sage -d "$production" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_extraction_job WHERE selector_version='$selector'")" == "$production_jobs_before" ]]
[[ "$(docker exec "$container" psql -U sage -d "$production" -X -Atqc \
  'SELECT count(*) FROM memory.claim')" == "$production_claims_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
phase=complete
rm -f "$dry.stdout" "$applied.stdout"
printf '%s\n' 'memory_v1_v5_1_legacy_observation_reextract_clone: PASS'
