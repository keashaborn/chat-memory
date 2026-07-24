#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Admits exactly one previously deferred, reviewed claim to
# the projection outbox. No embedding, Qdrant write, retrieval, or prompt
# influence occurs in this phase.

if [[ "${MEMORY_V1_DEFERRED_PROJECTION_ADMISSION_PRODUCTION_APPLY:-}" != authorized ]]; then
  echo 'MEMORY_V1_DEFERRED_PROJECTION_ADMISSION_PRODUCTION_APPLY=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
required_ancestor=7bbf78efbbb37387ee545e695f9f52d9382cfc83
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=557ea042-cb82-48f8-9429-472e96c957ef
claim_id=8fb8b3ab-a627-4555-99c6-fe4dc9b0ca89
source_dir=/home/ubuntu/memory-v1-reviews/reconciled-production-20260724T185638Z_ab5a83e9c3f6
apply_result="$source_dir/materialize-apply.json"
apply_manifest="$source_dir/apply-manifest.json"
runner=scripts/memory_v1_v5_deferred_projection_admission.py
python_bin="$repo_root/venv/bin/python"
review_root=/home/ubuntu/memory-v1-reviews
snapshot_root=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_deferred_projection_admission.lock
phase=initialization
timers_quiesced=0
brains_quiesced=0
brains_state_before=
artifact_dir=
status_file=
run_id=
timer_state=$(mktemp /tmp/memory-v1-deferred-projection-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v1-deferred-projection-tables.XXXXXX)

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

qdrant_claim_count() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d "{\"ids\":[\"$claim_id\"],\"with_payload\":true,\"with_vector\":false}" \
    http://127.0.0.1:6333/collections/memory_claim_v1/points \
    | jq -er '.result|length'
}

psql_row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
}

