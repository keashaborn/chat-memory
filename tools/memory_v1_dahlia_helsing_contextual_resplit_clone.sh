#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Clone-tests one exact five-span contextual split and
# three private zero-write semantic checks. Production remains unchanged.

repo=${REPO_ROOT:-/tmp/chat-memory-pet-species-domain-v1}
production_repo=/opt/chat-memory
container=brains-postgres-1
source_db=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=673d64a3-c4ba-4d1c-89e3-e0c579022fad
parent=e7b0831a-9a15-48dd-97da-e97e9726a5e5
parent_sha=f0e5a3944531e19afbdae73982faba4b71d95c7c105f62a64eeecb23592c6b25
selector=20260729_v4_contextual_resplit
runner=$repo/scripts/memory_v1_contextual_exact_resplit_v3.py
canary=$repo/tools/memory_v1_evidence_context_local_canary_v1.py
python_bin=/opt/chat-memory/venv/bin/python
api_key_file=/etc/memory-v1-local-inference/api-key
clone_db="memory_dahlia_helsing_context_$(date -u +%Y%m%dT%H%M%SZ)_$$"
artifact_dir=$(mktemp -d /tmp/memory-dahlia-helsing-context.XXXXXX)
timer_state=$artifact_dir/timers.tsv
protected_before=$artifact_dir/protected-before.tsv
protected_after=$artifact_dir/protected-after.tsv
timers_restored=0

restore_timers() {
  if [[ "$timers_restored" == 1 || ! -s "$timer_state" ]]; then
    return
  fi
  while IFS=$'\t' read -r unit enabled active; do
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null
    else
      systemctl disable "$unit" >/dev/null 2>&1 || true
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit"
    else
      systemctl stop "$unit"
    fi
  done <"$timer_state"
  timers_restored=1
}

cleanup() {
  rc=$?
  trap - EXIT
  restore_timers
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
  rm -rf "$artifact_dir"
  exit "$rc"
}
trap cleanup EXIT

test "$(id -u)" -eq 0
test -z "$(git -C "$production_repo" status --porcelain)"
test -z "$(git -C "$repo" status --porcelain)"
test -r "$runner"
test -r "$canary"
test -r "$api_key_file"
test "$(stat -c %a "$api_key_file")" = 600
test "$(systemctl is-active brains.service)" = active

systemctl list-unit-files 'memory-v1-*.timer' --no-legend --no-pager \
  | awk '{print $1}' | sort -u \
  | while read -r unit; do
      printf '%s\t%s\t%s\n' "$unit" \
        "$(systemctl is-enabled "$unit")" \
        "$(systemctl is-active "$unit")"
    done >"$timer_state"
test -s "$timer_state"
while IFS=$'\t' read -r unit _enabled _active; do
  systemctl stop "$unit"
done <"$timer_state"
test "$(systemctl is-active memory-v1-v5-local-inference-scheduler.service)" \
  != active
test "$(
  docker exec "$container" psql -U sage -d "$source_db" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE lease_token IS NOT NULL
      AND lease_expires_at>clock_timestamp();
  "
)" = 0

set -a
source "$production_repo/.env"
set +a
test -n "${POSTGRES_DSN:-}"

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

protected_snapshot() {
  local output=$1
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atq \
    -v ON_ERROR_STOP=1 >"$output" <<'SQL'
SELECT 'claim',count(*),coalesce(max(created_at)::text,'')
FROM memory.claim
UNION ALL
SELECT 'entity',count(*),coalesce(max(created_at)::text,'')
FROM memory.entity
UNION ALL
SELECT 'observation',count(*),coalesce(max(created_at)::text,'')
FROM memory.observation
UNION ALL
SELECT 'packet',count(*),coalesce(max(created_at)::text,'')
FROM memory.evidence_extraction_packet_v5_local
UNION ALL
SELECT 'projection_outbox',count(*),coalesce(max(created_at)::text,'')
FROM memory.projection_outbox
ORDER BY 1;
SQL
}

