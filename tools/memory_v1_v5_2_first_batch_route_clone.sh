#!/usr/bin/env bash
set -euo pipefail

# seebx only. Reconciles the exact original 66-packet batch inside a
# disposable production clone. Production Postgres and Qdrant stay read-only.

[[ "$EUID" -eq 0 ]]
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

container=brains-postgres-1
production=memory
clone="memory_v5_2_first_batch_route_$(date -u +%Y%m%d%H%M%S)_$$"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
manifest=manifests/memory_v1_v5_2_first_batch_route_20260728.json
manifest_sha=508080c08f199326577a4992a9b4f654b1d0f6caead5b66bdc77eaf456404a9d
terminal_worker=scripts/memory_v1_v5_2_exact_terminal_batch.py
terminal_worker_sha=f74c9de2434f90738d2c022b6b292bf0bcd09a5f88ca429885739b83d1d04147
terminal_test=tests/test_memory_v1_v5_2_exact_terminal_batch.py
terminal_test_sha=06fab06ced363babca09148a76b3fc515076de70a400495818588adbb4a1c5b2
review_worker=scripts/memory_v1_v5_2_exact_review_route.py
review_worker_sha=bc6d0f915b2e8bc9c42489abc0bbf7f9c8ae593d575b18291d004c58fee5e6ab
review_test=tests/test_memory_v1_v5_2_exact_review_route.py
review_test_sha=1c89259f58c2eea89c86e5263cf58f7c51104f555fe8f9f6082279ac444afd90
python_bin=/opt/chat-memory/venv/bin/python
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
artifact_dir="/home/ubuntu/memory-v1-reviews/first-batch-route-clone-$run_tag"
reviews="$artifact_dir/reviews"
backup=$(mktemp /tmp/memory-v5-2-first-batch.XXXXXX.dump)
production_before=$(mktemp /tmp/memory-v5-2-first-batch.XXXXXX.before)
production_after=$(mktemp /tmp/memory-v5-2-first-batch.XXXXXX.after)

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup" "$production_before" "$production_after"
}
trap cleanup EXIT

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$1" -c "$2" | tr -d '[:space:]'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

capture_production() {
  docker exec "$container" psql -X -A -F $'\t' -t -v ON_ERROR_STOP=1 \
    -U sage -d "$production" -c "
      SELECT 'claims',count(*) FROM memory.claim
      UNION ALL SELECT 'entities',count(*) FROM memory.entity
      UNION ALL SELECT 'observations',count(*) FROM memory.observation
      UNION ALL SELECT 'stage',count(*) FROM memory.relational_stage_batch
      UNION ALL SELECT 'routes',count(*) FROM memory.v5_2_local_packet_route_event
      UNION ALL SELECT 'dispositions',count(*) FROM memory.v5_local_packet_disposition
      ORDER BY 1
    " >"$1"
}

[[ -z "$(git status --porcelain)" ]]
[[ "$(sha256sum "$manifest" | awk '{print $1}')" == "$manifest_sha" ]]
[[ "$(sha256sum "$terminal_worker" | awk '{print $1}')" == "$terminal_worker_sha" ]]
[[ "$(sha256sum "$terminal_test" | awk '{print $1}')" == "$terminal_test_sha" ]]
[[ "$(sha256sum "$review_worker" | awk '{print $1}')" == "$review_worker_sha" ]]
[[ "$(sha256sum "$review_test" | awk '{print $1}')" == "$review_test_sha" ]]
git merge-base --is-ancestor "$(jq -er '.required_ancestor_commit' "$manifest")" HEAD
[[ "$(jq -er '.owner_user_id' "$manifest")" == "$owner" ]]
[[ "$(jq -er '.expected_counts.terminal_routes' "$manifest")" == 48 ]]
[[ "$(jq -er '.expected_counts.no_stage_dispositions' "$manifest")" == 7 ]]
[[ "$(jq -er '.expected_counts.review_routes' "$manifest")" == 11 ]]
[[ "$(jq -er '.external_model_calls' "$manifest")" == 0 ]]
[[ -x "$python_bin" && -x "$terminal_worker" && -x "$review_worker" ]]
bash -n "$0"

set -a
source /opt/chat-memory/.env
set +a
[[ -n "${POSTGRES_DSN:-}" ]]

