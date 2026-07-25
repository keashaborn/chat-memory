#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the backward-compatible compiler-v8 code and
# two dormant restricted functions. It writes no jobs, packets, claims,
# projections, Qdrant points, retrieval state, or prompt state.

if [[ "${MEMORY_V1_V5_2_SEMANTIC_COMPILER_V8_INSTALL:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_SEMANTIC_COMPILER_V8_INSTALL=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
live=/opt/chat-memory
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_v5_2_semantic_compiler_v8_install.lock
expected_live_head=500c390d3162d99bec13ddf5e47a68fd4ba72a39
required_source_commit=07709b622bcf3340830531b7a7be001db57bce72
selector=20260725_v5_2_semantic_compiler_v8_reextract_v1
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
manifest_sha=98e36ec66a5136a5bea4c3438be11acda18ceda2865b393cdb58b6bb3def337f
compiler_sha=f82e6f4339dfe4aada7e5c3edb71fde8125a819f33677b3f47b98f3726b60419
persistence_migration=ops/sql/20260725_memory_v1_v5_2_compiler_v8_persistence.sql
persistence_rollback=ops/sql/20260725_memory_v1_v5_2_compiler_v8_persistence_rollback.sql
persistence_test=tests/memory_v1_v5_2_compiler_v8_persistence.sql
persistence_clone=tools/memory_v1_v5_2_compiler_v8_persistence_clone.sh
reextract_migration=ops/sql/20260725_memory_v1_v5_2_semantic_compiler_v8_reextract.sql
reextract_rollback=ops/sql/20260725_memory_v1_v5_2_semantic_compiler_v8_reextract_rollback.sql
reextract_test=tests/memory_v1_v5_2_semantic_compiler_v8_reextract.sql
reextract_clone=tools/memory_v1_v5_2_semantic_compiler_v8_reextract_clone.sh
manifest=manifests/memory_v1_v5_2_semantic_compiler_v8_reextract_20260725.json
semantic_report=/home/ubuntu/memory-v1-reviews/semantic-compiler-v8-clone-07709b6.json

declare -A expected_sha256=(
  [scripts/memory_v1_relational_extraction_v5_local_provider.py]=f5af3a1795f92a097a715c30aa04206077e0e913bfe2122cf29b1ca4fc4f732d
  [scripts/memory_v1_v5_2_semantic_compiler_v8_clone_verify.py]=7b8ff235530309314b0864c709fb44043594beea3699e12c653306fafde94a77
  [tools/memory_v1_v5_2_semantic_compiler_v8_production_clone.sh]=c7a0a81a24c59df9fcb81ff0c41b6e7addfbea5e346a58da57442faaf3044a8a
  ["$persistence_migration"]=324aef4fc724db6c0d49aa2e3d52b7a4f535679d2a64ee4e712cdf083d0a880b
  ["$persistence_rollback"]=595e6183c21dc561fb612a292e61faebded16aea1163d7f5a849dc25f9994355
  ["$persistence_test"]=fb824d0d03353e68e96294bf5c1f1d538f686309cc7c20fd4f41b690b89f3945
  ["$persistence_clone"]=95f1ace49f0ed0254a4e22ac35b7a4baf8f1006dba1898e577813f223c377fbd
  ["$reextract_migration"]=26d9361821cfab9f64f463d119eebd7bd978d65b4ec6048834eb19bcb7a0a14f
  ["$reextract_rollback"]=0f60b5d09b8080575cb0f3f67001f3aff9baf7049f4db23f0cc567103658d60f
  ["$reextract_test"]=19cd58dc11ac6ba6c047ecbf9988973548153e29364ffe91f599949e2b275514
  ["$reextract_clone"]=e4598d63048e93037f7d5642223fece0da8dda2bcd6aaec710e68ba398c46b44
  ["$manifest"]=2beb49a41a930172a65e8457f25628ce8d2aaa0196805563d4237449aeb26e9b
)

timer_state=$(mktemp /tmp/memory-v5-2-compiler-v8-install-timers.XXXXXX)
table_list=$(mktemp /tmp/memory-v5-2-compiler-v8-install-tables.XXXXXX)
before=$(mktemp /tmp/memory-v5-2-compiler-v8-install-before.XXXXXX)
after=$(mktemp /tmp/memory-v5-2-compiler-v8-install-after.XXXXXX)
clone_output=$(mktemp /tmp/memory-v5-2-compiler-v8-install-clone.XXXXXX)
chmod 0600 "$timer_state" "$table_list" "$before" "$after" "$clone_output"
timers_quiesced=0
persistence_installed=0
reextract_installed=0
code_deployed=0
phase=initialization
status_file=

scalar() {
  docker exec "$container" psql -U sage -d "$database" -X -At \
    -v ON_ERROR_STOP=1 -c "$1" | tr -d '[:space:]'
}

run_sql() {
  docker exec -i "$container" psql -U sage -d "$database" -X \
    -v ON_ERROR_STOP=1 "$@"
}

restore_timers() {
  [[ "$timers_quiesced" -eq 1 ]] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      sudo -n systemctl enable "$unit" >/dev/null
    else
      sudo -n systemctl disable "$unit" >/dev/null
    fi
    if [[ "$active" == active ]]; then
      sudo -n systemctl start "$unit"
    else
      sudo -n systemctl stop "$unit"
    fi
    [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
    [[ "$(systemctl is-active "$unit")" == "$active" ]]
  done <"$timer_state"
  timers_quiesced=0
}

record_exit() {
  rc=$?
  trap - EXIT
  if [[ "$code_deployed" -eq 0 ]]; then
    if [[ "$reextract_installed" -eq 1 ]]; then
      run_sql <"$reextract_rollback" >/dev/null 2>&1 || rc=1
    fi
    if [[ "$persistence_installed" -eq 1 ]]; then
      run_sql <"$persistence_rollback" >/dev/null 2>&1 || rc=1
    fi
  fi
  if [[ "$timers_quiesced" -eq 1 ]]; then
    if [[ "$(systemctl is-active brains.service)" == active ]]; then
      restore_timers || rc=1
    else
      rc=1
    fi
  fi
  rm -f "$timer_state" "$table_list" "$before" "$after" "$clone_output"
  if [[ -n "$status_file" ]]; then
    printf 'phase=%s\nexit_code=%s\ncode_deployed=%s\ncompleted_at=%s\n' \
      "$phase" "$rc" "$code_deployed" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      >"$status_file"
    chmod 0600 "$status_file"
  fi
  exit "$rc"
}
trap record_exit EXIT

capture_state() {
  local output=$1 schema table state
  : >"$output"
  while IFS=$'\t' read -r schema table; do
    state=$(scalar "SELECT count(*)::text || E'\\t' ||
      encode(public.digest(convert_to(coalesce(string_agg(row_json,E'\\n'
        ORDER BY row_json),''),'UTF8'),'sha256'),'hex')
      FROM (SELECT to_jsonb(value)::text AS row_json
        FROM \"$schema\".\"$table\" AS value) rows")
    printf '%s\t%s\t%s\n' "$schema" "$table" "$state" >>"$output"
  done <"$table_list"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for artifact in "${!expected_sha256[@]}"; do
  [[ -f "$artifact" ]]
  [[ "$(sha256sum "$artifact" | awk '{print $1}')" == \
    "${expected_sha256[$artifact]}" ]]
done
[[ -f "$semantic_report" ]]
[[ "$(sha256sum "$semantic_report" | awk '{print $1}')" == \
  8a6e28bdb0439bfe36ebf76f3c717a6ed8d77ed3ea3f9134cba81e1ac7943214 ]]
jq -e '
  .passed==true and
  .compiler_version=="memory_v1_semantic_policy_compiler_v8" and
  .owner_isolation_visible_rows==0 and
  .external_model_calls==0 and
  .local_model_calls==1 and
  .effects.production_database_writes==0 and
  .effects.qdrant_writes==0 and
  .effects.prompt_influence==0
' "$semantic_report" >/dev/null
[[ -z "$(git status --porcelain)" ]]
git merge-base --is-ancestor "$required_source_commit" HEAD
target_commit=$(git rev-parse HEAD)
[[ "$(git -C "$live" rev-parse HEAD)" == "$expected_live_head" ]]
[[ -z "$(git -C "$live" status --porcelain)" ]]
git -C "$live" merge-base --is-ancestor "$expected_live_head" "$target_commit"
[[ "$(jq -cS . "$manifest" | tr -d '\n' | sha256sum | awk '{print $1}')" == \
  "$manifest_sha" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

phase=production_clone
bash "$persistence_clone" >"$clone_output"
[[ "$(tr -d '\r\n' <"$clone_output")" == \
  memory_v1_v5_2_compiler_v8_persistence_clone:\ PASS ]]
: >"$clone_output"
bash "$reextract_clone" >"$clone_output"
[[ "$(tr -d '\r\n' <"$clone_output")" == \
  memory_v1_v5_2_semantic_compiler_v8_reextract_clone:\ PASS ]]

exec 9>"$lock_file"
flock -n 9
umask 077
run_tag="$(date -u +%Y%m%dT%H%M%SZ)_$(git rev-parse --short=12 HEAD)"
status_file="$snapshot_dir/memory_v1_v5_2_semantic_compiler_v8_install_${run_tag}.status"
report="$snapshot_dir/memory_v1_v5_2_semantic_compiler_v8_install_${run_tag}.json"

phase=inventory_timers
while IFS= read -r unit; do
  printf '%s\t%s\t%s\n' "$unit" "$(systemctl is-enabled "$unit")" \
    "$(systemctl is-active "$unit")" >>"$timer_state"
done < <(systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u)
timer_count=$(wc -l <"$timer_state")
(( timer_count >= 15 && timer_count <= 32 ))

phase=quiesce_timers
timers_quiesced=1
while IFS=$'\t' read -r unit _enabled _active; do
  sudo -n systemctl stop "$unit"
done <"$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  service=${unit%.timer}.service
  for _attempt in $(seq 1 30); do
    systemctl is-active --quiet "$service" || break
    sleep 1
  done
  ! systemctl is-active --quiet "$service"
done <"$timer_state"

phase=fresh_backup
partial="$snapshot_dir/.memory_pre_v5_2_semantic_compiler_v8_${run_tag}.dump.partial"
backup="$snapshot_dir/memory_pre_v5_2_semantic_compiler_v8_${run_tag}.dump"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
docker exec -i "$container" pg_restore -l <"$partial" >"$backup.catalog"
[[ -s "$backup.catalog" ]]
mv "$partial" "$backup"
chmod 0600 "$backup" "$backup.catalog"
backup_sha=$(sha256sum "$backup" | awk '{print $1}')
printf '%s  %s\n' "$backup_sha" "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"

phase=baseline
docker exec "$container" psql -U sage -d "$database" -X -At -F $'\t' \
  -c "SELECT table_schema,table_name FROM information_schema.tables
      WHERE table_type='BASE TABLE'
        AND table_schema IN ('memory','public')
      ORDER BY table_schema,table_name" >"$table_list"
capture_state "$before"
qdrant_before=$(qdrant_signature)
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" == 0 ]]

phase=install_restricted_functions
run_sql <"$persistence_migration" >/dev/null
persistence_installed=1
run_sql <"$reextract_migration" >/dev/null
reextract_installed=1

phase=rollback_only_security
run_sql <"$persistence_test" >/dev/null
run_sql \
  -v target_owner="$owner" \
  -v other_owner="$other" \
  -v evidence_id=fea59e7e-30f5-4139-b634-97b291c88e14 \
  -v content_sha256=895146b94431f7e0ec3292e757e30fc4c782af222bd39598e610654adf08ccec \
  -v prior_packet_id=a7f23e7b-89ff-5d23-bb75-79473be6f57d \
  -v prior_packet_storage_sha256=430264a8f709288562505c0e50c97e2b379af59536ff5a65061352c3b925789e \
  -v operation_id=6d907502-d830-5467-9e61-fe846453383d \
  -v job_id=d0585d84-7386-5b5f-b7ff-ac97949883cc \
  -v terminal_id=25717c16-e87c-54ff-92da-d071770f6ab7 \
  -v manifest_sha256="$manifest_sha" \
  -v compiler_sha256="$compiler_sha" \
  <"$reextract_test" >/dev/null

phase=deploy_code
git -C "$live" merge --ff-only "$target_commit"
code_deployed=1
sudo -n systemctl restart brains.service
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(curl -sS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:8088/docs)" == 200 ]]
[[ "$(git -C "$live" rev-parse HEAD)" == "$target_commit" ]]
[[ -z "$(git -C "$live" status --porcelain)" ]]

phase=postflight
capture_state "$after"
cmp -s "$before" "$after"
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id<>'$owner'::uuid
    AND policy_compiler_sha256='$compiler_sha'")" == 0 ]]
[[ "$(scalar "SELECT count(*) FROM memory.evidence_extraction_packet_v5_local
  WHERE policy_compiler_sha256='$compiler_sha'")" == 0 ]]

phase=restore_timers
restore_timers
while IFS=$'\t' read -r unit enabled active; do
  [[ "$(systemctl is-enabled "$unit")" == "$enabled" ]]
  [[ "$(systemctl is-active "$unit")" == "$active" ]]
done <"$timer_state"

phase=report
jq -n \
  --arg contract_version memory_v1_v5_2_semantic_compiler_v8_install_v1 \
  --arg commit "$target_commit" \
  --arg backup "$backup" \
  --arg backup_sha256 "$backup_sha" \
  --arg qdrant_sha256 "$qdrant_before" \
  --arg semantic_report "$semantic_report" \
  --arg semantic_report_sha256 8a6e28bdb0439bfe36ebf76f3c717a6ed8d77ed3ea3f9134cba81e1ac7943214 \
  --argjson timer_count "$timer_count" \
  '{
    contract_version:$contract_version,
    outcome:"pass",
    commit:$commit,
    code_deployed:true,
    persistence_v8_installed:true,
    exact_reextract_function_installed:true,
    durable_rows_written:0,
    qdrant_unchanged:true,
    account_isolation_proved:true,
    service_health:true,
    timer_count:$timer_count,
    timers_restored:true,
    semantic_clone_report:{
      path:$semantic_report,sha256:$semantic_report_sha256
    },
    backup:{path:$backup,sha256:$backup_sha256},
    qdrant_sha256:$qdrant_sha256
  }' >"$report"
chmod 0600 "$report"
report_sha=$(sha256sum "$report" | awk '{print $1}')
printf '%s  %s\n' "$report_sha" "$report" >"$report.sha256"
chmod 0600 "$report.sha256"

phase=complete
printf 'memory_v1_v5_2_semantic_compiler_v8_install: PASS\n'
printf 'report=%s\nreport_sha256=%s\nbackup=%s\nbackup_sha256=%s\n' \
  "$report" "$report_sha" "$backup" "$backup_sha"