qdrant_before=$(qdrant_signature)
docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc \
  | docker exec -i "$container" pg_restore -U sage -d "$clone_db"

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" \
  "$python_bin" - <<'PY'
from urllib.parse import urlsplit, urlunsplit
import os

source = urlsplit(os.environ["SOURCE_DSN"])
if source.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("source DSN scheme is invalid")
if source.hostname not in {"127.0.0.1", "::1", "localhost"}:
    raise SystemExit("source DSN is not loopback")
print(urlunsplit((
    source.scheme,
    source.netloc,
    "/" + os.environ["CLONE_DB"],
    source.query,
    source.fragment,
)))
PY
)

protected_snapshot "$protected_before"
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" "$python_bin" "$runner" \
  --owner-user-id "$owner" \
  --evidence-id "$parent" \
  --limit 1 \
  --report-path "$artifact_dir/dry.json" \
  >"$artifact_dir/dry.out"
plan_sha=$("$python_bin" -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["owners"][0]["plan_sha256"])' \
  "$artifact_dir/dry.json")

MEMORY_V1_CONTEXTUAL_EXACT_RESPLIT_APPLY=memory_v1_contextual_exact_resplit_apply_v3 \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" "$python_bin" "$runner" \
  --owner-user-id "$owner" \
  --evidence-id "$parent" \
  --limit 1 \
  --expected-plan-sha256 "$plan_sha" \
  --apply \
  --report-path "$artifact_dir/apply.json" \
  >"$artifact_dir/apply.out"

jq -e '
  .apply==true
  and .model_calls==0
  and .packet_writes==0
  and .claim_writes==0
  and .qdrant_writes==0
  and .prompt_influence==0
  and (.owners|length)==1
  and .owners[0].rows==1
  and .owners[0].outcomes=={"deferred":1}
  and .owners[0].reasons=={"contextual_split_required":1}
  and (.owners[0].contextual_splits|length)==1
  and .owners[0].contextual_splits[0].span_count==5
  and .owners[0].contextual_splits[0].context_needed==2
  and .owners[0].contextual_splits[0].apply=={
    "children_applied":5,
    "children_queued":5,
    "children_replayed":5,
    "context_needed":2,
    "parent_terminal_applied":1,
    "parent_terminal_replayed":1,
    "queue_replayed":5
  }
' "$artifact_dir/apply.json" >/dev/null

docker exec "$container" psql -U sage -d "$clone_db" -X -Atq \
  -v ON_ERROR_STOP=1 -F $'\t' -c "
    SELECT span.ordinal,span.child_evidence_id,evidence.content_sha256
    FROM memory.evidence_contextual_span_v2 AS span
    JOIN memory.evidence AS evidence
      ON evidence.owner_user_id=span.owner_user_id
     AND evidence.evidence_id=span.child_evidence_id
    WHERE span.owner_user_id='$owner'::uuid
      AND span.parent_evidence_id='$parent'::uuid
      AND span.splitter_version=
          'memory_v1_contextual_span_splitter_20260728_v3'
    ORDER BY span.ordinal;
  " >"$artifact_dir/children.tsv"
test "$(wc -l <"$artifact_dir/children.tsv")" = 5

test "$(
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND evidence_id IN (
        SELECT child_evidence_id
        FROM memory.evidence_contextual_span_v2
        WHERE owner_user_id='$owner'::uuid
          AND parent_evidence_id='$parent'::uuid
          AND splitter_version=
              'memory_v1_contextual_span_splitter_20260728_v3'
      )
      AND selector_version='$selector'
      AND status='pending'
      AND attempts=0;
  "
)" = 5

while IFS=$'\t' read -r _ordinal child _child_sha; do
  test "$(
    psql "$clone_dsn" -X -Atq -v ON_ERROR_STOP=1 <<SQL | tail -1
