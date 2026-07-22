#!/usr/bin/env bash
set -euo pipefail

# Server: seebx backend. Rehearses the V5.2 belief guard compatibility and
# one append-only ambiguous-transcription deferral in a disposable clone.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
production=memory
clone="memory_v5_2_review_deferral_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=7357397a-26a3-5d19-aee4-f7284509cf3f
evidence=2e0c5951-a38f-5ffd-89ac-1e9e4c4c6721
migration=ops/sql/20260722_memory_v1_v5_2_ambiguous_review_deferral.sql
rollback=ops/sql/20260722_memory_v1_v5_2_ambiguous_review_deferral_rollback.sql
builder=scripts/memory_v1_v5_build_local_review_deferral.py
worker=scripts/memory_v1_v5_local_review_deferral.py
backup=$(mktemp /tmp/memory-v5-2-review-deferral.XXXXXX.dump)
review_dir=$(mktemp -d /home/ubuntu/memory-v1-reviews/v5-2-deferral-clone.XXXXXX)
decision="$review_dir/decision.json"
builder_output=$(mktemp /tmp/memory-v5-2-review-deferral-builder.XXXXXX.json)
dry_output=$(mktemp /tmp/memory-v5-2-review-deferral-dry.XXXXXX.json)
apply_output=$(mktemp /tmp/memory-v5-2-review-deferral-apply.XXXXXX.json)
chmod 0700 "$review_dir"
chmod 0600 "$backup" "$builder_output" "$dry_output" "$apply_output"

cleanup() {
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup" "$builder_output" "$dry_output" "$apply_output"
  rm -rf "$review_dir"
}
trap cleanup EXIT

clone_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$clone" -c "$1" | tr -d '[:space:]'
}

