#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Verifies the historical-claim/surface-policy reader
# contract on a disposable production clone. Production rows and Qdrant are
# read-only; no model, embedding, staging, retrieval, or prompt path is used.

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
container=brains-postgres-1
source_db=memory
clone_db="memory_v5_projection_recovery_$(date -u +%Y%m%d%H%M%S)_$$"
migration="$repo_root/ops/sql/20260725_memory_v1_v5_shadow_claim_contract.sql"
security_test="$repo_root/tests/memory_v1_v5_shadow_claim_contract.sql"
expected_migration_sha=17957ba743ce3f05709c18af19c2c9d602d4ec77dfcd350126fac176ce9bc50f
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
artifact_dir="/home/ubuntu/memory-v1-reviews/compiler-v8-projection-recovery-clone-$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "$repo_root" rev-parse --short=12 HEAD)"

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
[[ "$(sha256sum "$migration" | awk '{print $1}')" == "$expected_migration_sha" ]]
mkdir -p "$artifact_dir"
chmod 0700 "$artifact_dir"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

memory_data_signature() {
  local database=$1
  local table state
  {
    while IFS= read -r table; do
      state=$(docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
        -U sage -d "$database" -c "
          SELECT count(*)::text || E'\t' ||
                 encode(public.digest(convert_to(
                   coalesce(string_agg(row_json,E'\n' ORDER BY row_json),''),
                   'UTF8'),'sha256'),'hex')
          FROM (
            SELECT to_jsonb(value)::text AS row_json
            FROM memory.\"$table\" AS value
          ) AS rows
        ")
      printf '%s\t%s\n' "$table" "$state"
    done < <(
      docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
        -U sage -d "$database" -c "
          SELECT table_name FROM information_schema.tables
          WHERE table_schema='memory' AND table_type='BASE TABLE'
          ORDER BY table_name
        "
    )
  } | sha256sum | awk '{print $1}'
}

production_before=$(memory_data_signature "$source_db")
qdrant_before=$(qdrant_signature)
docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"
clone_before=$(memory_data_signature "$clone_db")

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" <"$migration" >"$artifact_dir/migration.log"
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone_db" <"$security_test" >"$artifact_dir/security.log"

clone_after=$(memory_data_signature "$clone_db")
production_after=$(memory_data_signature "$source_db")
qdrant_after=$(qdrant_signature)
[[ "$clone_after" == "$clone_before" ]]
[[ "$production_after" == "$production_before" ]]
[[ "$qdrant_after" == "$qdrant_before" ]]
grep -F 'memory_v1_v5_shadow_claim_contract: PASS' \
  "$artifact_dir/security.log" >/dev/null

report="$artifact_dir/report.json"
jq -n \
  --arg head "$(git -C "$repo_root" rev-parse HEAD)" \
  --arg migration_sha256 "$expected_migration_sha" \
  --arg production_memory_sha256 "$production_after" \
  --arg clone_memory_sha256 "$clone_after" \
  --arg qdrant_sha256 "$qdrant_after" \
  --arg owner "$owner" \
  '{
    contract_version:"memory_v1_v5_compiler_v8_projection_recovery_clone_report_v1",
    head_commit:$head,
    owner_user_id:$owner,
    migration_sha256:$migration_sha256,
    clone_memory_rows_unchanged:true,
    production_memory_rows_unchanged:true,
    production_memory_sha256:$production_memory_sha256,
    clone_memory_sha256:$clone_memory_sha256,
    qdrant_unchanged:true,
    qdrant_sha256:$qdrant_sha256,
    cross_owner_read_count:0,
    external_model_calls:0,
    embedding_requests:0,
    production_writes:0,
    retrieval_activated:false,
    prompt_influence_activated:false
  }' >"$report"
chmod 0600 "$artifact_dir"/*
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
printf 'report=%s\n' "$report"
printf 'report_sha256=%s\n' "$(awk '{print $1}' "$report.sha256")"
printf 'memory_v1_v5_compiler_v8_four_claim_projection_recovery_clone: PASS\n'