BEGIN;
SELECT set_config('app.user_id','$other_owner',true);
SELECT count(*)
FROM memory.plan_owner_evidence_intake_v2(
  '$selector',1,'$child'::uuid
);
ROLLBACK;
SQL
  )" = 0
done <"$artifact_dir/children.tsv"

for ordinal in 0 3 4; do
  IFS=$'\t' read -r _ordinal child child_sha < <(
    awk -F $'\t' -v wanted="$ordinal" '$1==wanted {print}' \
      "$artifact_dir/children.tsv"
  )
  packet="$artifact_dir/packet-$ordinal.json"
  MEMORY_V1_EVIDENCE_CONTEXT_CANARY=memory_v1_evidence_context_zero_write_canary_v1 \
  MEMORY_V1_LOCAL_INFERENCE_API_KEY="$(<"$api_key_file")" \
  POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" "$python_bin" "$canary" \
    --owner-user-id "$owner" \
    --target-evidence-id "$child" \
    --expected-target-content-sha256 "$child_sha" \
    --packet-output "$packet" \
    >"$artifact_dir/report-$ordinal.json"
done

"$python_bin" - "$artifact_dir" <<'PY'
from __future__ import annotations

import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
packets = {
    ordinal: json.loads((root / f"packet-{ordinal}.json").read_text())
    for ordinal in (0, 3, 4)
}
reports = {
    ordinal: json.loads((root / f"report-{ordinal}.json").read_text())
    for ordinal in (0, 3, 4)
}
for report in reports.values():
    assert report["outcome"] == "accepted"
    assert report["local_model_calls"] == 1
    assert report["external_model_calls"] == 0
    assert report["write_counts"] == {
        "database": 0,
        "prompt_influence": 0,
        "qdrant": 0,
        "retrieval": 0,
    }

def named_refs(packet: dict, name: str) -> set[str]:
    return {
        item["entity_ref"]
        for item in packet["entity_mentions"]
        if item.get("name_text") == name
    }

def death_observations(packet: dict) -> list[dict]:
    return [
        item
        for item in packet["observations"]
        if item["predicate"] == "life_event.died"
    ]

assert not death_observations(packets[0]), "Keasha acquired a false death event"
for ordinal, name, verb in (
    (3, "Dahlia", "lost"),
    (4, "Helsing", "died"),
):
    refs = named_refs(packets[ordinal], name)
    assert len(refs) == 1, f"{name} did not resolve to one named animal"
    deaths = death_observations(packets[ordinal])
    assert len(deaths) == 1, f"{name} did not receive exactly one death event"
    death = deaths[0]
    assert death["subject_entity_ref"] in refs
    quotes = [
        str(span.get("quote", "")).lower()
        for span in death.get("source_spans", [])
    ]
    assert any(verb in quote for quote in quotes)
    temporal = death["temporal"]
    assert temporal["basis"] in {"relative", "calendar"}
    assert temporal["shape"] in {"instant", "bounded_interval"}
    if temporal["source_form"] == "partial_absolute":
        assert temporal["anchored_to_source_time"] is True

print(json.dumps({
    "semantic_checks": {
        "keasha_false_death": 0,
        "dahlia_death": 1,
        "helsing_death": 1,
    },
    "local_model_calls": 3,
    "external_model_calls": 0,
    "writes": 0,
}, sort_keys=True, separators=(",", ":")))
PY

protected_snapshot "$protected_after"
cmp "$protected_before" "$protected_after"
qdrant_after=$(qdrant_signature)
test "$qdrant_before" = "$qdrant_after"

restore_timers
test "$(systemctl is-active brains.service)" = active
printf '%s\n' \
  'memory_v1_dahlia_helsing_contextual_resplit_clone: PASS' \
  'children=5 context_needed=2 local_model_calls=3 external_model_calls=0' \
  'claims=0 qdrant=0 prompt_influence=0 cross_owner_visible=0'
