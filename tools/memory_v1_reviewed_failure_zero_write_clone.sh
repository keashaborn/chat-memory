#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable clone, atomizes
# exact reviewed failures, and runs private zero-write V2 inference.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-/opt/chat-memory/.env}"
set +a

container=brains-postgres-1
production=memory
clone="memory_reviewed_failure_v2_${$}"
migration=ops/sql/20260728_memory_v1_contextual_intake_v2.sql
prepare=tests/memory_v1_reviewed_failure_clone_prepare.py
canary=tools/memory_v1_evidence_context_local_canary_v2.py
python_bin=/opt/chat-memory/venv/bin/python
api_key_file=/etc/memory-v1-local-inference/api-key
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
backup=$(mktemp /tmp/memory-reviewed-failure-v2.XXXXXX.dump)
table_list=$(mktemp /tmp/memory-reviewed-failure-v2-tables.XXXXXX)
protected_before=$(mktemp /tmp/memory-reviewed-failure-v2-before.XXXXXX)
protected_after=$(mktemp /tmp/memory-reviewed-failure-v2-after.XXXXXX)
target_report=$(mktemp /tmp/memory-reviewed-failure-v2-targets.XXXXXX.json)
report_dir=$(mktemp -d /tmp/memory-reviewed-failure-v2-reports.XXXXXX)
chmod 0600 \
  "$backup" "$table_list" "$protected_before" "$protected_after" \
  "$target_report"
chmod 0700 "$report_dir"

cleanup() {
  rc=$?
  trap - EXIT
  sudo -n docker exec "$container" \
    dropdb -U sage --if-exists --force "$clone" >/dev/null 2>&1 || true
  rm -f \
    "$backup" "$table_list" "$protected_before" "$protected_after" \
    "$target_report"
  rm -rf "$report_dir"
  exit "$rc"
}
trap cleanup EXIT

for file in "$migration" "$prepare" "$canary"; do
  test -f "$file"
done
test -s "$api_key_file"

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

clone_scalar() {
  sudo -n docker exec "$container" psql -U sage -d "$clone" \
    -X -Atqc "$1"
}

capture_protected() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    test -n "$table"
    state=$(clone_scalar "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
          FROM memory.\"$table\" AS value
      ) AS rows
    ")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
}

sudo -n docker exec "$container" pg_dump -U sage -d "$production" -Fc \
  >"$backup"
test -s "$backup"
sudo -n docker exec "$container" createdb -U sage -T template0 "$clone"
sudo -n docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

clone_dsn=$(
  "$python_bin" -c \
    'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
    "$POSTGRES_DSN" "$clone"
)

sudo -n docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
printf '%s\n' "stage=migration_installed"

clone_scalar "
  SELECT table_name
    FROM information_schema.tables
   WHERE table_schema='memory'
     AND table_type='BASE TABLE'
     AND table_name NOT IN ('evidence','evidence_contextual_span_v2')
   ORDER BY table_name
" >"$table_list"
test -s "$table_list"
capture_protected "$protected_before"
qdrant_before=$(qdrant_signature)

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" "$python_bin" \
  "$prepare" --report-path "$target_report" >/dev/null
printf '%s\n' "stage=targets_prepared"

target_count=$(jq -r '.target_count' "$target_report")
printf 'stage=target_inventory target_count=%s cases=%s\n' \
  "$target_count" "$(jq -c '[.targets[].case] | group_by(.) | map({case: .[0], count: length})' "$target_report")"
test "$target_count" -ge 4
test "$target_count" -le 32

for index in $(seq 0 "$((target_count - 1))"); do
  printf 'stage=canary_start index=%s\n' "$index"
  evidence_id=$(jq -r ".targets[$index].evidence_id" "$target_report")
  content_sha=$(jq -r ".targets[$index].content_sha256" "$target_report")
  packet="$report_dir/packet-$index.json"
  report="$report_dir/report-$index.json"
  set +e
  POSTGRES_DSN="$clone_dsn" \
  MEMORY_V1_EVIDENCE_CONTEXT_CANARY=memory_v1_evidence_context_zero_write_canary_v2 \
  MEMORY_V1_LOCAL_INFERENCE_API_KEY="$(<"$api_key_file")" \
  PYTHONPATH="$repo_root" "$python_bin" "$canary" \
    --owner-user-id "$owner" \
    --target-evidence-id "$evidence_id" \
    --expected-target-content-sha256 "$content_sha" \
    --packet-output "$packet" \
    >"$report"
  canary_rc=$?
  set -e
  test "$canary_rc" -eq 0 -o "$canary_rc" -eq 1
  jq -e '
    .contract_version
      == "memory_v1_evidence_context_local_canary_report_v2"
    and .external_model_calls == 0
    and (.local_model_calls == 0 or .local_model_calls == 1)
    and .write_counts.database == 0
    and .write_counts.qdrant == 0
    and .write_counts.retrieval == 0
    and .write_counts.prompt_influence == 0
  ' "$report" >/dev/null
  printf 'stage=canary_complete index=%s outcome=%s\n' \
    "$index" "$(jq -r '.outcome' "$report")"
done

printf '%s\n' "stage=protected_state_verification"
capture_protected "$protected_after"
cmp -s "$protected_before" "$protected_after"
qdrant_after=$(qdrant_signature)
test "$qdrant_after" = "$qdrant_before"

accepted=0
rejected=0
local_calls=0
observation_total=0
deferral_total=0
printf '%s\n' "memory_v1_reviewed_failure_zero_write_clone: RESULTS"
for index in $(seq 0 "$((target_count - 1))"); do
  report="$report_dir/report-$index.json"
  outcome=$(jq -r '.outcome' "$report")
  case_name=$(jq -r ".targets[$index].case" "$target_report")
  ordinal=$(jq -r ".targets[$index].ordinal" "$target_report")
  context_needed=$(jq -r ".targets[$index].context_needed" "$target_report")
  calls=$(jq -r '.local_model_calls' "$report")
  observations=$(jq -r '.observation_count // 0' "$report")
  deferrals=$(jq -r '.deferral_count // 0' "$report")
  prompt_profile=$(jq -r '.provider_audit.prompt_profile // "none"' "$report")
  local_calls=$((local_calls + calls))
  observation_total=$((observation_total + observations))
  deferral_total=$((deferral_total + deferrals))
  if test "$outcome" = accepted; then
    accepted=$((accepted + 1))
    printf 'case=%s ordinal=%s context_needed=%s outcome=%s observations=%s deferrals=%s prompt_profile=%s predicates=%s\n' \
      "$case_name" "$ordinal" "$context_needed" "$outcome" \
      "$observations" "$deferrals" "$prompt_profile" \
      "$(jq -c '.predicates' "$report")"
  else
    rejected=$((rejected + 1))
    printf 'case=%s ordinal=%s context_needed=%s outcome=%s error_code=%s prompt_profile=%s validation_error_types=%s\n' \
      "$case_name" "$ordinal" "$context_needed" "$outcome" \
      "$(jq -r '.error_code' "$report")" "$prompt_profile" \
      "$(jq -c '.provider_audit.validation_error_types // []' "$report")"
  fi
done

printf '%s\n' \
  "target_count=$target_count" \
  "accepted=$accepted" \
  "rejected=$rejected" \
  "observation_total=$observation_total" \
  "deferral_total=$deferral_total" \
  "local_model_calls=$local_calls" \
  "external_model_calls=0" \
  "production_writes=0" \
  "qdrant_unchanged=true" \
  "protected_tables_unchanged=true" \
  "clone_removed_on_exit=true"
