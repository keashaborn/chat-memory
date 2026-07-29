#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Restores production into a disposable clone, atom-admits
# only the reviewed Dahlia and Helsing projections, stages those projections
# plus the deferral-free Keasha packet, and records three manual entity reviews.
# It never applies an entity, creates a claim, or changes Qdrant/retrieval/prompts.

if [[ "$EUID" -ne 0 ]]; then
  echo 'run through sudo; root is required for the disposable clone' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
port=${MEMORY_V1_V5_2_PET_STAGE_REVIEW_CLONE_PORT:-55507}
export MEMORY_V1_STAGE_BATCH_CLONE_PORT="$port"
compose=(
  docker compose
  -p memoryv1v52petstagereviewclone
  -f docker-compose.ci.yml
  -f docker-compose.stage-batch-clone.yml
)
stage_runner=scripts/memory_v1_v5_2_stage_batch.py
stage_fixture=tests/memory_v1_v5_2_stage_batch_fixture.py
review_runner=scripts/memory_v1_v5_2_entity_resolution_review_batch.py
review_spec=manifests/memory_v1_v5_2_pet_identity_entity_review_spec_20260729.json
atom_manifest_runner=scripts/memory_v1_v5_2_atom_admission_manifest_batch.py
atom_apply_runner=scripts/memory_v1_v5_2_atom_admission_apply_v2.py
atom_stage_runner=scripts/memory_v1_v5_2_atom_stage_bundle_v2.py
atom_spec=manifests/memory_v1_v5_2_pet_identity_atom_admission_spec_20260729.json
temporal_migration=ops/sql/20260729_memory_v1_v5_2_reviewed_temporal_atom_compat.sql
temporal_runner=scripts/memory_v1_v5_2_temporal_review_registration.py
temporal_spec=manifests/memory_v1_v5_2_pet_temporal_review_spec_20260729.json
source_stage_manifest=manifests/memory_v1_v5_2_pet_identity_relational_stage_20260729.json
backup=$(mktemp /tmp/memory-v1-v5-2-pet-stage-review.XXXXXX.dump)
role_sql=$(mktemp /tmp/memory-v1-v5-2-pet-stage-review-roles.XXXXXX.sql)
review_root=/home/ubuntu/memory-v1-reviews
work=$(runuser -u ubuntu -- mktemp -d "$review_root/pet-stage-review-clone.XXXXXX")
dsn="postgresql://brains_app:clone_only_brains_password@127.0.0.1:${port}/memory"
target_owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other_owner=673d64a3-c4ba-4d1c-89e3-e0c579022fad
keasha_evidence=c5d6f5cf-c6d6-554c-85d9-02150e8b8ae7
dahlia_evidence=2be95051-30c8-5f87-8ff9-e0999600b447
helsing_evidence=88161526-0c53-5291-8601-0bd0ba41da53
evidence_sql="'$keasha_evidence'::uuid,'$dahlia_evidence'::uuid,'$helsing_evidence'::uuid"
atom_packet_sql="'4f091b92-3db2-5c44-8676-7a2cdc989d01'::uuid,'afe46ab5-6243-5568-bc71-b23772008c63'::uuid"

cleanup() {
  "${compose[@]}" down -v >/dev/null 2>&1 || true
  rm -f "$backup" "$role_sql"
  rm -rf "$work"
}
trap cleanup EXIT
chmod 0600 "$backup" "$role_sql"
chmod 0700 "$work"

run_sql() {
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U sage -d memory "$@"
}

scalar() {
  "${compose[@]}" exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "$1"
}