mapfile -t terminal_items < <(
  jq -er '.terminal_items[] |
    "\(.packet_id):\(.packet_storage_sha256)"' "$manifest"
)
mapfile -t disposition_items < <(
  jq -er '.disposition_items[] |
    "\(.packet_id):\(.packet_storage_sha256):\(.reason_code)"' "$manifest"
)
mapfile -t review_ids < <(jq -er '.review_items[].packet_id' "$manifest")
mapfile -t all_packet_ids < <(
  jq -er '[.terminal_items[],.disposition_items[],.review_items[]] |
    .[].packet_id' "$manifest"
)
mapfile -t all_evidence_ids < <(
  jq -er '[.terminal_items[],.disposition_items[],.review_items[]] |
    .[].evidence_id' "$manifest"
)
[[ "${#terminal_items[@]}" -eq 48 ]]
[[ "${#disposition_items[@]}" -eq 7 ]]
[[ "${#review_ids[@]}" -eq 11 ]]
[[ "${#all_packet_ids[@]}" -eq 66 ]]
[[ "${#all_evidence_ids[@]}" -eq 66 ]]
packet_csv=$(IFS=,; printf '%s' "${all_packet_ids[*]}")
evidence_csv=$(IFS=,; printf '%s' "${all_evidence_ids[*]}")

install -d -o ubuntu -g ubuntu -m 0700 "$artifact_dir" "$reviews"
chmod 0600 "$backup" "$production_before" "$production_after"
capture_production "$production_before"
qdrant_before=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc --no-owner >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --no-owner <"$backup"

clone_dsn=$(
  SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone" "$python_bin" -c \
    'import os; from urllib.parse import urlsplit,urlunsplit
v=urlsplit(os.environ["SOURCE_DSN"])
print(urlunsplit((v.scheme,v.netloc,"/"+os.environ["CLONE_DB"],v.query,v.fragment)))'
)

[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
")" == 66 ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
")" == 0 ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid
    AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
")" == 0 ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id=ANY(string_to_array('$evidence_csv',',')::uuid[])
")" == 0 ]]

PYTHONPATH="$repo_root" "$python_bin" -m unittest \
  tests.test_memory_v1_v5_2_exact_terminal_batch \
  tests.test_memory_v1_v5_2_exact_review_route

terminal_args=()
for item in "${terminal_items[@]}"; do
  terminal_args+=(--terminal-item "$item")
done
for item in "${disposition_items[@]}"; do
  terminal_args+=(--disposition-item "$item")
done
review_args=()
for packet_id in "${review_ids[@]}"; do
  review_args+=(--packet-id "$packet_id")
done

run_terminal() {
  local actor=$1 output=$2 apply=${3:-false}
  local -a command=(
    "$python_bin" "$terminal_worker"
    --owner-user-id "$actor"
    "${terminal_args[@]}"
  )
  if [[ "$apply" == true ]]; then command+=(--apply); fi
  runuser -u ubuntu -- env \
    POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
    MEMORY_V1_V5_2_EXACT_TERMINAL_BATCH_APPLY=memory_v1_v5_2_exact_terminal_batch_apply_v1 \
    "${command[@]}" >"$output"
  chmod 0600 "$output"
}

run_review() {
  local actor=$1 output=$2 apply=${3:-false}
  local -a command=(
    "$python_bin" "$review_worker"
    --owner-user-id "$actor"
    --review-root "$reviews"
    "${review_args[@]}"
  )
  if [[ "$apply" == true ]]; then command+=(--apply); fi
  runuser -u ubuntu -- env \
    POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
    MEMORY_V1_V5_2_EXACT_REVIEW_ROUTE_APPLY=memory_v1_v5_2_exact_review_route_apply_v1 \
    "${command[@]}" >"$output"
  chmod 0600 "$output"
}

terminal_dry="$artifact_dir/terminal-dry.json"
terminal_apply="$artifact_dir/terminal-apply.json"
review_dry="$artifact_dir/review-dry.json"
review_apply="$artifact_dir/review-apply.json"
review_replay="$artifact_dir/review-replay.json"
isolation_terminal="$artifact_dir/isolation-terminal.json"
isolation_review="$artifact_dir/isolation-review.json"

