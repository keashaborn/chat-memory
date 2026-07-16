#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 AUTHORIZATION.json" >&2
  exit 2
fi
if [[ "${MEMORY_V1_PROJECTION_REVIEW_APPLY:-}" != "authorized" ]]; then
  echo "MEMORY_V1_PROJECTION_REVIEW_APPLY=authorized is required" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
authorization=$(realpath "$1")
required_ancestor=083d71071a3c6f3e2b6c197cb54da3195329178c
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
plan=33000000-0000-4000-8000-000000000001
manifest=d78fadfe2334a95f77d2f11c549d65866f72fd4e522b25b0cd9c20ad8bab6f50
reviewer_ref=memory_v1_v5_entity_resolution_projection_preparation_20260716
reason='direct user statement with applied owner-scoped entity bindings; phase-authorized projection preparation'
reason_codes='["direct_user_statement","applied_entity_bindings","phase_authorized_projection_preparation"]'
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
lock_file=/home/ubuntu/brains/.memory_v1_projection_review_apply.lock
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
 "target_server","scope","owner_user_id","plan_id","projection_ref",
 "decision","reviewer_type","reviewer_ref","reason","reason_codes",
 "authorization_manifest_sha256","expected_rows_created"
}
if set(value)!=expected:
    raise SystemExit("authorization keys mismatch")
if value["contract_version"]!="memory_v1_v5_projection_review_apply_authorization_v1":
    raise SystemExit("authorization contract mismatch")
if value["phase_authorization"]!="memory_v1_v5_entity_resolution_projection_preparation_20260716":
    raise SystemExit("phase authorization mismatch")
if value["authorized"] is not True or value["authorized_by"]!="Eric Lund":
    raise SystemExit("authorization identity mismatch")
uuid.UUID(value["authorization_id"])
exact={
 "expected_head_commit":os.environ["EXPECTED_HEAD"],"target_server":"seebx",
 "scope":"authorize_staged_occupation_projection",
 "owner_user_id":"1240822d-ac9a-4096-95aa-e2b24d36ef50",
 "plan_id":"33000000-0000-4000-8000-000000000001","projection_ref":"p01",
 "decision":"authorized","reviewer_type":"system",
 "reviewer_ref":"memory_v1_v5_entity_resolution_projection_preparation_20260716",
 "reason":"direct user statement with applied owner-scoped entity bindings; phase-authorized projection preparation",
 "reason_codes":["direct_user_statement","applied_entity_bindings","phase_authorized_projection_preparation"],
 "authorization_manifest_sha256":"d78fadfe2334a95f77d2f11c549d65866f72fd4e522b25b0cd9c20ad8bab6f50",
 "expected_rows_created":1
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
flock -n 9 || { echo "another projection review holds the lock" >&2; exit 1; }
umask 077
auth_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["authorization_id"])' "$authorization")
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$auth_id"
status_file="$snapshot_dir/memory_v1_projection_review_apply_${run_id}.status"
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
actor_review() {
  docker exec -i "$container" psql -X -q -A -t -F '|' \
    -v ON_ERROR_STOP=1 -U sage -d "$database" <<SQL
BEGIN;
SET SESSION AUTHORIZATION brains_app;
SELECT set_config('app.user_id','$owner',true) \gset
SELECT review_id,outcome,rows_written
FROM memory.review_projection_v5(
  '$plan'::uuid,'p01','authorized','system','$reviewer_ref',
  '$reason','$reason_codes'::jsonb,'$manifest'
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

phase=preflight
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_review WHERE plan_id='$plan'::uuid AND projection_ref='p01'")" == 0 ]]
before_review=$(psql_scalar "SELECT count(*) FROM memory.projection_review")
before_apply=$(psql_scalar "SELECT count(*) FROM memory.projection_apply_event")
before_claim=$(psql_scalar "SELECT count(*) FROM memory.claim WHERE canonical_key='v5:f14ab73b4b2eb5db5494878638d229367af1a170902bd92bcc6c261ea50c3082'")
qdrant_before=$(qdrant_signature)