assert_equal() {
  local label=$1 actual=$2 expected=$3
  if [[ "$actual" != "$expected" ]]; then
    printf 'ASSERTION_FAILED=%s\nexpected=%s\nactual=%s\n' \
      "$label" "$expected" "$actual" >&2
    exit 1
  fi
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

production_signature() {
  docker exec brains-postgres-1 psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d memory -c "
      SELECT encode(public.digest(convert_to(
        coalesce(string_agg(value,E'\\n' ORDER BY value),''),
        'UTF8'),'sha256'),'hex')
      FROM (
        SELECT table_name || ':' || count(*)::text AS value
        FROM information_schema.tables
        WHERE table_schema='memory' AND table_type='BASE TABLE'
        GROUP BY table_name
      ) AS counts"
}

target_counts() {
  scalar "
    SELECT concat_ws(',',
      (SELECT count(*) FROM memory.relational_stage_batch
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id IN ($evidence_sql)),
      (SELECT count(*) FROM memory.entity_mention
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id IN ($evidence_sql)),
      (SELECT count(*) FROM memory.entity_resolution_plan
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id IN ($evidence_sql)),
      (SELECT count(*) FROM memory.entity_resolution_candidate AS candidate
       JOIN memory.entity_resolution_plan AS plan
         ON plan.owner_user_id=candidate.owner_user_id
        AND plan.resolution_id=candidate.resolution_id
       WHERE plan.owner_user_id='$target_owner'::uuid
         AND plan.evidence_id IN ($evidence_sql)),
      (SELECT count(*) FROM memory.observation
       WHERE owner_user_id='$target_owner'::uuid
         AND evidence_id IN ($evidence_sql)),
      (SELECT count(*) FROM memory.observation_temporal AS temporal
       JOIN memory.observation AS observation
         ON observation.owner_user_id=temporal.owner_user_id
        AND observation.observation_id=temporal.observation_id
       WHERE observation.owner_user_id='$target_owner'::uuid
         AND observation.evidence_id IN ($evidence_sql)),
      (SELECT count(*) FROM memory.entity_resolution_review AS review
       JOIN memory.entity_resolution_plan AS plan
         ON plan.owner_user_id=review.owner_user_id
        AND plan.resolution_id=review.resolution_id
       WHERE plan.owner_user_id='$target_owner'::uuid
         AND plan.evidence_id IN ($evidence_sql)),
      (SELECT count(*) FROM memory.entity_resolution_apply AS applied
       JOIN memory.entity_resolution_plan AS plan
         ON plan.owner_user_id=applied.owner_user_id
        AND plan.resolution_id=applied.resolution_id
       WHERE plan.owner_user_id='$target_owner'::uuid
         AND plan.evidence_id IN ($evidence_sql)),
      (SELECT count(*) FROM memory.relational_operation_request
       WHERE owner_user_id='$target_owner'::uuid
         AND target_key IN (
           SELECT evidence_id::text FROM memory.relational_stage_batch
           WHERE owner_user_id='$target_owner'::uuid
             AND evidence_id IN ($evidence_sql)
           UNION
           SELECT resolution_id::text FROM memory.entity_resolution_plan
           WHERE owner_user_id='$target_owner'::uuid
             AND evidence_id IN ($evidence_sql)
         ))
    )"
}

atom_counts() {
  scalar "
    SELECT concat_ws(',',
      (SELECT count(*) FROM memory.v5_2_atom_admission_proposal
       WHERE owner_user_id='$target_owner'::uuid
         AND packet_id IN ($atom_packet_sql)),
      (SELECT count(*) FROM memory.v5_2_atom_admission_review AS review
       JOIN memory.v5_2_atom_admission_proposal AS proposal
         USING(owner_user_id,proposal_id)
       WHERE proposal.owner_user_id='$target_owner'::uuid
         AND proposal.packet_id IN ($atom_packet_sql)),
      (SELECT count(*) FROM memory.v5_2_atom_admission_apply
       WHERE owner_user_id='$target_owner'::uuid
         AND packet_id IN ($atom_packet_sql)),
      (SELECT count(*) FROM memory.v5_2_atom_admission_operation
       WHERE owner_user_id='$target_owner'::uuid
         AND operation_id IN (
           SELECT operation_id
           FROM memory.v5_2_atom_admission_proposal
           WHERE owner_user_id='$target_owner'::uuid
             AND packet_id IN ($atom_packet_sql)
           UNION ALL
           SELECT review.operation_id
           FROM memory.v5_2_atom_admission_review AS review
           JOIN memory.v5_2_atom_admission_proposal AS proposal
             USING(owner_user_id,proposal_id)
           WHERE proposal.owner_user_id='$target_owner'::uuid
             AND proposal.packet_id IN ($atom_packet_sql)
           UNION ALL
           SELECT operation_id
           FROM memory.v5_2_atom_admission_apply
           WHERE owner_user_id='$target_owner'::uuid
             AND packet_id IN ($atom_packet_sql)
         ))
    )"
}

owner_atom_stage_plan() {
  local apply_id=$1
  runuser -u ubuntu -- env PGPASSWORD=clone_only_brains_password \
    psql "$dsn" -X -q -A -t -v ON_ERROR_STOP=1 -c "
      SELECT memory.plan_owner_v5_2_atom_stage_v2('$apply_id'::uuid)
      FROM (
        SELECT set_config('app.user_id','$target_owner',true)
      ) AS owner_scope"
}

make_atom_stage_manifest() {
  local case_id=$1 output=$2
  local admission_item apply_id evidence_id stage_plan resolution
  admission_item=$(jq -c --arg case "$case_id" \
    '.items[] | select(.case_id==$case)' "$work/atom-admission-manifest.json")
  [[ -n "$admission_item" ]]
  apply_id=$(jq -r '.apply_id' <<<"$admission_item")
  evidence_id=$(jq -r '.evidence_id' <<<"$admission_item")
  stage_plan=$(owner_atom_stage_plan "$apply_id")
  resolution=$(jq -c --arg evidence "$evidence_id" '
    .items[]
    | select(.evidence_id==$evidence)
    | {
        action:.expected_action,
        decision_state:"manual_review_required",
        selected_entity_id:.expected_selected_entity_id,
        proposed_entity:.expected_proposed_entity,
        review_reason_codes:.expected_review_reason_codes
      }
  ' "$repo_root/$review_spec")
  [[ -n "$stage_plan" && -n "$resolution" ]]
  jq -n \
    --arg case "$case_id" \
    --arg owner "$target_owner" \
    --argjson item "$admission_item" \
    --argjson plan "$stage_plan" \
    --argjson resolution "$resolution" \
    '{
      contract_version:"memory_v1_v5_2_atom_stage_build_manifest_v2",
      target_server:"seebx",
      owner_user_id:$owner,
      case_id:$case,
      apply_id:$item.apply_id,
      proposal_id:$item.proposal_id,
      review_id:$item.review_id,
      packet_id:$item.packet_id,
      evidence_id:$item.evidence_id,
      apply_manifest_sha256:$plan.apply_manifest_sha256,
      proposal_sha256:$plan.proposal_sha256,
      stage_projection_sha256:$plan.stage_projection_sha256,
      expected_counts:$plan.counts,
      expected_resolutions:{"e00":$resolution}
    }' >"$output"
  chown ubuntu:ubuntu "$output"
  chmod 0600 "$output"
}

qdrant_before=$(qdrant_signature)
production_before=$(production_signature)
production_head_before=$(git -C /opt/chat-memory rev-parse HEAD)

docker exec brains-postgres-1 pg_dump -U sage -d memory -Fc >"$backup"
[[ -s "$backup" ]]
docker exec brains-postgres-1 psql -X -A -t -U sage -d memory -c "
  SELECT format(
    'CREATE ROLE %I %s %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;',
    rolname,
    CASE WHEN rolcanlogin THEN 'LOGIN' ELSE 'NOLOGIN' END,
    CASE WHEN rolinherit THEN 'INHERIT' ELSE 'NOINHERIT' END
  )
  FROM pg_roles
  WHERE rolname NOT LIKE 'pg\\_%' ESCAPE '\\'
    AND rolname NOT IN ('sage','postgres')
  ORDER BY rolname
" >"$role_sql"
[[ -s "$role_sql" ]]

"${compose[@]}" up -d --wait postgres
run_sql <"$role_sql"
printf '%s\n' \
  "ALTER ROLE brains_app PASSWORD 'clone_only_brains_password';" | run_sql
"${compose[@]}" exec -T postgres pg_restore -U sage -d memory \
  --clean --if-exists <"$backup"
run_sql <"$repo_root/$temporal_migration"

assert_equal initial_target_counts "$(target_counts)" '0,0,0,0,0,0,0,0,0'
assert_equal initial_atom_counts "$(atom_counts)" '0,0,0,0'
assert_equal initial_temporal_review_count "$(scalar "
  SELECT count(*)
  FROM memory.v5_2_temporal_review_registration
  WHERE owner_user_id='$target_owner'::uuid")" 0
assert_equal route_rls "$(scalar "
  SELECT relrowsecurity::int::text||':'||relforcerowsecurity::int::text
  FROM pg_class
  WHERE oid='memory.v5_2_local_packet_route_event'::regclass")" '1:1'
assert_equal stage_rls "$(scalar "
  SELECT relrowsecurity::int::text||':'||relforcerowsecurity::int::text
  FROM pg_class
  WHERE oid='memory.relational_stage_batch'::regclass")" '1:1'
assert_equal review_rls "$(scalar "
  SELECT relrowsecurity::int::text||':'||relforcerowsecurity::int::text
  FROM pg_class
  WHERE oid='memory.entity_resolution_review'::regclass")" '1:1'
for atom_table in \
  v5_2_atom_admission_proposal \
  v5_2_atom_admission_review \
  v5_2_atom_admission_apply \
  v5_2_atom_admission_operation \
  v5_2_temporal_review_registration
do
  assert_equal "atom_${atom_table}_rls" "$(scalar "
    SELECT relrowsecurity::int::text||':'||relforcerowsecurity::int::text
    FROM pg_class
    WHERE oid='memory.${atom_table}'::regclass")" '1:1'
done
target_entities_before=$(scalar "
  SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")
target_claims_before=$(scalar "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")

runuser -u ubuntu -- env POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$temporal_runner" manifest \
  --spec "$repo_root/$temporal_spec" \
  --output "$work/temporal-review-manifest.json" \
  >"$work/temporal-review-manifest-report.json"
runuser -u ubuntu -- env POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$temporal_runner" run \
  --mode preflight \
  --manifest "$work/temporal-review-manifest.json" \
  >"$work/temporal-review-preflight.json"
assert_equal temporal_preflight_rows \
  "$(jq -r '.persistent_writes' "$work/temporal-review-preflight.json")" 0
assert_equal temporal_preflight_outcome \
  "$(jq -r '.results[0].outcome' "$work/temporal-review-preflight.json")" applied
assert_equal temporal_preflight_persisted "$(scalar "
  SELECT count(*)
  FROM memory.v5_2_temporal_review_registration
  WHERE owner_user_id='$target_owner'::uuid")" 0
runuser -u ubuntu -- env \
  MEMORY_V1_V5_2_TEMPORAL_REVIEW_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$repo_root/$temporal_runner" run \
  --mode apply \
  --manifest "$work/temporal-review-manifest.json" \
  --confirm REGISTER_REVIEWED_TEMPORAL_PROJECTIONS_ONLY \
  >"$work/temporal-review-apply.json"
assert_equal temporal_apply_rows \
  "$(jq -r '.persistent_writes' "$work/temporal-review-apply.json")" 1
assert_equal temporal_registration_count "$(scalar "
  SELECT count(*)
  FROM memory.v5_2_temporal_review_registration
  WHERE owner_user_id='$target_owner'::uuid")" 1
runuser -u ubuntu -- env POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$temporal_runner" run \
  --mode replay \
  --manifest "$work/temporal-review-manifest.json" \
  >"$work/temporal-review-replay.json"
assert_equal temporal_replay_rows \
  "$(jq -r '.persistent_writes' "$work/temporal-review-replay.json")" 0
assert_equal temporal_replay_outcome \
  "$(jq -r '.results[0].outcome' "$work/temporal-review-replay.json")" replayed

TARGET_OWNER="$target_owner" OTHER_OWNER="$other_owner" \
  TEMPORAL_MANIFEST="$work/temporal-review-manifest.json" \
  POSTGRES_DSN="$dsn" /opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import json
import os
import uuid

import asyncpg


async def must_fail(operation, sqlstates):
    try:
        await operation()
    except asyncpg.PostgresError as exc:
        if exc.sqlstate not in sqlstates:
            raise
    else:
        raise RuntimeError("expected reviewed-temporal security failure")


async def main() -> None:
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    target = os.environ["TARGET_OWNER"]
    other = os.environ["OTHER_OWNER"]
    row = json.loads(open(os.environ["TEMPORAL_MANIFEST"], encoding="utf-8").read())[
        "items"
    ][0]

    async def cross_owner():
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id',$1,true)", other)
            await conn.fetchrow(
                """
                SELECT * FROM memory.review_owner_v5_2_temporal_projection_v1(
                  $1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12::jsonb
                )
                """,
                uuid.uuid4(),
                uuid.UUID(row["registration_id"]),
                uuid.UUID(row["packet_id"]),
                uuid.UUID(row["evidence_id"]),
                row["observation_ref"],
                row["packet_storage_sha256"],
                row["stage_bundle_sha256"],
                row["review_report_sha256"],
                row["raw_temporal_sha256"],
                json.dumps(row["reviewed_temporal"]),
                row["reviewed_temporal_sha256"],
                json.dumps(row["review_reason_codes"]),
            )

    async def invalid_transform():
        changed = dict(row["reviewed_temporal"])
        changed["relative_offset"] = {
            **changed["relative_offset"],
            "magnitude": 2.0,
        }
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id',$1,true)", target)
            await conn.fetchrow(
                """
                SELECT * FROM memory.review_owner_v5_2_temporal_projection_v1(
                  $1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12::jsonb
                )
                """,
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.UUID(row["packet_id"]),
                uuid.UUID(row["evidence_id"]),
                row["observation_ref"],
                row["packet_storage_sha256"],
                row["stage_bundle_sha256"],
                row["review_report_sha256"],
                row["raw_temporal_sha256"],
                json.dumps(changed),
                "0" * 64,
                json.dumps(row["review_reason_codes"]),
            )

    async def direct_insert():
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id',$1,true)", target)
            await conn.execute(
                "INSERT INTO memory.v5_2_temporal_review_registration DEFAULT VALUES"
            )

    await must_fail(cross_owner, {"23514", "42501"})
    await must_fail(invalid_transform, {"23514"})
    await must_fail(direct_insert, {"42501"})
    await conn.close()


asyncio.run(main())
PY

runuser -u ubuntu -- env POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$atom_manifest_runner" \
  --spec "$repo_root/$atom_spec" \
  --output "$work/atom-admission-manifest.json"
runuser -u ubuntu -- env POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$atom_apply_runner" \
  --mode preflight \
  --manifest "$work/atom-admission-manifest.json" \
  --output "$work/atom-admission-preflight.json"
assert_equal atom_preflight_rows \
  "$(jq -r '.persistent_writes' "$work/atom-admission-preflight.json")" 0
runuser -u ubuntu -- env MEMORY_V1_V5_2_ATOM_ADMISSION_APPLY_V2=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$repo_root/$atom_apply_runner" \
  --mode apply \
  --manifest "$work/atom-admission-manifest.json" \
  --output "$work/atom-admission-apply.json"
assert_equal atom_rows "$(atom_counts)" '2,2,2,6'
assert_equal atom_apply_rows \
  "$(jq -r '.persistent_writes' "$work/atom-admission-apply.json")" 12
runuser -u ubuntu -- env POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$atom_apply_runner" \
  --mode replay \
  --manifest "$work/atom-admission-manifest.json" \
  --output "$work/atom-admission-replay.json"
assert_equal atom_replay_rows \
  "$(jq -r '.persistent_writes' "$work/atom-admission-replay.json")" 0

make_atom_stage_manifest \
  pet_identity_dahlia "$work/dahlia-atom-stage-build.json"
make_atom_stage_manifest \
  pet_identity_helsing "$work/helsing-atom-stage-build.json"
runuser -u ubuntu -- env POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$atom_stage_runner" build \
  --manifest "$work/dahlia-atom-stage-build.json" \
  --output-root "$work/dahlia-atom-stage"
runuser -u ubuntu -- env POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$atom_stage_runner" build \
  --manifest "$work/helsing-atom-stage-build.json" \
  --output-root "$work/helsing-atom-stage"

dahlia_apply_id=$(jq -r '
  .items[] | select(.case_id=="pet_identity_dahlia") | .apply_id
' "$work/atom-admission-manifest.json")
helsing_apply_id=$(jq -r '
  .items[] | select(.case_id=="pet_identity_helsing") | .apply_id
' "$work/atom-admission-manifest.json")
runuser -u ubuntu -- env POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$atom_stage_runner" probe \
  --bundle "$work/dahlia-atom-stage/bundle.json" \
  --apply-id "$dahlia_apply_id" \
  --other-owner-user-id "$other_owner"
runuser -u ubuntu -- env POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$repo_root/$atom_stage_runner" probe \
  --bundle "$work/helsing-atom-stage/bundle.json" \
  --apply-id "$helsing_apply_id" \
  --other-owner-user-id "$other_owner"

jq -n \
  --arg owner "$target_owner" \
  --argjson keasha "$(jq -c '.bundles[0]' \
    "$repo_root/$source_stage_manifest")" \
  --argjson dahlia "$(jq -c '.bundles[0]' \
    "$work/dahlia-atom-stage/stage-manifest.json")" \
  --argjson helsing "$(jq -c '.bundles[0]' \
    "$work/helsing-atom-stage/stage-manifest.json")" \
  '{
    contract_version:"memory_v1_v5_2_stage_batch_manifest_v1",
    target_server:"seebx",
    owner_user_id:$owner,
    bundles:[$keasha,$dahlia,$helsing]
  }' >"$work/stage-manifest.json"
chown ubuntu:ubuntu "$work/stage-manifest.json"
chmod 0600 "$work/stage-manifest.json"
runuser -u ubuntu -- env POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$stage_runner" plan \
  --manifest "$work/stage-manifest.json" \
  --review-root "$review_root" \
  --output "$work/stage-plan.json"
head=$(git -C "$repo_root" rev-parse HEAD)
runuser -u ubuntu -- /opt/chat-memory/venv/bin/python \
  "$repo_root/$stage_fixture" authorize \
  --plan "$work/stage-plan.json" \
  --output "$work/stage-authorization.json" \
  --head "$head"
runuser -u ubuntu -- env MEMORY_V1_V5_2_STAGE_BATCH_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$repo_root/$stage_runner" apply \
  --plan "$work/stage-plan.json" \
  --authorization "$work/stage-authorization.json" \
  --review-root "$review_root" \
  --confirm STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY \
  --output "$work/stage-apply.json"

assert_equal staged_target_counts "$(target_counts)" '3,3,3,2,3,3,0,0,3'
assert_equal staged_atom_counts "$(atom_counts)" '2,2,2,6'
assert_equal staged_temporal_review_count "$(scalar "
  SELECT count(*)
  FROM memory.v5_2_temporal_review_registration
  WHERE owner_user_id='$target_owner'::uuid")" 1
assert_equal stage_rows \
  "$(jq -r '.database_rows_created' "$work/stage-apply.json")" 20
assert_equal stage_replay_rows \
  "$(jq -r '.checks.replay_rows_written' "$work/stage-apply.json")" 0

runuser -u ubuntu -- env POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$review_runner" manifest \
  --spec "$repo_root/$review_spec" \
  --output "$work/entity-review-manifest.json"
runuser -u ubuntu -- env POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" \
  GIT_OPTIONAL_LOCKS=0 /opt/chat-memory/venv/bin/python \
  "$repo_root/$review_runner" plan \
  --manifest "$work/entity-review-manifest.json" \
  --review-root "$review_root" \
  --output "$work/entity-review-plan.json"

plan_sha=$(sha256sum "$work/entity-review-plan.json" | awk '{print $1}')
authorized_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
expires_at=$(date -u -d '+20 minutes' +%Y-%m-%dT%H:%M:%SZ)
jq -n \
  --arg authorization_id "$(cat /proc/sys/kernel/random/uuid)" \
  --arg authorized_at "$authorized_at" --arg expires_at "$expires_at" \
  --arg head "$head" --arg owner "$target_owner" --arg plan_sha "$plan_sha" \
  '{
    contract_version:"memory_v1_v5_2_entity_review_authorization_v1",
    authorization_id:$authorization_id,authorized:true,authorized_by:"Eric Lund",
    authorized_at:$authorized_at,expires_at:$expires_at,
    expected_head_commit:$head,target_server:"seebx",
    scope:"review_owner_v5_2_entity_resolutions_without_apply",
    owner_user_id:$owner,plan_sha256:$plan_sha,expected_item_count:3,
    expected_new_rows:6,
    confirmation:"REVIEW_OWNER_V5_2_ENTITY_RESOLUTIONS_WITHOUT_APPLY"
  }' >"$work/entity-review-authorization.json"
chown ubuntu:ubuntu "$work/entity-review-authorization.json"
chmod 0600 "$work/entity-review-authorization.json"

runuser -u ubuntu -- env MEMORY_V1_V5_2_ENTITY_REVIEW_ONLY_APPLY=authorized \
  POSTGRES_DSN="$dsn" PYTHONPATH="$repo_root" GIT_OPTIONAL_LOCKS=0 \
  /opt/chat-memory/venv/bin/python "$repo_root/$review_runner" apply \
  --plan "$work/entity-review-plan.json" \
  --authorization "$work/entity-review-authorization.json" \
  --review-root "$review_root" \
  --confirm REVIEW_OWNER_V5_2_ENTITY_RESOLUTIONS_WITHOUT_APPLY \
  --output "$work/entity-review-apply.json"

assert_equal reviewed_target_counts "$(target_counts)" '3,3,3,2,3,3,3,0,6'
assert_equal review_rows \
  "$(jq -r '.new_rows' "$work/entity-review-apply.json")" 6
assert_equal review_replay \
  "$(jq -r '.zero_write_replay' "$work/entity-review-apply.json")" true
assert_equal reviewed_actions "$(scalar "
  SELECT string_agg(
    plan.action::text||':'||
    coalesce(plan.selected_entity_id::text,plan.proposed_entity->>'canonical_name'),
    ',' ORDER BY mention.name_text
  )
  FROM memory.entity_resolution_plan AS plan
  JOIN memory.entity_mention AS mention
    USING(owner_user_id,mention_id)
  WHERE plan.owner_user_id='$target_owner'::uuid
    AND plan.evidence_id IN ($evidence_sql)")" \
  'link_existing:0c620047-b302-44ef-b04a-810de3b311fc,create_new:Helsing,link_existing:6db538e2-b7f9-48ff-b2bc-db757708b660'
assert_equal reviewed_decisions "$(scalar "
  SELECT string_agg(review.decision::text,',' ORDER BY plan.evidence_id)
  FROM memory.entity_resolution_review AS review
  JOIN memory.entity_resolution_plan AS plan
    USING(owner_user_id,resolution_id)
  WHERE plan.owner_user_id='$target_owner'::uuid
    AND plan.evidence_id IN ($evidence_sql)")" \
  'approved,approved,approved'
assert_equal helsing_temporal "$(scalar "
  SELECT concat_ws(':',
    observation.predicate,
    temporal.semantic::text,
    temporal.basis::text,
    temporal.source_form::text,
    temporal.certainty::text,
    temporal.precision::text,
    temporal.anchored_to_source_time::text,
    temporal.relative_offset->>'direction',
    temporal.relative_offset->>'magnitude',
    temporal.relative_offset->>'unit')
  FROM memory.observation AS observation
  JOIN memory.observation_temporal AS temporal
    USING(owner_user_id,observation_id)
  WHERE observation.owner_user_id='$target_owner'::uuid
    AND observation.evidence_id='$helsing_evidence'::uuid")" \
  'life_event.died:occurrence:relative:relative:approximate:year:true:past:1.0:year'

OTHER_OWNER="$other_owner" EVIDENCE_IDS="$keasha_evidence,$dahlia_evidence,$helsing_evidence" \
  POSTGRES_DSN="$dsn" /opt/chat-memory/venv/bin/python - <<'PY'
import asyncio
import os
import uuid

import asyncpg


async def main() -> None:
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        for evidence in os.environ["EVIDENCE_IDS"].split(","):
            try:
                async with conn.transaction(readonly=True):
                    await conn.execute(
                        "SELECT set_config('app.user_id',$1,true)",
                        os.environ["OTHER_OWNER"],
                    )
                    await conn.fetchval(
                        "SELECT memory.plan_owner_v5_2_entity_resolution_review_v1($1::uuid)",
                        uuid.UUID(evidence),
                    )
            except asyncpg.PostgresError as exc:
                if exc.sqlstate != "P0002":
                    raise
            else:
                raise RuntimeError("cross-owner entity review plan resolved")
    finally:
        await conn.close()


asyncio.run(main())
PY

assert_equal target_entities_unchanged "$(scalar "
  SELECT count(*) FROM memory.entity
  WHERE owner_user_id='$target_owner'::uuid")" "$target_entities_before"
assert_equal target_claims_unchanged "$(scalar "
  SELECT count(*) FROM memory.claim
  WHERE owner_user_id='$target_owner'::uuid")" "$target_claims_before"
assert_equal qdrant_unchanged "$(qdrant_signature)" "$qdrant_before"
assert_equal production_rows_unchanged \
  "$(production_signature)" "$production_before"
assert_equal production_head_unchanged \
  "$(git -C /opt/chat-memory rev-parse HEAD)" "$production_head_before"
assert_equal brains_service "$(systemctl is-active brains.service)" active
docker exec brains-postgres-1 pg_isready -U sage -d memory >/dev/null

printf '%s\n' \
  'MEMORY_V1_V5_2_PET_RELATIONAL_STAGE_REVIEW_CLONE=PASS' \
  'clone_temporal_review_rows_created=1' \
  'clone_atom_admission_rows_created=12' \
  'clone_stage_rows_created=20' \
  'clone_review_rows_created=6' \
  'manual_reviews=3' \
  'entity_apply_rows=0' \
  'entity_rows_created=0' \
  'claim_rows_created=0' \
  'production_writes=0' \
  'qdrant_writes=0' \
  'retrieval_changes=0' \
  'prompt_changes=0' \
  'hard_stop=before_entity_apply_claims_projection_or_retrieval'