authenticated_health() {
  set -a
  source "$repo_root/.env"
  set +a
  for _attempt in $(seq 1 30); do
    if [[ "$(systemctl is-active brains.service)" == active ]] \
       && curl --fail --silent --max-time 5 \
          -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
          http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null \
       && curl --fail --silent --max-time 5 \
          -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
          http://127.0.0.1:8088/readyz | jq -e '.ok==true and .postgres==true' >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

restore_runtime() {
  if [[ "$brains_quiesced" -eq 1 ]]; then
    if [[ "$brains_state_before" == active ]]; then
      sudo -n systemctl start brains.service
    else
      sudo -n systemctl stop brains.service
    fi
    [[ "$(systemctl is-active brains.service)" == "$brains_state_before" ]]
    brains_quiesced=0
  fi
  if [[ "$timers_quiesced" -eq 1 ]]; then
    while IFS=$'\t' read -r unit enabled active; do
      [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
      [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
      if [[ "$active" == active ]]; then
        sudo -n systemctl start "$unit"
      else
        sudo -n systemctl stop "$unit"
      fi
      [[ "$(systemctl is-active "$unit")" == "$active" ]]
    done <"$timer_state"
    timers_quiesced=0
  fi
}

record_exit() {
  code=$?
  if [[ "$code" -eq 0 && "$phase" != complete ]]; then code=1; fi
  restore_runtime || code=1
  rm -f "$timer_state" "$table_list"
  if [[ -n "$status_file" ]]; then
    printf 'run_id=%s\nphase=%s\nexit_code=%s\ncompleted_at=%s\n' \
      "$run_id" "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$code"
}
trap record_exit EXIT

capture_non_outbox_state() {
  local output=$1 table state
  : >"$output"
  while IFS= read -r table; do
    state=$(psql_row "
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
  chmod 0600 "$output"
}

[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
head=$(git -C "$repo_root" rev-parse HEAD)
[[ -x "$python_bin" && -f "$repo_root/$runner" ]]
[[ -f "$apply_result" && -f "$apply_manifest" ]]
[[ "$(stat -c '%a' "$apply_result")" == 600 ]]
[[ "$(stat -c '%a' "$apply_manifest")" == 600 ]]
required_head=$(jq -er '.required_head_commit' "$apply_manifest")
[[ "$required_head" == ab5a83e9c3f62c835a82e1742b2b2d003c3ee258 ]]
[[ "$(jq -er '.claim_id' "$apply_result")" == "$claim_id" ]] 2>/dev/null || \
  [[ "$(jq -er '.outcomes[0].claim_id' "$apply_result")" == "$claim_id" ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$owner'::uuid AND claim_id='$claim_id'::uuid
    AND predicate='stance.reported' AND status='supported'
")" == 1 ]]
[[ "$(psql_row "
  SELECT count(*) FROM memory.projection_outbox
  WHERE owner_user_id='$owner'::uuid AND aggregate_id='$claim_id'::uuid
")" == 0 ]]
[[ "$(qdrant_claim_count)" == 0 ]]

set -a
source "$repo_root/.env"
set +a
[[ -n "${POSTGRES_DSN:-}" ]]
authenticated_health

exec 9>"$lock_file"
flock -n 9
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)_${head:0:12}"
artifact_dir="$review_root/deferred-projection-production-$run_id"
status_file="$snapshot_root/memory_v1_deferred_projection_admission_${run_id}.status"
mkdir -p "$artifact_dir"
chmod 0700 "$artifact_dir"

phase=quiesce
: >"$timer_state"
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
chmod 0600 "$timer_state"
while IFS=$'\t' read -r unit _enabled active; do
  [[ "$active" != active ]] || sudo -n systemctl stop "$unit"
done <"$timer_state"
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"
brains_state_before=$(systemctl is-active brains.service)
[[ "$brains_state_before" != active ]] || sudo -n systemctl stop brains.service
brains_quiesced=1
for _attempt in $(seq 1 30); do
  systemctl is-active --quiet brains.service || break
  sleep 1
done
! systemctl is-active --quiet brains.service

phase=backup
backup_partial="$snapshot_root/.memory_pre_deferred_projection_${run_id}.dump.partial"
backup="$snapshot_root/memory_pre_deferred_projection_${run_id}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -X -A -t -U sage -d "$database" -c "
  SELECT table_name
  FROM information_schema.tables
  WHERE table_schema='memory' AND table_type='BASE TABLE'
    AND table_name <> 'projection_outbox'
  ORDER BY table_name
" >"$table_list"
before="$artifact_dir/non-outbox-before.tsv"
after="$artifact_dir/non-outbox-after.tsv"
capture_non_outbox_state "$before"
qdrant_before=$(qdrant_signature)

preflight="$artifact_dir/preflight.json"
apply="$artifact_dir/apply.json"
replay="$artifact_dir/replay.json"
env_common=(
  "POSTGRES_DSN=$POSTGRES_DSN"
  "PYTHONPATH=$repo_root/scripts"
  "MEMORY_V1_REQUIRED_HEAD=$required_head"
)
phase=preflight
env "${env_common[@]}" "$python_bin" "$repo_root/$runner" \
  --mode preflight --apply-result "$apply_result" \
  --apply-manifest "$apply_manifest" --output "$preflight"
[[ "$(jq -er '.rows_written' "$preflight")" == 0 ]]

phase=apply
env "${env_common[@]}" MEMORY_V1_DEFERRED_PROJECTION_ADMISSION=authorized \
  "$python_bin" "$repo_root/$runner" \
  --mode apply --apply-result "$apply_result" \
  --apply-manifest "$apply_manifest" --output "$apply"
[[ "$(jq -er '.rows_written' "$apply")" == 1 ]]

phase=replay
env "${env_common[@]}" "$python_bin" "$repo_root/$runner" \
  --mode replay --apply-result "$apply_result" \
  --apply-manifest "$apply_manifest" --prior-result "$apply" \
  --output "$replay"
[[ "$(jq -er '.rows_written' "$replay")" == 0 ]]

phase=verify
outbox_id=$(jq -er '.outcomes[0].outbox_id' "$apply")
[[ "$(psql_row "
  SELECT count(*) FROM memory.projection_outbox
  WHERE owner_user_id='$owner'::uuid
    AND outbox_id='$outbox_id'::uuid
    AND aggregate_id='$claim_id'::uuid
    AND status='pending' AND attempts=0
    AND payload=jsonb_build_object(
      'claim_id','$claim_id','revision_number',2
    )
")" == 1 ]]
cross_owner_count=$(
  psql "$POSTGRES_DSN" -X -A -t -v ON_ERROR_STOP=1 \
    -v actor="$other_owner" -v claim="$claim_id" <<'SQL'
BEGIN READ ONLY;
SELECT set_config('app.user_id', :'actor', true);
SELECT count(*)
FROM memory.projection_outbox
WHERE aggregate_id=:'claim'::uuid;
ROLLBACK;
SQL
)
cross_owner_count=$(printf '%s\n' "$cross_owner_count" | awk '/^[0-9]+$/{print;exit}')
[[ "$cross_owner_count" == 0 ]]
capture_non_outbox_state "$after"
cmp -s "$before" "$after"
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]
[[ "$(qdrant_claim_count)" == 0 ]]

phase=restore_runtime
restore_runtime
authenticated_health

phase=report
report="$artifact_dir/report.json"
jq -n \
  --arg head "$head" --arg backup "$backup" \
  --arg claim_id "$claim_id" --arg outbox_id "$outbox_id" \
  --arg qdrant_sha256 "$qdrant_after" \
  --arg result_sha256 "$(jq -er '.result_sha256' "$apply")" \
  '{
    contract_version:"memory_v1_v5_deferred_projection_admission_production_report_v1",
    head_commit:$head,
    owner_user_id:"1240822d-ac9a-4096-95aa-e2b24d36ef50",
    backup:$backup,
    claim_id:$claim_id,
    outbox_id:$outbox_id,
    preflight_rows:0,
    apply_rows:1,
    replay_rows:0,
    admission_result_sha256:$result_sha256,
    cross_owner_visible_rows:0,
    non_outbox_memory_unchanged:true,
    qdrant_sha256:$qdrant_sha256,
    qdrant_unchanged:true,
    target_claim_points:0,
    timers_restored_exactly:true,
    brains_service_restored:true,
    external_model_calls:0,
    retrieval_activated:false,
    prompt_influence_activated:false
  }' >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'backup=%s\nreport=%s\nadmission_result=%s\n' "$backup" "$report" "$apply"
printf 'report_sha256=%s\n' "$(awk '{print $1}' "$report.sha256")"
printf 'memory_v1_v5_deferred_projection_admission_production_apply: PASS\n'
