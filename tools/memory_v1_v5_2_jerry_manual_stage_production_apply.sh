#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Deploys the clone-tested manual atom-selection bridge and
# persists only the reviewed Jerry entity mention, o02/o03 observations, and
# Jerry entity-resolution review. Stops before entity application, observation
# binding, claims, projection, retrieval, or prompt influence.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for deployment and backup' >&2
  exit 1
fi
if [[ "${MEMORY_V1_V5_2_JERRY_MANUAL_STAGE_PRODUCTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_JERRY_MANUAL_STAGE_PRODUCTION=authorized is required' >&2
  exit 1
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(git -C "$script_dir/.." rev-parse --show-toplevel)
cd "$repo_root"

production_root=/opt/chat-memory
required_production=b8fe8c71097baa3c24d47e818d157db28fd0dd1c
target_head=$(git rev-parse HEAD)
container=brains-postgres-1
database=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=c4db1405-ad9d-5c9a-81e3-10ad0200b0ca
evidence=681ab38d-a742-463c-ad26-c74c65eacaa9
prior_role_resolution=fa0e7209-67c4-4cf3-b808-e8d8c2dc35ef
selection_id=6cfb3ef5-7c85-4035-92ae-8b5c8d71ed14
selection_operation_id=a43cbe80-b1d5-4be4-940f-7d9ebed02960
packet_sha=90baad563bd05cdaf93f9d9b72ec5593e86592e4e0f334143a287e5f5e76dc5d
baseline_sha=bfed594b759d942701b51c9275d0d8e7ab6b4c6e529c09af8b6fdb4f19ebd9af
review_sha=f412bb26c8cefea3dd1b100c6a2d6eec7654d637fb4fdda5763263727fd50748
snapshot_root=/home/ubuntu/brains/snapshots
review_root=/home/ubuntu/memory-v1-reviews
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_jerry_manual_stage.lock
env_file=/opt/chat-memory/.env
migration=ops/sql/20260731_memory_v1_v5_2_manual_atom_selection_v1.sql
rollback=ops/sql/20260731_memory_v1_v5_2_manual_atom_selection_v1_rollback.sql
security_test=tests/memory_v1_v5_2_manual_atom_selection_v1.sql
atom_spec=manifests/memory_v1_v5_2_jerry_manual_atom_admission_spec_20260731.json
entity_spec=manifests/memory_v1_v5_2_jerry_entity_review_spec_20260731.json
clone_wrapper=tools/memory_v1_v5_2_jerry_manual_stage_clone.sh
atom_manifest_runner=scripts/memory_v1_v5_2_atom_admission_manifest_batch.py
atom_apply_runner=scripts/memory_v1_v5_2_atom_admission_apply_v2.py
bundle_builder=scripts/memory_v1_v5_2_atom_stage_bundle_v2.py
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
stage_fixture=tests/memory_v1_v5_2_stage_batch_fixture.py
entity_review_runner=scripts/memory_v1_v5_2_entity_resolution_review_batch.py

declare -A expected_sha256=(
  ["$migration"]="5727e24df98925881d029bdd3f02f634d790970b88f38836d78e6b6cb6eeb9c8"
  ["$rollback"]="f1d3d1602feb43143586a1bd2ba233bf379b2162c77696c54ab96b9c1a2e98eb"
  ["$security_test"]="f6396ca64d9c5a6e345a9a0649804557d1113d598c47fc6ee4202d8d9dec05e6"
  ["$atom_spec"]="5a9d1aab1d7671250603009310bf14681afbeffb9f92da4ddcb211d0d9c16f88"
  ["$entity_spec"]="8371a9e3765dcee0ccffe57ee3fc2659eb668a5cb65898ffb3f75f8789322ee8"
  ["$clone_wrapper"]="a88c375bf098285164952e99d1f1a7e5933a1659df07b9e04ee53da67ac1c7f0"
  ["$atom_manifest_runner"]="ab16b4ac196d12c6b3928a753e8d85385ec974e6fe396bb764d1c49a00c7a74d"
  ["$atom_apply_runner"]="721184f8eebf5f4d14b553eb0cf0134960456d8834029263da1d10963868c5e7"
  ["$bundle_builder"]="5a611180ecbb3fa3b9220459ef870879e74a8d487847ef76084b379c32ac30bb"
  ["$stage_runner"]="3dddef3dbf71862fc42252d6263075ad8d407bc292a4f06760866c816c3699b9"
  ["$stage_fixture"]="96461968b5afa0ec2a8aa207ec7ec2096278d851cd51d4e0a7c90541ab5a54ac"
  ["$entity_review_runner"]="9cf93805b89a8ab3b898a4659b58c941798ba2c07e6847b7ac1efe717d4e93cd"
)

phase=initialization
run_tag=
status_file=
timer_state=
brains_state=
timers_quiesced=0
brains_quiesced=0
schema_installed=0
durable_rows_present=0

scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | tr -d '[:space:]'
}

row() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1" | sed -n '1p'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

authenticated_health() {
  set -a
  source "$env_file"
  set +a
  [[ -n "${VS_SERVICE_TOKEN:-}" ]]
  [[ "$(systemctl is-active brains.service)" == active ]]
  docker exec "$container" pg_isready -U sage -d "$database" >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/healthz | jq -e '.status=="ok"' >/dev/null
  curl --fail --silent --show-error --max-time 10 \
    -H "x-vs-service-token: $VS_SERVICE_TOKEN" \
    http://127.0.0.1:8088/readyz \
    | jq -e '.ok==true and .postgres==true' >/dev/null
}

restore_services() {
  if [[ "$brains_quiesced" -eq 1 ]]; then
    if [[ "$brains_state" == active ]]; then
      systemctl start brains.service
    else
      [[ "$brains_state" == inactive ]]
      systemctl stop brains.service
    fi
    [[ "$(systemctl is-active brains.service)" == "$brains_state" ]]
    brains_quiesced=0
  fi
  if [[ "$timers_quiesced" -eq 1 ]]; then
    while IFS=$'\t' read -r unit enabled active; do
      [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
      [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
      if [[ "$active" == active ]]; then
        systemctl start "$unit"
      else
        [[ "$active" == inactive ]]
        systemctl stop "$unit"
      fi
      [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
      [[ "$(systemctl is-active "$unit")" == "$active" ]]
    done <"$timer_state"
    timers_quiesced=0
  fi
}

record_exit() {
  exit_code=$?
  trap - EXIT
  set +e
  if [[ "$schema_installed" -eq 1 && "$durable_rows_present" -eq 0 ]]; then
    docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
      -U sage -d "$database" <"$rollback" >/dev/null 2>&1 || exit_code=1
  fi
  restore_services || exit_code=1
  if [[ -n "$status_file" ]]; then
    {
      printf 'run_tag=%s\n' "$run_tag"
      printf 'phase=%s\n' "$phase"
      printf 'exit_code=%s\n' "$exit_code"
      printf 'target_head=%s\n' "$target_head"
      printf 'production_head=%s\n' "$(git -C "$production_root" rev-parse HEAD)"
      printf 'schema_installed=%s\n' "$schema_installed"
      printf 'durable_rows_present=%s\n' "$durable_rows_present"
      printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$exit_code"
}
trap record_exit EXIT

capture_partition() {
  local partition=$1 output=$2 table predicate state
  : >"$output"
  while IFS= read -r table; do
    [[ "$table" =~ ^[a-z][a-z0-9_]*$ ]]
    if [[ "$partition" == target ]]; then
      predicate="owner_user_id='$owner'::uuid"
    else
      predicate="owner_user_id<>'$owner'::uuid"
    fi
    state=$(row "
      SELECT count(*)::text || E'\\t' || encode(public.digest(convert_to(
        coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT to_jsonb(value)::text AS row_json
        FROM memory.\"$table\" AS value
        WHERE $predicate
      ) AS rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done <"$table_list"
  chmod 0600 "$output"
}

verify_target_delta() {
  BEFORE="$1" AFTER="$2" python3 - <<'PY'
import os
from pathlib import Path

def load(path):
    rows = {}
    for line in Path(path).read_text().splitlines():
        table, count, digest = line.split("\t")
        rows[table] = (int(count), digest)
    return rows

before = load(os.environ["BEFORE"])
after = load(os.environ["AFTER"])
if before.keys() != after.keys():
    raise SystemExit("owner-scoped table set changed")
expected = {
    "v5_2_manual_atom_selection_v1": 1,
    "v5_2_atom_admission_proposal": 1,
    "v5_2_atom_admission_review": 1,
    "v5_2_atom_admission_apply": 1,
    "v5_2_atom_admission_operation": 3,
    "relational_stage_batch": 1,
    "entity_mention": 1,
    "entity_resolution_plan": 1,
    "observation": 2,
    "observation_temporal": 2,
    "relational_operation_request": 2,
    "entity_resolution_review": 1,
}
for table in before:
    delta = after[table][0] - before[table][0]
    wanted = expected.get(table, 0)
    if delta != wanted:
        raise SystemExit(f"unexpected target-owner delta {table}: {delta} != {wanted}")
    if wanted == 0 and before[table][1] != after[table][1]:
        raise SystemExit(f"unexpected target-owner mutation {table}")
if sum(expected.values()) != 17:
    raise SystemExit("expected durable-row budget drifted")
PY
}

phase=source_preflight
[[ -z "$(git status --porcelain)" ]]
[[ -z "$(git -C "$production_root" status --porcelain)" ]]
[[ "$(git -C "$production_root" rev-parse HEAD)" == "$required_production" ]]
git merge-base --is-ancestor "$required_production" "$target_head"
for artifact in "${!expected_sha256[@]}"; do
  [[ -f "$artifact" ]]
  [[ "$(sha256sum "$artifact" | awk '{print $1}')" == \
    "${expected_sha256[$artifact]}" ]]
done
authenticated_health
[[ "$(scalar "SELECT count(*) FROM memory.v5_2_atom_admission_proposal WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.relational_stage_batch WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid")" == 0 ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_root/memory_v1_v5_2_jerry_manual_stage_${run_tag}.status"
timer_state="$snapshot_root/memory_v1_v5_2_jerry_manual_stage_timers_${run_tag}.tsv"
table_list="$snapshot_root/memory_v1_v5_2_jerry_manual_stage_tables_${run_tag}.tsv"
target_before="$snapshot_root/memory_v1_v5_2_jerry_manual_stage_target_before_${run_tag}.tsv"
target_after="$snapshot_root/memory_v1_v5_2_jerry_manual_stage_target_after_${run_tag}.tsv"
other_before="$snapshot_root/memory_v1_v5_2_jerry_manual_stage_other_before_${run_tag}.tsv"
other_after="$snapshot_root/memory_v1_v5_2_jerry_manual_stage_other_after_${run_tag}.tsv"
work="$review_root/jerry-manual-stage-production-$run_tag"
install -d -o ubuntu -g ubuntu -m 0700 "$work" "$work/stage"

phase=capture_service_state
mapfile -t timers < <(
  systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
    | awk '{print $1}' | sort -u
)
[[ ${#timers[@]} -gt 0 ]]
: >"$timer_state"
for unit in "${timers[@]}"; do
  [[ "$unit" =~ ^memory-v1-[a-z0-9-]+\.timer$ ]]
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit")" "$(systemctl is-active "$unit")" \
    >>"$timer_state"
done
chmod 0600 "$timer_state"
brains_state=$(systemctl is-active brains.service)
[[ "$brains_state" == active || "$brains_state" == inactive ]]

phase=quiesce_services
for unit in "${timers[@]}"; do systemctl stop "$unit"; done
timers_quiesced=1
for unit in "${timers[@]}"; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done
systemctl stop brains.service
brains_quiesced=1
[[ "$(systemctl is-active brains.service)" == inactive ]]

phase=disposable_clone_verification
clone_output=$(mktemp /tmp/memory-v5-2-jerry-manual-production-clone.XXXXXX)
bash "$clone_wrapper" >"$clone_output"
grep -qx 'memory_v1_v5_2_jerry_manual_stage_clone: PASS' "$clone_output"
rm -f "$clone_output"

phase=fresh_backup
database_size=$(scalar 'SELECT pg_database_size(current_database())')
free_bytes=$(df --output=avail -B1 "$snapshot_root" | tail -1 | tr -d '[:space:]')
(( free_bytes >= database_size * 2 ))
backup_partial="$snapshot_root/.memory_pre_v5_2_jerry_manual_stage_${run_tag}.dump.partial"
backup="$snapshot_root/memory_pre_v5_2_jerry_manual_stage_${run_tag}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$backup_partial"
[[ -s "$backup_partial" ]]
docker exec -i "$container" pg_restore -l <"$backup_partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$backup_partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=deploy_code
git -C "$production_root" merge --ff-only "$target_head"
[[ "$(git -C "$production_root" rev-parse HEAD)" == "$target_head" ]]
[[ -z "$(git -C "$production_root" status --porcelain)" ]]

phase=install_schema
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$database" <"$migration" >/dev/null
schema_installed=1

phase=rollback_only_security
set -a
source "$env_file"
set +a
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 -f "$security_test" >/dev/null

phase=capture_baseline
docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
  -U sage -d "$database" -c "
    SELECT table_name
    FROM information_schema.columns
    WHERE table_schema='memory' AND column_name='owner_user_id'
    GROUP BY table_name ORDER BY table_name" >"$table_list"
[[ -s "$table_list" ]]
capture_partition target "$target_before"
capture_partition other "$other_before"
qdrant_before=$(qdrant_signature)
claim_before=$(scalar 'SELECT count(*) FROM memory.claim')
entity_before=$(scalar 'SELECT count(*) FROM memory.entity')
binding_before=$(scalar 'SELECT count(*) FROM memory.observation_entity_binding')
answer_binding_before=$(scalar 'SELECT count(*) FROM memory.final_answer_memory_binding_v1')

phase=manual_selection
selection_manifest=$(psql "$POSTGRES_DSN" -X -q -A -t -v ON_ERROR_STOP=1 -c "
  SELECT memory.v5_2_manual_atom_selection_manifest_sha_v1(
    '$owner'::uuid,'$selection_id'::uuid,'$packet'::uuid,'$evidence'::uuid,
    '$packet_sha','$baseline_sha','$review_sha',
    '[\"e00\"]'::jsonb,'[\"o02\",\"o03\"]'::jsonb)" | tr -d '[:space:]')
psql "$POSTGRES_DSN" -X -v ON_ERROR_STOP=1 <<SQL >/dev/null
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SELECT * FROM memory.register_owner_v5_2_manual_atom_selection_v1(
  '$selection_id'::uuid,'$selection_operation_id'::uuid,'$packet'::uuid,
  '$packet_sha','$baseline_sha','$review_sha',
  '["e00"]'::jsonb,'["o02","o03"]'::jsonb,'$selection_manifest');
COMMIT;
SQL
durable_rows_present=1

phase=atom_admission
runuser -u ubuntu -- env POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$atom_manifest_runner" --spec "$repo_root/$atom_spec" \
  --output "$work/atom-manifest.json" >/dev/null
runuser -u ubuntu -- env POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$atom_apply_runner" --mode preflight \
  --manifest "$work/atom-manifest.json" --output "$work/atom-preflight.json" >/dev/null
runuser -u ubuntu -- env MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY_V2=authorized \
  POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$repo_root/$atom_apply_runner" --mode apply \
  --manifest "$work/atom-manifest.json" --output "$work/atom-apply.json" >/dev/null
runuser -u ubuntu -- env POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$atom_apply_runner" --mode replay \
  --manifest "$work/atom-manifest.json" --output "$work/atom-replay.json" >/dev/null
jq -e '.persistent_writes==6' "$work/atom-apply.json" >/dev/null
jq -e '.persistent_writes==0' "$work/atom-replay.json" >/dev/null

apply_id=$(jq -r '.items[0].apply_id' "$work/atom-manifest.json")
proposal_id=$(jq -r '.items[0].proposal_id' "$work/atom-manifest.json")
review_id=$(jq -r '.items[0].review_id' "$work/atom-manifest.json")

phase=relational_stage
stage_plan=$(psql "$POSTGRES_DSN" -X -q -A -t -v ON_ERROR_STOP=1 -c "
  BEGIN; SET LOCAL app.user_id='$owner';
  SELECT memory.plan_owner_v5_2_atom_stage_v2('$apply_id'::uuid); COMMIT;")
jq -n --arg owner "$owner" --arg packet "$packet" --arg evidence "$evidence" \
  --arg apply "$apply_id" --arg proposal "$proposal_id" --arg review "$review_id" \
  --argjson plan "$stage_plan" '
  {
    contract_version:"memory_v1_v5_2_atom_stage_build_manifest_v2",
    target_server:"seebx",owner_user_id:$owner,
    case_id:"compiler_v11_jerry_restricted_health_stage",
    apply_id:$apply,proposal_id:$proposal,review_id:$review,
    packet_id:$packet,evidence_id:$evidence,
    apply_manifest_sha256:$plan.apply_manifest_sha256,
    proposal_sha256:$plan.proposal_sha256,
    stage_projection_sha256:$plan.stage_projection_sha256,
    expected_counts:$plan.counts,
    expected_resolutions:{e00:{action:"create_new",
      decision_state:"manual_review_required",selected_entity_id:null,
      proposed_entity:{entity_type:"person",display_label:"Jerry",
        canonical_name:"Jerry",identity_state:"named",
        creation_reason:"new_named_entity_no_exact_owner_match"},
      review_reason_codes:["new_named_entity_requires_review"]}}
  }' >"$work/stage-build-manifest.json"
chmod 0600 "$work/stage-build-manifest.json"
chown ubuntu:ubuntu "$work/stage-build-manifest.json"
runuser -u ubuntu -- env POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python "$repo_root/$bundle_builder" \
  build --manifest "$work/stage-build-manifest.json" --output-root "$work/stage" >/dev/null
runuser -u ubuntu -- env POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python "$repo_root/$bundle_builder" \
  probe --bundle "$work/stage/bundle.json" --apply-id "$apply_id" \
  --other-owner-user-id "$other" >/dev/null
runuser -u ubuntu -- env POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" \
  plan --manifest "$work/stage/stage-manifest.json" --review-root "$work" \
  --output "$work/stage-plan.json" >/dev/null
runuser -u ubuntu -- /opt/chat-memory/venv/bin/python "$repo_root/$stage_fixture" \
  authorize --plan "$work/stage-plan.json" --output "$work/stage-authorization.json" \
  --head "$target_head" >/dev/null
runuser -u ubuntu -- env MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized \
  POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" apply \
  --plan "$work/stage-plan.json" --authorization "$work/stage-authorization.json" \
  --review-root "$work" --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
  --output "$work/stage-apply.json" >/dev/null
jq -e '.database_rows_created==8 and .checks.replay_rows_written==0' \
  "$work/stage-apply.json" >/dev/null

phase=entity_resolution_review
runuser -u ubuntu -- env POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python "$repo_root/$entity_review_runner" \
  manifest --spec "$repo_root/$entity_spec" --output "$work/entity-manifest.json" >/dev/null
runuser -u ubuntu -- env POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python "$repo_root/$entity_review_runner" \
  plan --manifest "$work/entity-manifest.json" --review-root "$work" \
  --output "$work/entity-plan.json" >/dev/null
entity_plan_sha=$(sha256sum "$work/entity-plan.json" | awk '{print $1}')
authorized_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
expires_at=$(date -u -d '+20 minutes' +%Y-%m-%dT%H:%M:%SZ)
jq -n --arg authorization_id "a550e170-92bd-4a51-962e-9054c0d0eaff" \
  --arg authorized_at "$authorized_at" --arg expires_at "$expires_at" \
  --arg head "$target_head" --arg owner "$owner" --arg plan_sha "$entity_plan_sha" '
  {contract_version:"memory_v1_v5_2_entity_review_authorization_v1",
   authorization_id:$authorization_id,authorized:true,authorized_by:"Eric Lund",
   authorized_at:$authorized_at,expires_at:$expires_at,expected_head_commit:$head,
   target_server:"seebx",scope:"review_owner_v5_2_entity_resolutions_without_apply",
   owner_user_id:$owner,plan_sha256:$plan_sha,expected_item_count:1,
   expected_new_rows:2,confirmation:"REVIEW_OWNER_V5_2_ENTITY_RESOLUTIONS_WITHOUT_APPLY"}' \
  >"$work/entity-authorization.json"
chmod 0600 "$work/entity-authorization.json"
chown ubuntu:ubuntu "$work/entity-authorization.json"
runuser -u ubuntu -- env MEMORY_V1_V5_2_ENTITY_REVIEW_ONLY_APPLY=authorized \
  POSTGRES_DSN="$POSTGRES_DSN" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$repo_root/$entity_review_runner" apply \
  --plan "$work/entity-plan.json" --authorization "$work/entity-authorization.json" \
  --review-root "$work" --confirm REVIEW_OWNER_V5_2_ENTITY_RESOLUTIONS_WITHOUT_APPLY \
  --output "$work/entity-apply.json" >/dev/null
jq -e '.new_rows==2 and .zero_write_replay==true and .entity_apply_calls==0' \
  "$work/entity-apply.json" >/dev/null

phase=verification
[[ "$(scalar "
  SELECT concat_ws(',',
    (SELECT count(*) FROM memory.v5_2_manual_atom_selection_v1 WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid),
    (SELECT count(*) FROM memory.relational_stage_batch WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_mention WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_resolution_plan WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_resolution_candidate c JOIN memory.entity_resolution_plan p USING(owner_user_id,resolution_id) WHERE p.owner_user_id='$owner'::uuid AND p.evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.observation WHERE owner_user_id='$owner'::uuid AND evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.observation_temporal t JOIN memory.observation o USING(owner_user_id,observation_id) WHERE o.owner_user_id='$owner'::uuid AND o.evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_resolution_review r JOIN memory.entity_resolution_plan p USING(owner_user_id,resolution_id) WHERE p.owner_user_id='$owner'::uuid AND p.evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.entity_resolution_apply a JOIN memory.entity_resolution_plan p USING(owner_user_id,resolution_id) WHERE p.owner_user_id='$owner'::uuid AND p.evidence_id='$evidence'::uuid),
    (SELECT count(*) FROM memory.observation_entity_binding b JOIN memory.observation o USING(owner_user_id,observation_id) WHERE o.owner_user_id='$owner'::uuid AND o.evidence_id='$evidence'::uuid))")" == '1,1,1,1,0,2,2,1,0,0' ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity_resolution_plan WHERE owner_user_id='$owner'::uuid AND resolution_id='$prior_role_resolution'::uuid AND action='defer' AND decision_state='deferred'")" == 1 ]]
[[ "$(scalar "SELECT count(*) FROM memory.entity WHERE owner_user_id='$owner'::uuid AND lower(coalesce(canonical_name,''))='jerry'")" == 0 ]]
[[ "$(scalar 'SELECT count(*) FROM memory.claim')" == "$claim_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.entity')" == "$entity_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.observation_entity_binding')" == "$binding_before" ]]
[[ "$(scalar 'SELECT count(*) FROM memory.final_answer_memory_binding_v1')" == "$answer_binding_before" ]]
capture_partition target "$target_after"
capture_partition other "$other_after"
verify_target_delta "$target_before" "$target_after"
cmp -s "$other_before" "$other_after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

phase=cross_owner_isolation
psql "$POSTGRES_DSN" -X -A -t -q -v ON_ERROR_STOP=1 <<SQL >/dev/null
BEGIN READ ONLY;
SELECT set_config('app.user_id','$other',true);
DO \$probe\$
BEGIN
  BEGIN
    PERFORM memory.plan_owner_v5_2_atom_admission_v2('$packet'::uuid);
    RAISE EXCEPTION 'cross-owner Jerry atom plan unexpectedly resolved';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN NULL;
  END;
END
\$probe\$;
ROLLBACK;
SQL

phase=restore_services
restore_services
authenticated_health

phase=report
report="$snapshot_root/memory_v1_v5_2_jerry_manual_stage_${run_tag}.json"
jq -n --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg head "$target_head" --arg backup "$backup" \
  --arg backup_sha "$(sha256sum "$backup" | awk '{print $1}')" \
  --arg migration_sha "${expected_sha256[$migration]}" \
  --arg rollback_sha "${expected_sha256[$rollback]}" \
  --arg qdrant_sha "$qdrant_before" '
  {contract_version:"memory_v1_v5_2_jerry_manual_stage_production_report_v1",
   completed_at:$completed_at,head_commit:$head,
   backup:{path:$backup,sha256:$backup_sha},
   artifacts:{migration_sha256:$migration_sha,rollback_sha256:$rollback_sha},
   results:{manual_selection_rows:1,atom_admission_rows:6,
     relational_stage_rows:8,entity_review_rows:2,total_rows:17,
     staged_entity_mentions:1,staged_observations:2,entity_apply_rows:0,
     observation_binding_rows:0,claim_rows:0,qdrant_writes:0,model_calls:0,
     zero_write_replay:true,cross_owner_isolation:true,
     non_target_unchanged:true,timers_restored:true,brains_restored:true,
     retrieval_or_prompt_influence:false},qdrant_sha256:$qdrant_sha,
   hard_stop:"before_entity_apply_observation_binding_claims_projection_or_retrieval"}' \
  >"$report"
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf '%s\n' \
  'memory_v1_v5_2_jerry_manual_stage_production_apply: PASS' \
  "head=$target_head" "backup=$backup" "report=$report" \
  'rows=1,6,8,2 total=17' \
  'entity_apply=0 bindings=0 claims=0 qdrant=0 model_calls=0 prompt_influence=0' \
  'cross_owner_isolation=PASS replay=PASS timers=RESTORED brains=ACTIVE'
