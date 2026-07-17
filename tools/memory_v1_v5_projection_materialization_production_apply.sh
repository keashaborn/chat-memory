#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 AUTHORIZATION.json" >&2
  exit 2
fi
if [[ "${MEMORY_V1_PROJECTION_MATERIALIZE:-}" != "authorized" ]]; then
  echo "MEMORY_V1_PROJECTION_MATERIALIZE=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
authorization=$(realpath "$1")
required_ancestor=3af5db7fbdb456fe9193d96f8f417791aaed4c16
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
plan=33000000-0000-4000-8000-000000000001
review=79f6d3fe-2a8f-486a-8681-e3a66420385d
request=34000000-0000-4000-8000-000000000001
manifest=7ca9fb9157392e38ed5e8d8f136c970bfb25226b0208d0f81b30f42c5c0b2486
semantic=f14ab73b4b2eb5db5494878638d229367af1a170902bd92bcc6c261ea50c3082
observation=9bf1e6b2-1840-4524-98dc-142567ebe013
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_projection_materialization.lock
phase=initialization
status_file=

[[ "$authorization" != "$repo_root"/* ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD

validate_authorization() {
  AUTHORIZATION="$authorization" EXPECTED_HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
    python3 - <<'PY'
import datetime as dt, json, os, stat, uuid
from pathlib import Path
path=Path(os.environ["AUTHORIZATION"])
if stat.S_IMODE(path.stat().st_mode)!=0o600:
    raise SystemExit("authorization mode mismatch")
value=json.loads(path.read_text())
expected={
 "contract_version","authorization_id","phase_authorization","authorized",
 "authorized_by","authorized_at","expires_at","expected_head_commit",
 "target_server","scope","owner_user_id","request_id","plan_id",
 "projection_ref","review_id","apply_manifest_sha256","expected_rows_created"
}
if set(value)!=expected:
    raise SystemExit("authorization keys mismatch")
if value["contract_version"]!="memory_v1_v5_projection_materialization_authorization_v1":
    raise SystemExit("authorization contract mismatch")
if value["phase_authorization"]!="memory_v1_v5_projection_materialization_20260716":
    raise SystemExit("phase authorization mismatch")
if value["authorized"] is not True or value["authorized_by"]!="Eric Lund":
    raise SystemExit("authorization identity mismatch")
uuid.UUID(value["authorization_id"])
exact={
 "expected_head_commit":os.environ["EXPECTED_HEAD"],"target_server":"seebx",
 "scope":"materialize_reviewed_occupation_claim_without_runtime_activation",
 "owner_user_id":"1240822d-ac9a-4096-95aa-e2b24d36ef50",
 "request_id":"34000000-0000-4000-8000-000000000001",
 "plan_id":"33000000-0000-4000-8000-000000000001",
 "projection_ref":"p01","review_id":"79f6d3fe-2a8f-486a-8681-e3a66420385d",
 "apply_manifest_sha256":"7ca9fb9157392e38ed5e8d8f136c970bfb25226b0208d0f81b30f42c5c0b2486",
 "expected_rows_created":5
}
for key, expected_value in exact.items():
    if value[key]!=expected_value:
        raise SystemExit(f"authorization {key} mismatch")
issued=dt.datetime.fromisoformat(value["authorized_at"].replace("Z","+00:00"))
expires=dt.datetime.fromisoformat(value["expires_at"].replace("Z","+00:00"))
now=dt.datetime.now(dt.timezone.utc)
if expires<=issued or expires-issued>dt.timedelta(minutes=30):
    raise SystemExit("authorization window invalid")
if now<issued-dt.timedelta(seconds=30) or now>=expires:
    raise SystemExit("authorization expired")
PY
}
validate_authorization
exec 9>"$lock_file"
flock -n 9 || { echo "another projection materialization holds the lock" >&2; exit 1; }
umask 077
auth_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["authorization_id"])' "$authorization")
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$auth_id"
status_file="$snapshot_dir/memory_v1_projection_materialization_${run_id}.status"
record_exit() {
  code=$?
  printf 'run_id=%s\nphase=%s\nexit_code=%s\ncompleted_at=%s\n' \
    "$run_id" "$phase" "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"$status_file"
  chmod 0600 "$status_file"
}
trap record_exit EXIT

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}
actor_apply() {
  docker exec -i "$container" psql -X -q -A -t -F '|' \
    -v ON_ERROR_STOP=1 -U sage -d "$database" <<SQL
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','$owner',true) \gset
SELECT apply_event_id,outcome,lane,aggregate_id,revision_id,
       revision_number,rows_written
FROM memory.apply_projection_v5(
  '$request'::uuid,'$plan'::uuid,'p01','$review'::uuid,'$manifest'
);
RESET SESSION AUTHORIZATION;
COMMIT;
SQL
}
qdrant_signature() {
  curl --fail --silent --show-error -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id)' | sha256sum | awk '{print $1}'
}
table_state() {
  local table=$1 predicate=${2:-true}
  psql_scalar "SELECT count(*)::text || E'\\t' || encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(t)::text row_json FROM memory.\"$table\" t WHERE $predicate) rows"
}
capture_all() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    printf '%s\t%s\n' "$table" "$(table_state "$table")" >>"$output"
  done < <(psql_scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name")
  chmod 0600 "$output"
}
capture_post_non_target() {
  local output=$1 claim_id=$2 event_id=$3
  : >"$output"
  while IFS= read -r table; do
    printf '%s\t%s\n' "$table" "$(table_state "$table")" >>"$output"
  done < <(psql_scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' AND table_name NOT IN ('claim','claim_revision','claim_observation','projection_apply_event','projection_dispatch_v5') ORDER BY table_name")
  printf 'claim\t%s\n' "$(table_state claim "claim_id <> '$claim_id'::uuid")" >>"$output"
  printf 'claim_revision\t%s\n' "$(table_state claim_revision "claim_id <> '$claim_id'::uuid")" >>"$output"
  printf 'claim_observation\t%s\n' "$(table_state claim_observation "claim_id <> '$claim_id'::uuid")" >>"$output"
  printf 'projection_apply_event\t%s\n' "$(table_state projection_apply_event "event_id <> '$event_id'::uuid")" >>"$output"
  printf 'projection_dispatch_v5\t%s\n' "$(table_state projection_dispatch_v5 "apply_event_id <> '$event_id'::uuid")" >>"$output"
  sort -o "$output" "$output"
  chmod 0600 "$output"
}

phase=preflight
[[ "$(psql_scalar "SELECT count(*) FROM memory.claim WHERE canonical_key='v5:$semantic'")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_apply_event WHERE request_id='$request'::uuid")" == 0 ]]
baseline="$snapshot_dir/memory_v1_projection_materialization_baseline_${run_id}.tsv"
capture_all "$baseline"
sort -o "$baseline" "$baseline"
qdrant_before=$(qdrant_signature)
before_claim=$(psql_scalar "SELECT count(*) FROM memory.claim")
before_revision=$(psql_scalar "SELECT count(*) FROM memory.claim_revision")
before_link=$(psql_scalar "SELECT count(*) FROM memory.claim_observation")
before_event=$(psql_scalar "SELECT count(*) FROM memory.projection_apply_event")
before_dispatch=$(psql_scalar "SELECT count(*) FROM memory.projection_dispatch_v5")

phase=backup
partial="$snapshot_dir/.memory_pre_projection_materialization_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_projection_materialization_${run_id}.dump"
catalog="$backup.catalog"
docker exec "$container" pg_dump -U sage -d "$database" \
  -Fc --no-owner --no-privileges >"$partial"
[[ -s "$partial" ]]
docker exec -i "$container" pg_restore -l <"$partial" >"$catalog"
[[ -s "$catalog" ]]
mv "$partial" "$backup"
chmod 0600 "$backup" "$catalog"
sha256sum "$backup" >"$backup.sha256"
chmod 0600 "$backup.sha256"
validate_authorization

phase=transactional_materialization
apply_output=$(actor_apply)
IFS='|' read -r event_id outcome lane claim_id revision_id revision_number rows_written <<<"$apply_output"
[[ "$event_id" =~ ^[0-9a-f-]{36}$ && "$claim_id" =~ ^[0-9a-f-]{36}$ ]]
[[ "$revision_id" =~ ^[0-9a-f-]{36}$ ]]
[[ "$outcome" == applied && "$lane" == claim ]]
[[ "$revision_number" == 1 && "$rows_written" == 5 ]]

phase=zero_write_replay
replay_before="$snapshot_dir/memory_v1_projection_materialization_replay_before_${run_id}.tsv"
replay_after="$snapshot_dir/memory_v1_projection_materialization_replay_after_${run_id}.tsv"
capture_all "$replay_before"
replay_output=$(actor_apply)
IFS='|' read -r replay_event replay_outcome replay_lane replay_claim replay_revision replay_number replay_rows <<<"$replay_output"
[[ "$replay_event" == "$event_id" && "$replay_outcome" == replayed ]]
[[ "$replay_lane" == claim && "$replay_claim" == "$claim_id" ]]
[[ "$replay_revision" == "$revision_id" && "$replay_number" == 1 && "$replay_rows" == 0 ]]
capture_all "$replay_after"
cmp -s "$replay_before" "$replay_after"

phase=postflight
post="$snapshot_dir/memory_v1_projection_materialization_post_${run_id}.tsv"
capture_post_non_target "$post" "$claim_id" "$event_id"
cmp -s "$baseline" "$post"
[[ "$(psql_scalar "SELECT count(*) FROM memory.claim")" == "$((before_claim+1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.claim_revision")" == "$((before_revision+1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.claim_observation")" == "$((before_link+1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_apply_event")" == "$((before_event+1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_dispatch_v5")" == "$((before_dispatch+1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.claim WHERE claim_id='$claim_id'::uuid AND owner_user_id='$owner'::uuid AND canonical_key='v5:$semantic' AND status='candidate' AND canonical_text='The user works as a personal trainer.'")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.claim_revision WHERE claim_id='$claim_id'::uuid AND revision_id='$revision_id'::uuid AND revision_number=1")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.claim_observation WHERE claim_id='$claim_id'::uuid AND observation_id='$observation'::uuid")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_apply_event WHERE event_id='$event_id'::uuid AND request_id='$request'::uuid AND apply_manifest_sha256='$manifest'")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_dispatch_v5 WHERE apply_event_id='$event_id'::uuid")" == 1 ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=report
report="$snapshot_dir/memory_v1_projection_materialization_${run_id}.json"
AUTHORIZATION="$authorization" BACKUP="$backup" CATALOG="$catalog" REPORT="$report" \
CLAIM_ID="$claim_id" REVISION_ID="$revision_id" EVENT_ID="$event_id" \
BASELINE="$baseline" POST="$post" REPLAY_BEFORE="$replay_before" REPLAY_AFTER="$replay_after" \
QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" python3 - <<'PY'
import datetime as dt, json, os
from pathlib import Path
auth=json.loads(Path(os.environ["AUTHORIZATION"]).read_text())
value={
 "contract_version":"memory_v1_v5_projection_materialization_report_v1",
 "authorization_id":auth["authorization_id"],"phase_authorization":auth["phase_authorization"],
 "completed_at":dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00","Z"),
 "head_commit":os.environ["HEAD"],"claim_id":os.environ["CLAIM_ID"],
 "revision_id":os.environ["REVISION_ID"],"apply_event_id":os.environ["EVENT_ID"],
 "backup":{"path":os.environ["BACKUP"],"catalog":os.environ["CATALOG"]},
 "created":{"claim":1,"claim_revision":1,"claim_observation":1,
            "projection_apply_event":1,"projection_dispatch_v5":1,"total":5},
 "evidence":{"baseline":os.environ["BASELINE"],"post":os.environ["POST"],
             "replay_before":os.environ["REPLAY_BEFORE"],"replay_after":os.environ["REPLAY_AFTER"],
             "qdrant_before_sha256":os.environ["QDRANT_BEFORE"],
             "qdrant_after_sha256":os.environ["QDRANT_AFTER"]},
 "checks":{"claim_status":"candidate","zero_write_replay":True,
           "other_owner_state_unchanged":True,"qdrant_unchanged":True,
           "dispatch_not_consumed":True,"runtime_activated":False},
 "hard_stop":"before_dispatch_consumption_qdrant_or_runtime_retrieval"
}
Path(os.environ["REPORT"]).write_text(json.dumps(value,indent=2,sort_keys=True)+"\n")
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'memory_v1_v5_projection_materialization_production_apply: PASS\n'
printf 'report=%s\nbackup=%s\nclaim_id=%s\n' "$report" "$backup" "$claim_id"
