#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo 'run through sudo' >&2
  exit 2
fi

repo=$(git -C "$(dirname "${BASH_SOURCE[0]}")/.." rev-parse --show-toplevel)
production_repo=/opt/chat-memory
container=brains-postgres-1
production=memory
clone="memory_v10_jerry_review_${$}"
work=$(mktemp -d /tmp/memory-v10-jerry-review.XXXXXX)
backup="$work/production.dump"
review_root="$work/reviews"
report="$review_root/jerry-manual-review.json"
spec="$repo/manifests/memory_v1_v5_2_jerry_manual_review_v1.json"
migration="$repo/ops/sql/20260731_memory_v1_v5_2_manual_packet_review_reader_v1.sql"
rollback="$repo/ops/sql/20260731_memory_v1_v5_2_manual_packet_review_reader_v1_rollback.sql"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
clone_created=0
stage=initialization
mkdir -p "$review_root"
chmod 0700 "$work" "$review_root"

cleanup() {
  rc=$?
  trap - EXIT
  if [[ "$rc" -ne 0 ]]; then
    printf 'FAILED_STAGE=%s\n' "$stage" >&2
  fi
  if [[ "$clone_created" -eq 1 ]]; then
    docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
      >/dev/null 2>&1 || rc=1
  fi
  rm -rf "$work"
  exit "$rc"
}
trap cleanup EXIT

scalar() {
  local database=$1 query=$2
  docker exec "$container" psql -U sage -d "$database" -X -Atqc "$query"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

protected_signature() {
  local database=$1
  scalar "$database" "
    SELECT encode(public.digest(convert_to(coalesce(string_agg(
      row_value::text,E'\\n' ORDER BY row_value::text
    ),''),'UTF8'),'sha256'),'hex')
    FROM (
      SELECT to_jsonb(value) AS row_value FROM memory.entity AS value
      WHERE owner_user_id='$owner'::uuid
      UNION ALL
      SELECT to_jsonb(value) FROM memory.observation AS value
      WHERE owner_user_id='$owner'::uuid
      UNION ALL
      SELECT to_jsonb(value) FROM memory.claim AS value
      WHERE owner_user_id='$owner'::uuid
      UNION ALL
      SELECT to_jsonb(value) FROM memory.observation_entity_binding AS value
      WHERE owner_user_id='$owner'::uuid
    ) AS protected"
}

test -s "$spec"
test -s "$migration"
test -s "$rollback"
test "$(git -C "$repo" status --short)" = ''
test "$(git -C "$production_repo" status --short)" = ''
test "$(systemctl is-active brains.service)" = active

stage=unit_tests
PYTHONPATH="$repo" /opt/chat-memory/venv/bin/python -m unittest -q \
  tests.test_memory_v1_v5_2_manual_packet_review_v1

stage=production_signatures
production_head=$(git -C "$production_repo" rev-parse HEAD)
production_protected_before=$(protected_signature "$production")
production_qdrant_before=$(qdrant_signature)

stage=clone_backup
docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
test -s "$backup"
docker exec "$container" createdb -U sage -T template0 "$clone"
clone_created=1
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

stage=clone_dsn
set -a
source "$production_repo/.env"
set +a
clone_dsn=$(
  /opt/chat-memory/venv/bin/python - "$POSTGRES_DSN" "$clone" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit

parts = urlsplit(sys.argv[1])
if parts.hostname not in {"127.0.0.1", "::1", "localhost"}:
    raise SystemExit("production DSN is not loopback")
print(urlunsplit((parts.scheme, parts.netloc, "/" + sys.argv[2], parts.query, "")))
PY
)

stage=review_execution
clone_protected_before=$(protected_signature "$clone")
test "$(scalar "$clone" "SELECT has_function_privilege(
  'brains_app','memory.plan_owner_v5_2_manual_packet_review_v1(uuid,uuid,uuid)',
  'EXECUTE')::integer")" -eq 1
test "$(scalar "$clone" "SELECT count(*) FROM pg_proc AS p
  JOIN pg_namespace AS n ON n.oid=p.pronamespace
  CROSS JOIN LATERAL aclexplode(
    coalesce(p.proacl,acldefault('f',p.proowner))
  ) AS privilege
  WHERE n.nspname='memory'
    AND p.proname='plan_owner_v5_2_manual_packet_review_v1'
    AND privilege.grantee=0
    AND privilege.privilege_type='EXECUTE'")" -eq 0
test "$(scalar "$clone" "SELECT count(*) FROM pg_policies
  WHERE policyname='manual_packet_review_owner_select_v1'")" -eq 6
set +e
output=$(POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" \
  /opt/chat-memory/venv/bin/python \
  "$repo/scripts/memory_v1_v5_2_manual_packet_review_v1.py" \
  --spec "$spec" --output "$report")
review_rc=$?
set -e
if [[ "$review_rc" -ne 0 ]]; then
  printf '%s\n' "$output" >&2
  exit "$review_rc"
fi

stage=report_assertions
test "$(jq -r '.status' <<<"$output")" = reviewed
test "$(jq -r '.disposition' <<<"$output")" = corrected_split_required
test "$(jq -r '.database_writes' <<<"$output")" -eq 0
test "$(jq -r '.qdrant_writes' <<<"$output")" -eq 0
test "$(jq -r '.model_calls' <<<"$output")" -eq 0
test "$(jq -r '.prompt_influence' <<<"$output")" -eq 0
test "$(jq -r '.contract_version' "$report")" = \
  memory_v1_v5_2_manual_packet_review_report_v1
test "$(jq -r '.review.entity_decisions[0].decision' "$report")" = \
  approve_named_role_resolution
test "$(jq -r '.review.entity_decisions[1].decision' "$report")" = \
  hold_generic_place_identity
test "$(jq -r '.review.observation_decisions[0].decision' "$report")" = \
  hold_for_object_and_temporal_correction
test "$(jq -r '.review.observation_decisions[1].decision' "$report")" = \
  approve_restricted_user_report
test "$(jq -r '.review.observation_decisions[2].decision' "$report")" = \
  hold_for_approximation_and_temporal_comparison
test "$(jq -r '.review.corrected_duration_approximate' "$report")" = true
test "$(jq -r '.review.corrected_temporal_anchor' "$report")" = \
  2026-07-30T21:39:53.840736Z

stage=protected_store_assertions
test "$(protected_signature "$clone")" = "$clone_protected_before"
test "$(protected_signature "$production")" = "$production_protected_before"
test "$(qdrant_signature)" = "$production_qdrant_before"
test "$(git -C "$production_repo" rev-parse HEAD)" = "$production_head"
test "$(git -C "$production_repo" status --short)" = ''

stage=rollback_assertions
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$rollback" >/dev/null
test "$(scalar "$clone" "SELECT to_regprocedure(
  'memory.plan_owner_v5_2_manual_packet_review_v1(uuid,uuid,uuid)') IS NULL")" = t
test "$(scalar "$clone" "SELECT to_regrole(
  'memory_v5_manual_packet_review_reader') IS NULL")" = t

printf '%s\n' "$output"
