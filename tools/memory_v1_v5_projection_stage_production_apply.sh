#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 AUTHORIZATION.json BUNDLE.json" >&2
  exit 2
fi
if [[ "${MEMORY_V1_PROJECTION_STAGE_APPLY:-}" != "authorized" ]]; then
  echo "MEMORY_V1_PROJECTION_STAGE_APPLY=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
authorization=$(realpath "$1")
bundle=$(realpath "$2")
required_ancestor=f6a790d00f5f936e7c39e0c192e5407bb7acc58b
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
plan=33000000-0000-4000-8000-000000000001
packet_sha=b8860a6714792a33ea873d4b216fc18366cbb33795e29ddea044a0c67462e9ae
owner_manifest=f17b7c235a2f398bdf5744f43e9e4da153ed75c55a954fb4a0cbc1c41b537138
expected_bundle_sha=573a57042ada49d6a30fc32e4c705e42b5030d3aaf2b316592e0caa3756bfc31
expected_file_sha=7e1d7a111835c0a069912cc3cad0afc3f86e7e4e1233a301e1a7cefe4b87b4e2
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_projection_stage_apply.lock
phase=initialization
status_file=

[[ "$authorization" != "$repo_root"/* && "$bundle" != "$repo_root"/* ]]
[[ -z "$(git -C "$repo_root" status --porcelain)" ]]
git -C "$repo_root" merge-base --is-ancestor "$required_ancestor" HEAD
[[ "$(stat -c '%a' "$authorization")" == 600 ]]
[[ "$(stat -c '%a' "$bundle")" == 600 ]]
[[ "$(sha256sum "$bundle" | awk '{print $1}')" == "$expected_file_sha" ]]

bundle_data=$(BUNDLE="$bundle" python3 - <<'PY'
import hashlib, json, os
from pathlib import Path
path=Path(os.environ["BUNDLE"])
value=json.loads(path.read_text())
provided=value.pop("bundle_sha256")
canonical=json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False)
computed=hashlib.sha256(canonical.encode()).hexdigest()
if provided != computed:
    raise SystemExit("bundle manifest mismatch")
exact={
 "contract_version":"memory_v1_projection_stage_bundle_v1",
 "owner_user_id":"1240822d-ac9a-4096-95aa-e2b24d36ef50",
 "plan_id":"33000000-0000-4000-8000-000000000001",
 "projection_ref":"p01",
 "packet_sha256":"b8860a6714792a33ea873d4b216fc18366cbb33795e29ddea044a0c67462e9ae",
 "owner_manifest_sha256":"f17b7c235a2f398bdf5744f43e9e4da153ed75c55a954fb4a0cbc1c41b537138",
 "database_writes":0,"external_model_calls":0,"qdrant_writes":0,
}
for key, expected in exact.items():
    if value.get(key) != expected:
        raise SystemExit(f"bundle {key} mismatch")
print(provided)
print(value["packet_text"])
PY
)
bundle_manifest=$(printf '%s\n' "$bundle_data" | sed -n '1p')
packet_text=$(printf '%s\n' "$bundle_data" | sed -n '2p')
[[ "$bundle_manifest" == "$expected_bundle_sha" ]]

validate_authorization() {
  AUTHORIZATION="$authorization" EXPECTED_HEAD="$(git -C "$repo_root" rev-parse HEAD)" \
    python3 - <<'PY'
import datetime as dt, json, os, stat, uuid
from pathlib import Path
path=Path(os.environ["AUTHORIZATION"])
if stat.S_IMODE(path.stat().st_mode) != 0o600:
    raise SystemExit("authorization mode mismatch")
value=json.loads(path.read_text())
expected={
 "contract_version","authorization_id","phase_authorization","authorized",
 "authorized_by","authorized_at","expires_at","expected_head_commit",
 "target_server","scope","owner_user_id","plan_id","bundle_sha256",
 "bundle_file_sha256","packet_sha256","owner_manifest_sha256",
 "expected_rows_created"
}
if set(value) != expected:
    raise SystemExit("authorization keys mismatch")
if value["contract_version"]!="memory_v1_v5_projection_stage_apply_authorization_v1":
    raise SystemExit("authorization contract mismatch")
if value["phase_authorization"]!="memory_v1_v5_entity_resolution_projection_preparation_20260716":
    raise SystemExit("phase authorization mismatch")
if value["authorized"] is not True or value["authorized_by"]!="Eric Lund":
    raise SystemExit("authorization identity mismatch")
uuid.UUID(value["authorization_id"])
exact={
 "expected_head_commit":os.environ["EXPECTED_HEAD"],"target_server":"seebx",
 "scope":"stage_hash_locked_occupation_projection_plan",
 "owner_user_id":"1240822d-ac9a-4096-95aa-e2b24d36ef50",
 "plan_id":"33000000-0000-4000-8000-000000000001",
 "bundle_sha256":"573a57042ada49d6a30fc32e4c705e42b5030d3aaf2b316592e0caa3756bfc31",
 "bundle_file_sha256":"7e1d7a111835c0a069912cc3cad0afc3f86e7e4e1233a301e1a7cefe4b87b4e2",
 "packet_sha256":"b8860a6714792a33ea873d4b216fc18366cbb33795e29ddea044a0c67462e9ae",
 "owner_manifest_sha256":"f17b7c235a2f398bdf5744f43e9e4da153ed75c55a954fb4a0cbc1c41b537138",
 "expected_rows_created":4
}
for key, expected_value in exact.items():
    if value[key] != expected_value:
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
flock -n 9 || { echo "another projection stage holds the lock" >&2; exit 1; }
umask 077
auth_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["authorization_id"])' "$authorization")
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$auth_id"
status_file="$snapshot_dir/memory_v1_projection_stage_apply_${run_id}.status"
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
actor_call() {
  docker exec -i "$container" psql -X -q -A -t -F '|' \
    -v ON_ERROR_STOP=1 -v packet_text="$packet_text" \
    -U sage -d "$database" <<SQL
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','$owner',true) \gset
SELECT plan_id,outcome,rows_written
FROM memory.stage_projection_plan_v5(
  '$plan'::uuid,:'packet_text','$owner_manifest'
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
capture_all() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(psql_scalar "SELECT count(*)::text || E'\\t' || encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(t)::text row_json FROM memory.\"$table\" t) rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(psql_scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' ORDER BY table_name")
  chmod 0600 "$output"
}
capture_non_target() {
  local output=$1
  : >"$output"
  while IFS= read -r table; do
    state=$(psql_scalar "SELECT count(*)::text || E'\\t' || encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(t)::text row_json FROM memory.\"$table\" t) rows")
    printf '%s\t%s\n' "$table" "$state" >>"$output"
  done < <(psql_scalar "SELECT table_name FROM information_schema.tables WHERE table_schema='memory' AND table_type='BASE TABLE' AND table_name NOT IN ('projection_plan','projection_plan_item','projection_claim_payload','projection_plan_observation') ORDER BY table_name")
  printf 'projection_plan_other\t%s\n' "$(psql_scalar "SELECT count(*)::text || E'\\t' || encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(t)::text row_json FROM memory.projection_plan t WHERE plan_id <> '$plan'::uuid) rows")" >>"$output"
  printf 'projection_plan_item_other\t%s\n' "$(psql_scalar "SELECT count(*)::text || E'\\t' || encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(t)::text row_json FROM memory.projection_plan_item t WHERE plan_id <> '$plan'::uuid) rows")" >>"$output"
  printf 'projection_claim_payload_other\t%s\n' "$(psql_scalar "SELECT count(*)::text || E'\\t' || encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(t)::text row_json FROM memory.projection_claim_payload t WHERE plan_id <> '$plan'::uuid) rows")" >>"$output"
  printf 'projection_plan_observation_other\t%s\n' "$(psql_scalar "SELECT count(*)::text || E'\\t' || encode(digest(coalesce(string_agg(row_json,E'\\n' ORDER BY row_json),''),'sha256'),'hex') FROM (SELECT to_jsonb(t)::text row_json FROM memory.projection_plan_observation t WHERE plan_id <> '$plan'::uuid) rows")" >>"$output"
  chmod 0600 "$output"
}

phase=preflight
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_plan WHERE plan_id='$plan'::uuid")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.claim WHERE canonical_key='v5:f14ab73b4b2eb5db5494878638d229367af1a170902bd92bcc6c261ea50c3082'")" == 0 ]]
before_plan=$(psql_scalar "SELECT count(*) FROM memory.projection_plan")
before_item=$(psql_scalar "SELECT count(*) FROM memory.projection_plan_item")
before_payload=$(psql_scalar "SELECT count(*) FROM memory.projection_claim_payload")
before_link=$(psql_scalar "SELECT count(*) FROM memory.projection_plan_observation")
baseline="$snapshot_dir/memory_v1_projection_stage_baseline_${run_id}.tsv"
post="$snapshot_dir/memory_v1_projection_stage_post_${run_id}.tsv"
capture_non_target "$baseline"
qdrant_before=$(qdrant_signature)

phase=backup
partial="$snapshot_dir/.memory_pre_projection_stage_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_projection_stage_${run_id}.dump"
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

phase=transactional_stage
stage_output=$(actor_call)
[[ "$stage_output" == "$plan|applied|4" ]]

phase=zero_write_replay
replay_before="$snapshot_dir/memory_v1_projection_stage_replay_before_${run_id}.tsv"
replay_after="$snapshot_dir/memory_v1_projection_stage_replay_after_${run_id}.tsv"
capture_all "$replay_before"
replay_output=$(actor_call)
[[ "$replay_output" == "$plan|replayed|0" ]]
capture_all "$replay_after"
cmp -s "$replay_before" "$replay_after"

phase=postflight
capture_non_target "$post"
cmp -s "$baseline" "$post"
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_plan")" == "$((before_plan+1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_plan_item")" == "$((before_item+1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_claim_payload")" == "$((before_payload+1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_plan_observation")" == "$((before_link+1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_plan WHERE owner_user_id='$owner'::uuid AND plan_id='$plan'::uuid AND packet_sha256='$packet_sha' AND owner_manifest_sha256='$owner_manifest'")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_plan_item WHERE owner_user_id='$owner'::uuid AND plan_id='$plan'::uuid AND projection_ref='p01' AND review_state='manual_review_required'")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_review")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_apply_event")" == 0 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.claim WHERE canonical_key='v5:f14ab73b4b2eb5db5494878638d229367af1a170902bd92bcc6c261ea50c3082'")" == 0 ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=report
report="$snapshot_dir/memory_v1_projection_stage_apply_${run_id}.json"
AUTHORIZATION="$authorization" BUNDLE="$bundle" BACKUP="$backup" CATALOG="$catalog" \
BASELINE="$baseline" POST="$post" REPLAY_BEFORE="$replay_before" REPLAY_AFTER="$replay_after" \
REPORT="$report" QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" python3 - <<'PY'
import datetime as dt, json, os
from pathlib import Path
auth=json.loads(Path(os.environ["AUTHORIZATION"]).read_text())
value={
 "contract_version":"memory_v1_v5_projection_stage_apply_report_v1",
 "authorization_id":auth["authorization_id"],"phase_authorization":auth["phase_authorization"],
 "completed_at":dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00","Z"),
 "head_commit":os.environ["HEAD"],"bundle":os.environ["BUNDLE"],
 "backup":{"path":os.environ["BACKUP"],"catalog":os.environ["CATALOG"]},
 "created":{"projection_plan":1,"projection_plan_item":1,
            "projection_claim_payload":1,"projection_plan_observation":1,"total":4},
 "evidence":{"baseline":os.environ["BASELINE"],"post":os.environ["POST"],
             "replay_before":os.environ["REPLAY_BEFORE"],
             "replay_after":os.environ["REPLAY_AFTER"],
             "qdrant_before_sha256":os.environ["QDRANT_BEFORE"],
             "qdrant_after_sha256":os.environ["QDRANT_AFTER"]},
 "checks":{"zero_write_replay":True,"owner_scoped":True,"claim_rows_unchanged":True,
           "projection_review_rows":0,"projection_apply_rows":0,
           "qdrant_unchanged":True,"runtime_activated":False},
 "hard_stop":"before_projection_review_or_apply"
}
Path(os.environ["REPORT"]).write_text(json.dumps(value,indent=2,sort_keys=True)+"\n")
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'memory_v1_v5_projection_stage_production_apply: PASS\n'
printf 'report=%s\nbackup=%s\n' "$report" "$backup"