run_terminal "$owner" "$terminal_dry"
jq -e '
  .apply==false and .packet_count==55 and
  .terminal_route_count==48 and .disposition_count==7 and
  .disposition_reason_counts.deferral_only_no_stage==2 and
  .disposition_reason_counts.deferral_only_review_unresolved==5 and
  .database_writes==0 and .stage_writes==0 and .claim_writes==0 and
  .qdrant_writes==0 and .external_model_calls==0 and
  .prompt_influence==0 and .zero_write_replay_proved==true
' "$terminal_dry" >/dev/null

if run_terminal "$other" "$isolation_terminal"; then
  echo 'cross-owner terminal batch unexpectedly succeeded' >&2
  exit 1
fi

run_terminal "$owner" "$terminal_apply" true
jq -e '
  .apply==true and .packet_count==55 and
  .terminal_route_count==48 and .disposition_count==7 and
  .database_writes==55 and .transactional_apply_proved==true and
  .zero_write_replay_proved==true and .stage_writes==0 and
  .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$terminal_apply" >/dev/null

run_review "$owner" "$review_dry"
jq -e '
  .apply==false and (.plans|length)==11 and
  ([.plans[].route]|all(.=="manual_review_artifact_ready")) and
  .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$review_dry" >/dev/null

run_review "$other" "$isolation_review"
jq -e '
  .apply==false and (.plans|length)==11 and
  ([.plans[].route]|all(.=="no_work")) and
  .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$isolation_review" >/dev/null

run_review "$owner" "$review_apply" true
jq -e '
  .apply==true and .outcome=="manual_review_artifacts_ready" and
  .write_counts.route_events==11 and
  .write_counts.restricted_review_artifacts==22 and
  .write_counts.stage==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .transactional_apply_proved==true and
  .zero_write_replay_proved==true and .external_model_calls==0
' "$review_apply" >/dev/null

run_review "$owner" "$review_replay"
jq -e '
  .apply==false and (.plans|length)==11 and
  ([.plans[].route]|all(.=="no_work")) and
  .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$review_replay" >/dev/null

[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
    AND route='terminal_no_stage'
")" == 48 ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.v5_2_local_packet_route_event
  WHERE owner_user_id='$owner'::uuid
    AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
    AND route='manual_review_artifact_ready'
")" == 11 ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid
    AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
    AND reason_code='deferral_only_no_stage'
")" == 2 ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid
    AND packet_id=ANY(string_to_array('$packet_csv',',')::uuid[])
    AND reason_code='deferral_only_review_unresolved'
")" == 5 ]]
[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.relational_stage_batch
  WHERE owner_user_id='$owner'::uuid
    AND evidence_id=ANY(string_to_array('$evidence_csv',',')::uuid[])
")" == 0 ]]

artifact_count=$(find "$reviews" -maxdepth 1 -type f -name '*.json' | wc -l)
[[ "$artifact_count" -eq 22 ]]
while IFS= read -r path; do
  [[ "$(stat -c '%a:%U:%G' "$path")" == 600:ubuntu:ubuntu ]]
done < <(find "$reviews" -maxdepth 1 -type f -name '*.json' | sort)

capture_production "$production_after"
cmp -s "$production_before" "$production_after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_before" == "$qdrant_after" ]]

report="$artifact_dir/report.json"
jq -n \
  --arg head_commit "$(git rev-parse HEAD)" \
  --arg manifest_sha256 "$manifest_sha" \
  --arg qdrant_sha256 "$qdrant_after" \
  '{
    contract_version:"memory_v1_v5_2_first_batch_route_clone_report_v1",
    head_commit:$head_commit,
    manifest_sha256:$manifest_sha256,
    packet_count:66,
    terminal_route_events:48,
    no_stage_dispositions:7,
    review_route_events:11,
    restricted_review_artifacts:22,
    transactional_apply_proved:true,
    zero_write_replay_proved:true,
    account_isolation_proved:true,
    production_database_unchanged:true,
    qdrant_unchanged:true,
    qdrant_sha256:$qdrant_sha256,
    external_model_calls:0,
    stage_writes:0,
    claim_writes:0,
    prompt_influence:0,
    hard_stop:"before_relational_staging_claims_qdrant_retrieval_or_prompt_influence"
  }' >"$report"
chown ubuntu:ubuntu "$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chown ubuntu:ubuntu "$report.sha256"
chmod 0600 "$report.sha256"

printf 'memory_v1_v5_2_first_batch_route_clone: PASS\n'
printf 'report=%s\nreport_sha256=%s\n' \
  "$report" "$(awk '{print $1}' "$report.sha256")"
