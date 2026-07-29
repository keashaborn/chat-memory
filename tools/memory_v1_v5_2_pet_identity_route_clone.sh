#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Tests exactly two append-only packet supersessions and
# three exact review routes in a disposable production clone.

repo=${REPO_ROOT:-/tmp/chat-memory-pet-temporal-integration-v2}
production_repo=/opt/chat-memory
container=brains-postgres-1
source_db=memory
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=673d64a3-c4ba-4d1c-89e3-e0c579022fad
migration=$repo/ops/sql/20260729_memory_v1_v5_2_pet_identity_packet_supersession.sql
manifest=$repo/manifests/memory_v1_v5_2_pet_identity_exact_route_20260729.json
route_worker=$repo/scripts/memory_v1_v5_2_exact_route_batch.py
clone_db="memory_pet_identity_route_$(date -u +%Y%m%dT%H%M%SZ)_$$"
work=$(mktemp -d /tmp/memory-pet-identity-route.XXXXXX)
backup=$work/source.dump
review_root=$work/reviews
timer_state=$work/timers.tsv
mkdir -m 0700 "$review_root"
: >"$backup"
chmod 0600 "$backup"
timers_restored=0
stage=preflight

prior_packets=(
  adc8ecf7-63bd-5dc0-8283-0c2f765893c5
  3b44e557-2910-510c-ac8a-707ef92a398d
)
replacement_packets=(
  f7bd7d42-9241-562c-9150-014d5c5f762f
  4f091b92-3db2-5c44-8676-7a2cdc989d01
)
prior_hashes=(
  5454b320db178148ca5832108cd84632180d7ca3329b88dd7a03bb9009689d11
  df60115074ecfa66de0e3efa767f7a53448db4bc932cd49b9c65afca36d1f039
)
replacement_hashes=(
  27e970beef702d8e451dda81a437192395f6ef5aaa549f006b681712e3b2c461
  973dbda42fdda807505fef2f51389b740e1f39950b2a0b8a686055c296ab17c2
)
operations=(
  775a9b00-73e4-53b4-a5f2-69db59593823
  4745d633-6218-541a-a744-5cdebd1f8e35
)
supersessions=(
  6b9dbb1c-8700-5c99-a09a-67ff71ecf4cd
  2fa76c50-a700-5682-aa54-b9b9e01db157
)
reason=pet_identity_semantics_reextracted

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
    test "$(systemctl is-enabled "$unit")" = "$enabled"
    test "$(systemctl is-active "$unit")" = "$active"
  done <"$timer_state"
  timers_restored=1
}

cleanup() {
  rc=$?
  trap - EXIT
  restore_timers || rc=1
  docker exec "$container" dropdb -U sage --if-exists --force "$clone_db" \
    >/dev/null 2>&1 || true
  rm -rf "$work"
  if [[ "$rc" != 0 ]]; then
    printf 'memory_v1_v5_2_pet_identity_route_clone: FAIL stage=%s rc=%s\n' \
      "$stage" "$rc" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT

test "$(id -u)" -eq 0
test -z "$(git -C "$production_repo" status --porcelain)"
test -z "$(git -C "$repo" status --porcelain)"
test -r "$migration"
test -r "$manifest"
test -r "$route_worker"
test "$(systemctl is-active brains.service)" = active
jq -e '
  .expected_counts.packets==3 and
  .expected_counts.manual_review_routes==3 and
  .expected_counts.route_events==3 and
  .expected_counts.restricted_review_artifacts==6 and
  .expected_counts.model_calls==0
' "$manifest" >/dev/null

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
test "$(
  docker exec "$container" psql -U sage -d "$source_db" -X -Atqc "
    SELECT count(*)
    FROM memory.evidence_extraction_job
    WHERE lease_token IS NOT NULL
      AND lease_expires_at > clock_timestamp();
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
  local database=$1
  local output=$2
  local exclusions=$3
  docker exec -i "$container" psql -X -U sage -d "$database" \
    -Atq -v ON_ERROR_STOP=1 -v exclusions="$exclusions" >"$output" <<'SQL'
CREATE TEMP TABLE protected_state(
  table_name text PRIMARY KEY,
  row_count bigint NOT NULL,
  content_sha256 text NOT NULL
);
DO $snapshot$
DECLARE
  item record;
  item_count bigint;
  item_sha text;
  excluded text[] := string_to_array(
    current_setting('psql.exclusions',true),','
  );
BEGIN
  FOR item IN
    SELECT namespace.nspname AS schema_name,class.relname AS table_name
    FROM pg_class AS class
    JOIN pg_namespace AS namespace ON namespace.oid=class.relnamespace
    WHERE namespace.nspname='memory'
      AND class.relkind IN ('r','p')
      AND NOT class.relname=ANY(coalesce(excluded,ARRAY[]::text[]))
    ORDER BY class.relname
  LOOP
    EXECUTE format('SELECT count(*) FROM %I.%I',
      item.schema_name,item.table_name) INTO item_count;
    EXECUTE format(
      $format$
      SELECT encode(public.digest(convert_to(coalesce(
        jsonb_agg(to_jsonb(value) ORDER BY to_jsonb(value)::text)::text,
        '[]'),'UTF8'),'sha256'),'hex')
      FROM %I.%I AS value
      $format$,
      item.schema_name,item.table_name
    ) INTO item_sha;
    INSERT INTO protected_state VALUES(
      item.table_name,item_count,item_sha
    );
  END LOOP;
END
$snapshot$;
SELECT table_name||E'\t'||row_count||E'\t'||content_sha256
FROM protected_state ORDER BY table_name;
SQL
}