phase=backup
partial="$snapshot_dir/.memory_pre_projection_review_${run_id}.dump.partial"
backup="$snapshot_dir/memory_pre_projection_review_${run_id}.dump"
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

phase=transactional_review
review_output=$(actor_review)
IFS='|' read -r review_id outcome rows_written <<<"$review_output"
[[ "$review_id" =~ ^[0-9a-f-]{36}$ ]]
[[ "$outcome" == applied && "$rows_written" == 1 ]]

phase=zero_write_replay
replay_before="$snapshot_dir/memory_v1_projection_review_replay_before_${run_id}.tsv"
replay_after="$snapshot_dir/memory_v1_projection_review_replay_after_${run_id}.tsv"
capture_all "$replay_before"
replay_output=$(actor_review)
IFS='|' read -r replay_id replay_outcome replay_rows <<<"$replay_output"
[[ "$replay_id" == "$review_id" && "$replay_outcome" == replayed && "$replay_rows" == 0 ]]
capture_all "$replay_after"
cmp -s "$replay_before" "$replay_after"

phase=postflight
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_review")" == "$((before_review+1))" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_review WHERE review_id='$review_id'::uuid AND owner_user_id='$owner'::uuid AND plan_id='$plan'::uuid AND projection_ref='p01' AND decision='authorized' AND reviewer_type='system' AND reviewer_ref='$reviewer_ref' AND authorization_manifest_sha256='$manifest'")" == 1 ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.projection_apply_event")" == "$before_apply" ]]
[[ "$(psql_scalar "SELECT count(*) FROM memory.claim WHERE canonical_key='v5:f14ab73b4b2eb5db5494878638d229367af1a170902bd92bcc6c261ea50c3082'")" == "$before_claim" ]]
qdrant_after=$(qdrant_signature)
[[ "$qdrant_after" == "$qdrant_before" ]]

phase=report
report="$snapshot_dir/memory_v1_projection_review_apply_${run_id}.json"
AUTHORIZATION="$authorization" BACKUP="$backup" CATALOG="$catalog" REPORT="$report" \
REVIEW_ID="$review_id" REPLAY_BEFORE="$replay_before" REPLAY_AFTER="$replay_after" \
QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
HEAD="$(git -C "$repo_root" rev-parse HEAD)" python3 - <<'PY'
import datetime as dt, json, os
from pathlib import Path
auth=json.loads(Path(os.environ["AUTHORIZATION"]).read_text())
value={
 "contract_version":"memory_v1_v5_projection_review_apply_report_v1",
 "authorization_id":auth["authorization_id"],"phase_authorization":auth["phase_authorization"],
 "completed_at":dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00","Z"),
 "head_commit":os.environ["HEAD"],"review_id":os.environ["REVIEW_ID"],
 "backup":{"path":os.environ["BACKUP"],"catalog":os.environ["CATALOG"]},
 "evidence":{"replay_before":os.environ["REPLAY_BEFORE"],"replay_after":os.environ["REPLAY_AFTER"],
             "qdrant_before_sha256":os.environ["QDRANT_BEFORE"],
             "qdrant_after_sha256":os.environ["QDRANT_AFTER"]},
 "checks":{"review_rows_created":1,"zero_write_replay":True,
           "projection_apply_rows":0,"claim_rows_unchanged":True,
           "qdrant_unchanged":True,"runtime_activated":False},
 "hard_stop":"before_projection_apply_or_runtime_activation"
}
Path(os.environ["REPORT"]).write_text(json.dumps(value,indent=2,sort_keys=True)+"\n")
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
phase=complete
printf 'memory_v1_v5_projection_review_production_apply: PASS\n'
printf 'report=%s\nbackup=%s\nreview_id=%s\n' "$report" "$backup" "$review_id"
