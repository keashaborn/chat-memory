#!/usr/bin/env bash
set -euo pipefail

owner_user_id=${1:?owner user ID is required}
repo_root=$(git rev-parse --show-toplevel)
container=brains-postgres-1
database=memory
snapshot_dir=/home/ubuntu/brains/snapshots
run_id="$(date -u +%Y%m%dT%H%M%SZ)_$(git -C "$repo_root" rev-parse --short=12 HEAD)"
report="$snapshot_dir/memory_v1_record_evidence_replay_${run_id}.json"
umask 077

psql_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$database" -c "$1"
}

evidence_signature() {
  psql_scalar "
    SELECT count(*)::text || E'\\t' ||
           encode(digest(coalesce(string_agg(row_json,E'\\n'
             ORDER BY row_json),''),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(evidence_row)::text AS row_json
      FROM memory.evidence AS evidence_row
    ) rows
  "
}

owner_signature() {
  psql_scalar "
    SELECT encode(digest(coalesce(string_agg(
      owner_user_id::text || ':' || row_count::text,E'\\n'
      ORDER BY owner_user_id::text),''),'sha256'),'hex')
    FROM (
      SELECT owner_user_id,count(*) AS row_count
      FROM memory.evidence
      GROUP BY owner_user_id
    ) owner_rows
  "
}

qdrant_signature() {
  curl --fail --silent --show-error \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id)' \
    | sha256sum | awk '{print $1}'
}

evidence_before=$(evidence_signature)
owners_before=$(owner_signature)
qdrant_before=$(qdrant_signature)

set -a
source "$repo_root/.env"
set +a
probe_output=$(
  "$repo_root/venv/bin/python" \
    "$repo_root/scripts/memory_v1_record_evidence_replay_probe.py" \
    --owner-user-id "$owner_user_id"
)

evidence_after=$(evidence_signature)
owners_after=$(owner_signature)
qdrant_after=$(qdrant_signature)

[[ "$evidence_after" == "$evidence_before" ]]
[[ "$owners_after" == "$owners_before" ]]
[[ "$qdrant_after" == "$qdrant_before" ]]

REPORT="$report" PROBE_OUTPUT="$probe_output" \
EVIDENCE_BEFORE="$evidence_before" EVIDENCE_AFTER="$evidence_after" \
OWNERS_BEFORE="$owners_before" OWNERS_AFTER="$owners_after" \
QDRANT_BEFORE="$qdrant_before" QDRANT_AFTER="$qdrant_after" \
python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

probe = json.loads(os.environ["PROBE_OUTPUT"])
value = {
    **probe,
    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "checks": {
        "same_evidence_id": probe["same_evidence_id"],
        "evidence_table_unchanged": os.environ["EVIDENCE_BEFORE"] == os.environ["EVIDENCE_AFTER"],
        "owner_partition_counts_unchanged": os.environ["OWNERS_BEFORE"] == os.environ["OWNERS_AFTER"],
        "qdrant_unchanged": os.environ["QDRANT_BEFORE"] == os.environ["QDRANT_AFTER"],
    },
}
Path(os.environ["REPORT"]).write_text(
    json.dumps(value, indent=2, sort_keys=True) + "\n"
)
PY
chmod 0600 "$report"
sha256sum "$report" >"$report.sha256"
chmod 0600 "$report.sha256"
printf 'memory_v1_record_evidence_replay_probe: PASS\n'
printf 'report=%s\n' "$report"
