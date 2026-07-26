#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:-/opt/chat-memory}"
output="${2:?output path is required}"
owner="1240822d-ac9a-4096-95aa-e2b24d36ef50"
python_bin="/opt/chat-memory/venv/bin/python"
probe="scripts/memory_v1_v5_2_evidence_context_stance_typed_shadow_probe.py"

cd "$repo_root"
set -a
source .env
set +a

test -x "$python_bin"
test -f "$probe"
test -n "${POSTGRES_DSN:-}"
test -n "${QDRANT_URL:-}"
test "$(git status --short | wc -l | tr -d ' ')" = "0"
test "$(systemctl is-active brains.service)" = "active"

before_head="$(git rev-parse HEAD)"
before_status="$(git status --porcelain=v1)"
before_timers="$(
  systemctl list-timers --all --no-legend \
    | awk '$1 ~ /^memory-v1-/ {print $1 "|" $2 "|" $3 "|" $4 "|" $5}' \
    | sort
)"

umask 077
PYTHONPATH="$repo_root" "$python_bin" "$probe" \
  --owner-user-id "$owner" \
  >"$output"

OUTPUT="$output" "$python_bin" - <<'PY'
import json
import os

with open(os.environ["OUTPUT"], encoding="utf-8") as handle:
    report = json.load(handle)
assert report["status"] == "pass"
assert report["target_claim_present"] is True
assert report["memory_included"] is True
assert report["memory_estimated_tokens"] > 0
assert report["memory_binding"] == "shadow_only_not_bound_to_answer"
assert report["intent_domain"] == "stance_recall"
assert report["predicate_policy"] == ["stance.reported"]
assert report["other_owner_target_absent"] is True
assert report["database_transaction_read_only"] is True
assert report["database_writes"] == 0
assert report["qdrant_writes"] == 0
assert report["external_model_calls"] == 0
assert report["answer_model_calls"] == 0
assert report["live_prompt_influence"] is False
assert report["qdrant_unchanged"] is True
PY

after_head="$(git rev-parse HEAD)"
after_status="$(git status --porcelain=v1)"
after_timers="$(
  systemctl list-timers --all --no-legend \
    | awk '$1 ~ /^memory-v1-/ {print $1 "|" $2 "|" $3 "|" $4 "|" $5}' \
    | sort
)"

test "$before_head" = "$after_head"
test "$before_status" = "$after_status"
test "$before_timers" = "$after_timers"
test "$(systemctl is-active brains.service)" = "active"

printf 'PASS output=%s sha256=%s head=%s\n' \
  "$output" \
  "$(sha256sum "$output" | awk '{print $1}')" \
  "$after_head"
