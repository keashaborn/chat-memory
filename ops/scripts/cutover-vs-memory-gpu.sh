#!/usr/bin/env bash
set -euo pipefail

direct_unit="/run/vs-memory-gpu-cutover/memory-v1-v5-local-inference-tunnel.service"
production_unit="/etc/systemd/system/memory-v1-v5-local-inference-tunnel.service"
new_key="/etc/memory-v1-local-inference/vs_memory_gpu_api_key"
production_key="/etc/memory-v1-local-inference/api-key"
health_timer="memory-v1-v5-local-inference-health.timer"
scheduler_timer="memory-v1-v5-local-inference-scheduler.timer"
health_service="memory-v1-v5-local-inference-health.service"
scheduler_service="memory-v1-v5-local-inference-scheduler.service"
tunnel_service="memory-v1-v5-local-inference-tunnel.service"
backup_root="/var/backups/vs-memory-gpu-cutover"
backup_dir="${backup_root}/$(date -u +%Y%m%dT%H%M%SZ)"
cutover_complete=0
health_timer_was_active=0
scheduler_timer_was_active=0

require_file() {
  test -f "$1" || { printf 'missing required file: %s\n' "$1" >&2; exit 1; }
}

restore_timers() {
  if (( health_timer_was_active )); then systemctl start "$health_timer"; fi
  if (( scheduler_timer_was_active )); then systemctl start "$scheduler_timer"; fi
}

rollback() {
  status=$?
  if (( cutover_complete == 0 )) && test -d "$backup_dir"; then
    printf 'cutover failed; restoring previous tunnel and credential\n' >&2
    systemctl stop "$tunnel_service" || true
    install -o root -g root -m 0644 "$backup_dir/tunnel.service" "$production_unit"
    install -o root -g root -m 0600 "$backup_dir/api-key" "$production_key"
    systemctl daemon-reload
    systemctl start "$tunnel_service" || true
  fi
  restore_timers
  exit "$status"
}
trap rollback EXIT

test "$(id -u)" = 0
require_file "$direct_unit"
require_file "$production_unit"
require_file "$new_key"
require_file "$production_key"

systemctl is-active --quiet "$health_timer" && health_timer_was_active=1
systemctl is-active --quiet "$scheduler_timer" && scheduler_timer_was_active=1
systemctl stop "$health_timer" "$scheduler_timer"

if systemctl is-active --quiet "$health_service" || \
   systemctl is-active --quiet "$scheduler_service"; then
  printf 'an inference job is active; refusing cutover\n' >&2
  exit 1
fi

install -d -o root -g root -m 0700 "$backup_dir"
install -o root -g root -m 0600 "$production_unit" "$backup_dir/tunnel.service"
install -o root -g root -m 0600 "$production_key" "$backup_dir/api-key"
systemctl cat "$health_timer" "$scheduler_timer" > "$backup_dir/timers.txt"
chmod 0600 "$backup_dir/timers.txt"

systemctl stop "$tunnel_service"
install -o root -g root -m 0644 "$direct_unit" "$production_unit"
install -o root -g root -m 0600 "$new_key" "$production_key"
systemctl daemon-reload
systemctl start "$tunnel_service"

listener_ready=0
for _ in $(seq 1 20); do
  if systemctl is-active --quiet "$tunnel_service" && \
     ss -ltn | grep -q '127.0.0.1:18080'; then
    listener_ready=1
    break
  fi
  sleep 0.5
done
test "$listener_ready" = 1
systemctl start "$health_service"
/usr/local/sbin/test-vs-memory-gpu-canary \
  http://127.0.0.1:18080 \
  "$production_key"

cutover_complete=1
restore_timers
trap - EXIT

printf 'cutover=complete backup=%s tunnel=%s health_timer=%s scheduler_timer=%s\n' \
  "$backup_dir" \
  "$(systemctl is-active "$tunnel_service")" \
  "$(systemctl is-active "$health_timer")" \
  "$(systemctl is-active "$scheduler_timer")"