production_scalar() {
  docker exec "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U sage -d "$production" -c "$1" | tr -d '[:space:]'
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

for file in "$migration" "$rollback" "$builder" "$worker" \
  tests/test_memory_v1_v5_2_local_review_deferral.py \
  tests/test_memory_v1_local_provider_v5_2.py; do
  [[ -f "$file" ]]
done

qdrant_before=$(qdrant_signature)
production_dispositions_before=$(production_scalar \
  'SELECT count(*) FROM memory.v5_local_packet_disposition')
production_stage_before=$(production_scalar \
  'SELECT count(*) FROM memory.relational_stage_batch')

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc --no-owner >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --no-owner <"$backup"

rows_before=$(clone_scalar \
  'SELECT count(*) FROM memory.v5_local_packet_disposition')
stage_before=$(clone_scalar 'SELECT count(*) FROM memory.relational_stage_batch')
claims_before=$(clone_scalar 'SELECT count(*) FROM memory.claim')

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration" >/dev/null
[[ "$(clone_scalar "SELECT strpos(pg_get_functiondef(
  'memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)'::regprocedure
),'memory_v1_semantic_policy_compiler_v2')>0")" == t ]]
[[ "$(clone_scalar "SELECT to_regprocedure(
  'memory.finalize_owner_v5_2_review_deferral_v1(uuid,uuid,uuid,text,text,text)'
) IS NOT NULL")" == t ]]
[[ "$(clone_scalar "SELECT count(*) FROM pg_trigger
  WHERE tgrelid='memory.relational_stage_batch'::regclass
    AND tgname='v5_local_disposition_stage_guard' AND NOT tgisinternal")" == 1 ]]

# The rollback is valid only before a review disposition exists.
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$rollback" >/dev/null
[[ "$(clone_scalar "SELECT to_regprocedure(
  'memory.finalize_owner_v5_2_review_deferral_v1(uuid,uuid,uuid,text,text,text)'
) IS NULL")" == t ]]
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration" >/dev/null
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" <"$migration" >/dev/null

set -a
source /opt/chat-memory/.env
set +a
clone_dsn=$(SOURCE_DSN="$POSTGRES_DSN" CLONE_DB="$clone" python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit
value = urlsplit(os.environ["SOURCE_DSN"])
print(urlunsplit((value.scheme, value.netloc, "/" + os.environ["CLONE_DB"], value.query, value.fragment)))
PY
)

PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python -m unittest \
  tests.test_memory_v1_v5_2_local_review_deferral \
  tests.test_memory_v1_local_provider_v5_2 >/dev/null
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$builder" \
  --owner-user-id "$owner" --packet-id "$packet" \
  --reviewer-ref user_authorized_2026-07-21 \
  --review-root "$review_dir" --output "$decision" >"$builder_output"
jq -e '.review_decision=="deferred" and
  .reason_code=="ambiguous_transcription" and
  .promotion_eligible==false and .source_prose_included==false and
  .database_writes==0' "$builder_output" >/dev/null

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" --packet-id "$packet" \
  --decision "$decision" >"$dry_output"
jq -e '.apply==false and .outcome=="eligible" and
  .write_counts.dispositions==0 and .write_counts.staging==0 and
  .write_counts.claims==0 and .write_counts.qdrant==0 and
  .zero_write_replay_proved==false and .external_model_calls==0' \
  "$dry_output" >/dev/null
[[ "$(clone_scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" == "$rows_before" ]]

POSTGRES_DSN="$clone_dsn" \
MEMORY_V1_V5_2_REVIEW_DEFERRAL_APPLY=memory_v1_v5_2_local_review_deferral_apply_v1 \
PYTHONPATH="$repo_root" /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$owner" --packet-id "$packet" \
  --decision "$decision" --apply >"$apply_output"
jq -e '.apply==true and .outcome=="deferred" and
  .reason_code=="ambiguous_transcription" and
  .promotion_eligible==false and .write_counts.dispositions==1 and
  .write_counts.staging==0 and .write_counts.claims==0 and
  .write_counts.qdrant==0 and .zero_write_replay_proved==true and
  .external_model_calls==0' "$apply_output" >/dev/null

[[ "$(clone_scalar "SELECT count(*)
  FROM memory.v5_local_packet_disposition
  WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid
    AND evidence_id='$evidence'::uuid
    AND disposition='terminal_no_stage'
    AND review_decision='deferred'
    AND reason_code='ambiguous_transcription'
    AND NOT promotion_eligible
    AND review_basis_sha256 ~ '^[0-9a-f]{64}$'")" == 1 ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" == "$((rows_before+1))" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.relational_stage_batch')" == "$stage_before" ]]
[[ "$(clone_scalar 'SELECT count(*) FROM memory.claim')" == "$claims_before" ]]

# The database guard blocks every staging path, not only the current worker.
docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 \
  -U sage -d "$clone" >/dev/null <<SQL
DO \$test\$
BEGIN
  PERFORM set_config('app.user_id','$owner',true);
  BEGIN
    INSERT INTO memory.relational_stage_batch(
      owner_user_id,batch_id,evidence_id,
      extraction_packet_text,resolution_packet_text,
      extraction_packet_sha256,resolution_packet_sha256,
      stage_manifest_sha256,extractor,extractor_version,
      mention_count,resolution_count,candidate_count,
      observation_count,temporal_count,result,invoked_by_session
    ) VALUES (
      '$owner'::uuid,gen_random_uuid(),'$evidence'::uuid,
      '{}','{}',encode(digest(convert_to('{}','UTF8'),'sha256'),'hex'),
      encode(digest(convert_to('{}','UTF8'),'sha256'),'hex'),
      encode(digest(convert_to(gen_random_uuid()::text,'UTF8'),'sha256'),'hex'),
      'guard_test','v1',0,0,0,0,0,'{}','sage'
    );
    RAISE EXCEPTION 'stage guard did not reject disposed evidence';
  EXCEPTION WHEN check_violation THEN
    IF SQLERRM<>'disposed evidence cannot enter relational staging' THEN
      RAISE;
    END IF;
  END;
END
\$test\$;
SQL

# An unrelated authenticated owner cannot see or finalize the packet.
if POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" \
  /opt/chat-memory/venv/bin/python "$worker" \
  --owner-user-id "$other" --packet-id "$packet" \
  --decision "$decision" >/dev/null 2>&1; then
  echo 'cross-owner review unexpectedly succeeded' >&2
  exit 1
fi

[[ "$(production_scalar 'SELECT count(*) FROM memory.v5_local_packet_disposition')" == "$production_dispositions_before" ]]
[[ "$(production_scalar 'SELECT count(*) FROM memory.relational_stage_batch')" == "$production_stage_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]

printf '%s\n' 'memory_v1_v5_2_ambiguous_review_deferral_clone: PASS'