# Pass the exclusion list through a transaction-local setting because the
# snapshot body is intentionally static SQL.
snapshot_with_exclusions() {
  local database=$1
  local output=$2
  local exclusions=$3
  docker exec -i "$container" psql -X -U sage -d "$database" \
    -Atq -v ON_ERROR_STOP=1 >"$output" <<SQL
BEGIN;
SELECT set_config('psql.exclusions','$exclusions',true);
CREATE TEMP TABLE protected_state(
  table_name text PRIMARY KEY,
  row_count bigint NOT NULL,
  content_sha256 text NOT NULL
);
DO \$snapshot\$
DECLARE
  item record;
  item_count bigint;
  item_sha text;
  excluded text[] := string_to_array(
    current_setting('psql.exclusions'),','
  );
BEGIN
  FOR item IN
    SELECT namespace.nspname AS schema_name,class.relname AS table_name
    FROM pg_class AS class
    JOIN pg_namespace AS namespace ON namespace.oid=class.relnamespace
    WHERE namespace.nspname='memory'
      AND class.relkind IN ('r','p')
      AND NOT class.relname=ANY(excluded)
    ORDER BY class.relname
  LOOP
    EXECUTE format('SELECT count(*) FROM %I.%I',
      item.schema_name,item.table_name) INTO item_count;
    EXECUTE format(
      'SELECT encode(public.digest(convert_to(coalesce('
      ||'jsonb_agg(to_jsonb(value) ORDER BY to_jsonb(value)::text)::text,'
      ||'''[]''),''UTF8''),''sha256''),''hex'') FROM %I.%I AS value',
      item.schema_name,item.table_name
    ) INTO item_sha;
    INSERT INTO protected_state VALUES(
      item.table_name,item_count,item_sha
    );
  END LOOP;
END
\$snapshot\$;
SELECT table_name||E'\\t'||row_count||E'\\t'||content_sha256
FROM protected_state ORDER BY table_name;
ROLLBACK;
SQL
}

stage=clone_create
qdrant_before=$(qdrant_signature)
snapshot_with_exclusions "$source_db" "$work/production-before.tsv" \
  "__none__"
docker exec "$container" pg_dump -U sage -d "$source_db" -Fc >"$backup"
test -s "$backup"
docker exec "$container" createdb -U sage -T template0 "$clone_db"
docker exec -i "$container" pg_restore -U sage -d "$clone_db" <"$backup"
docker exec -i "$container" psql -U sage -d "$clone_db" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone_db" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone_db" \
  /opt/chat-memory/venv/bin/python - <<'PY'
from urllib.parse import urlsplit, urlunsplit
import os
source = urlsplit(os.environ["SOURCE_DSN"])
if source.scheme not in {"postgres","postgresql"}:
    raise SystemExit("source DSN scheme is invalid")
if source.hostname not in {"127.0.0.1","::1","localhost"}:
    raise SystemExit("source DSN must be loopback")
print(urlunsplit((
    source.scheme,source.netloc,"/"+os.environ["CLONE_DB"],
    source.query,source.fragment,
)))
PY
)

stage=plan_and_isolation
for index in 0 1; do
  plan=$(psql "$clone_dsn" -X -Atq -F '|' -v ON_ERROR_STOP=1 <<SQL | tail -1
BEGIN READ ONLY;
SELECT set_config('app.user_id','$owner',true);
SELECT prior_packet_id,replacement_packet_id,
       prior_packet_storage_sha256,replacement_packet_storage_sha256,
       reason_code
FROM memory.plan_owner_v5_2_pet_identity_packet_supersession_v1(
  '${prior_packets[$index]}'::uuid,
  '${replacement_packets[$index]}'::uuid
);
ROLLBACK;
SQL
)
  test "$plan" = "${prior_packets[$index]}|${replacement_packets[$index]}|${prior_hashes[$index]}|${replacement_hashes[$index]}|$reason"
  foreign=$(psql "$clone_dsn" -X -Atq -v ON_ERROR_STOP=1 <<SQL | tail -1
BEGIN READ ONLY;
SELECT set_config('app.user_id','$other_owner',true);
SELECT count(*)
FROM memory.plan_owner_v5_2_pet_identity_packet_supersession_v1(
  '${prior_packets[$index]}'::uuid,
  '${replacement_packets[$index]}'::uuid
);
ROLLBACK;
SQL
)
  test "$foreign" = 0
done

snapshot_with_exclusions "$clone_db" "$work/clone-before.tsv" \
  "v5_local_packet_supersession,v5_2_local_packet_route_event,v5_local_packet_review_artifact"
supersession_before=$(docker exec "$container" psql -U sage -d "$clone_db" \
  -X -Atqc "SELECT count(*) FROM memory.v5_local_packet_supersession")
route_before=$(docker exec "$container" psql -U sage -d "$clone_db" \
  -X -Atqc "SELECT count(*) FROM memory.v5_2_local_packet_route_event")
review_before=$(docker exec "$container" psql -U sage -d "$clone_db" \
  -X -Atqc "SELECT count(*) FROM memory.v5_local_packet_review_artifact")

stage=supersession_apply
apply_sql="BEGIN;
SELECT set_config('app.user_id','$owner',true);"
for index in 0 1; do
  apply_sql+="
SELECT apply_outcome
FROM memory.finalize_owner_v5_2_pet_identity_packet_supersession_v1(
  '${operations[$index]}'::uuid,
  '${supersessions[$index]}'::uuid,
  '${prior_packets[$index]}'::uuid,
  '${replacement_packets[$index]}'::uuid,
  '${prior_hashes[$index]}',
  '${replacement_hashes[$index]}',
  '$reason'
);"
done
apply_sql+="
COMMIT;"
mapfile -t applied < <(
  psql "$clone_dsn" -X -Atq -v ON_ERROR_STOP=1 -c "$apply_sql" \
    | grep -E '^(applied|replayed)$'
)
test "${#applied[@]}" = 2
test "${applied[0]}" = applied
test "${applied[1]}" = applied

replay_sql=${apply_sql/BEGIN;/BEGIN;}
mapfile -t replayed < <(
  psql "$clone_dsn" -X -Atq -v ON_ERROR_STOP=1 -c "$replay_sql" \
    | grep -E '^(applied|replayed)$'
)
test "${#replayed[@]}" = 2
test "${replayed[0]}" = replayed
test "${replayed[1]}" = replayed

stage=route_dry_run
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" \
  /opt/chat-memory/venv/bin/python "$route_worker" \
    --manifest "$manifest" \
    --review-root "$review_root" \
    --other-owner-user-id "$other_owner" \
    >"$work/route-dry.json"
jq -e '
  .apply==false and .packet_count==3 and
  .route_counts.manual_review_routes==3 and
  .database_writes==0 and .filesystem_writes==0 and
  .stage_writes==0 and .claim_writes==0 and .qdrant_writes==0 and
  .external_model_calls==0 and .prompt_influence==0
' "$work/route-dry.json" >/dev/null

stage=route_apply
MEMORY_V1_V5_2_EXACT_ROUTE_BATCH_APPLY=memory_v1_v5_2_exact_route_batch_apply_v1 \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo" \
  /opt/chat-memory/venv/bin/python "$route_worker" \
    --manifest "$manifest" \
    --review-root "$review_root" \
    --other-owner-user-id "$other_owner" \
    --apply >"$work/route-apply.json"
jq -e '
  .apply==true and .outcome=="exact_routes_applied" and
  .packet_count==3 and .route_counts.manual_review_routes==3 and
  .write_counts.route_events==3 and
  .write_counts.restricted_review_artifacts==6 and
  .transactional_apply_proved==true and
  .zero_write_replay_proved==true and .cross_owner_visible==0 and
  .write_counts.stage==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .write_counts.prompt_influence==0 and
  .external_model_calls==0
' "$work/route-apply.json" >/dev/null

stage=postconditions
test "$(
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atqc "
    SELECT count(*) FROM memory.v5_local_packet_supersession
    WHERE owner_user_id='$owner'::uuid
      AND prior_packet_id=ANY(ARRAY[
        '${prior_packets[0]}'::uuid,'${prior_packets[1]}'::uuid
      ]);
  "
)" = 2
test "$(
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atqc "
    SELECT count(*) FROM memory.v5_2_local_packet_route_event
    WHERE owner_user_id='$owner'::uuid
      AND packet_id=ANY(ARRAY[
        '${replacement_packets[0]}'::uuid,
        '${replacement_packets[1]}'::uuid,
        'afe46ab5-6243-5568-bc71-b23772008c63'::uuid
      ])
      AND route='manual_review_artifact_ready';
  "
)" = 3
test "$(
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atqc "
    SELECT count(*) FROM memory.v5_2_local_packet_route_event
    WHERE owner_user_id='$owner'::uuid
      AND packet_id=ANY(ARRAY[
        '354c4cd6-64c5-598b-8f89-4b14fe159b2a'::uuid,
        '7769d626-0ddb-573f-a017-dfdb2c2971cf'::uuid
      ])
      AND route='terminal_no_stage';
  "
)" = 2
test "$(
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atqc "
    SELECT count(*) FROM memory.v5_local_packet_supersession
  "
)" = "$((supersession_before + 2))"
test "$(
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atqc "
    SELECT count(*) FROM memory.v5_2_local_packet_route_event
  "
)" = "$((route_before + 3))"
test "$(
  docker exec "$container" psql -U sage -d "$clone_db" -X -Atqc "
    SELECT count(*) FROM memory.v5_local_packet_review_artifact
  "
)" = "$((review_before + 3))"
for prior in "${prior_packets[@]}"; do
  test "$(
    psql "$clone_dsn" -X -Atq -v ON_ERROR_STOP=1 <<SQL | tail -1
BEGIN READ ONLY;
SELECT set_config('app.user_id','$owner',true);
SELECT count(*) FROM memory.plan_owner_v5_2_exact_packet_route_v1(
  '$prior'::uuid
);
ROLLBACK;
SQL
  )" = 0
done

snapshot_with_exclusions "$clone_db" "$work/clone-after.tsv" \
  "v5_local_packet_supersession,v5_2_local_packet_route_event,v5_local_packet_review_artifact"
cmp "$work/clone-before.tsv" "$work/clone-after.tsv"
snapshot_with_exclusions "$source_db" "$work/production-after.tsv" \
  "__none__"
cmp "$work/production-before.tsv" "$work/production-after.tsv"
test "$(qdrant_signature)" = "$qdrant_before"

stage=restore_timers
restore_timers
stage=complete
printf '%s\n' \
  'memory_v1_v5_2_pet_identity_route_clone: PASS' \
  'supersessions=2 routes=3 terminal_fragments=2 replay_writes=0' \
  'cross_owner_visible=0 claims=0 staging=0 qdrant=0 prompt_influence=0'
