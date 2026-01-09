#!/usr/bin/env bash
set -euo pipefail

# Simple nightly consolidation across all distinct user_ids in memory_raw.
# Skips 'guest' and obvious test ids.

BASE_DIR="/opt/chat-memory"
VENV_BIN="$BASE_DIR/venv/bin"
LOG_FILE="$BASE_DIR/logs/eval_all_users.log"

cd "$BASE_DIR"

ts() { date +"%Y-%m-%d %H:%M:%S"; }

echo "$(ts) --- eval_all_users.sh starting ---" >> "$LOG_FILE"

# 1) Get distinct user_ids from memory_raw
USER_IDS=$(
  curl -s \
    -X POST "http://127.0.0.1:6333/collections/memory_raw/points/scroll" \
    -H "Content-Type: application/json" \
    -d '{"limit":10000, "with_payload": true}' \
  | jq -r '.result.points[].payload.user_id // empty' \
  | sort -u \
  | grep -v '^guest$' \
  | grep -v '^eric-test$'
)

if [ -z "$USER_IDS" ]; then
  echo "$(ts) no user_ids found; nothing to do" >> "$LOG_FILE"
  exit 0
fi

# 2) Activate venv
source "$VENV_BIN/activate"

# 3) Run eval_user_memory.py for each user_id
for UID in $USER_IDS; do
  echo "$(ts) consolidating user_id=$UID" >> "$LOG_FILE"
  python3 eval_user_memory.py "$UID" >> "$LOG_FILE" 2>&1 || \
    echo "$(ts) eval_user_memory failed for user_id=$UID" >> "$LOG_FILE"
done

echo "$(ts) --- eval_all_users.sh complete ---" >> "$LOG_FILE"
