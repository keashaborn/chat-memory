#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Proves the owner-scoped stale-review refresh path on a
# disposable production clone. It makes no production writes.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source .env
set +a

container=brains-postgres-1
production=memory
clone="memory_v5_review_refresh_${$}"
owner=d839b4bc-0bd2-4f2d-aafe-0f3f75883db8
other=557ea042-cb82-48f8-9429-472e96c957ef
packet=72b091a6-22be-549e-9c63-a1a4360f3eac
content=48760fb157c7a4b3776fabe26367fbe5fe8838e9eca079bad26b9f3d60862989
storage=7d57a27543cbc93335cdc31ed3e7da3cdfc828adbcf1c427acbdbbf55758dfc5
report_sha=0c2ae98f1e8a4307447e6c333104b2f146b1e59a29b9f545b50db6fe5fcc7234
bundle_sha=4ee56a17c9f72d94706703a2a93c21eb0e9cbf4d58ed87e09556448d7785b158
plan_sha=72da532d9cc84052cb07aa8380db1f28852ef43e3f08329a0a6fadcf2ee1cace
migration=ops/sql/20260719_memory_v1_v5_local_review_refresh_reextract.sql
test_sql=tests/memory_v1_v5_local_review_refresh_reextract.sql
worker=scripts/memory_v1_v5_local_review_refresh_reextract.py
backup=$(mktemp /tmp/memory-v5-review-refresh.XXXXXX.dump)
dry=$(mktemp /tmp/memory-v5-review-refresh.XXXXXX.dry)
applied=$(mktemp /tmp/memory-v5-review-refresh.XXXXXX.apply)
chmod 0600 "$backup" "$dry" "$applied"

cleanup() {
  rc=$?
  trap - EXIT
  docker exec "$container" dropdb -U sage --if-exists "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup" "$dry" "$applied"
  exit "$rc"
}
trap cleanup EXIT

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

clone_dsn=$(venv/bin/python -c \
  'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
  "$POSTGRES_DSN" "$clone")
psql "$clone_dsn" -X -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" -v other_owner="$other" \
  -v prior_packet="$packet" -v content_sha256="$content" \
  -v packet_storage_sha256="$storage" \
  -v review_report_sha256="$report_sha" \
  -v stage_bundle_sha256="$bundle_sha" \
  -v operation_id=00000000-0000-4000-8000-000000000301 \
  -v new_job_id=00000000-0000-4000-8000-000000000302 \
  -v new_terminal_id=00000000-0000-4000-8000-000000000303 \
  <"$test_sql" >/dev/null

POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner" --prior-packet-id "$packet" \
  --expected-content-sha256 "$content" \
  --expected-packet-storage-sha256 "$storage" \
  --expected-review-report-sha256 "$report_sha" \
  --expected-stage-bundle-sha256 "$bundle_sha" >"$dry"
jq -e --arg plan "$plan_sha" '
  .apply==false and .outcome=="eligible" and .plan_sha256==$plan and
  .active_self_entity_count==1 and
  .write_counts=={"events":0,"jobs":0,"terminals":0} and
  .external_model_calls==0 and .claim_writes==0 and .qdrant_writes==0 and
  .prompt_influence==0
' "$dry" >/dev/null

MEMORY_V1_V5_LOCAL_REVIEW_REFRESH_REEXTRACT_APPLY=memory_v1_v5_local_review_refresh_reextract_apply_v1 \
POSTGRES_DSN="$clone_dsn" PYTHONPATH="$repo_root" venv/bin/python "$worker" \
  --owner-user-id "$owner" --prior-packet-id "$packet" \
  --expected-content-sha256 "$content" \
  --expected-packet-storage-sha256 "$storage" \
  --expected-review-report-sha256 "$report_sha" \
  --expected-stage-bundle-sha256 "$bundle_sha" \
  --expected-plan-sha256 "$plan_sha" --apply >"$applied"
jq -e '
  .apply==true and .outcome=="queued" and
  .write_counts=={"events":1,"jobs":1,"terminals":1} and
  .zero_write_replay_proved==true and .external_model_calls==0 and
  .claim_writes==0 and .qdrant_writes==0 and .prompt_influence==0
' "$applied" >/dev/null
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT count(*) FROM memory.evidence_extraction_job
   WHERE owner_user_id='$owner'::uuid
     AND selector_version='20260719_v5_self_bootstrap_review_refresh_v1'
     AND status='pending' AND attempts=0")" == 1 ]]
[[ "$(docker exec "$container" psql -U sage -d "$clone" -X -Atqc \
  "SELECT count(*) FROM memory.v5_local_packet_review_artifact
   WHERE owner_user_id='$owner'::uuid AND packet_id='$packet'::uuid")" == 1 ]]

printf '%s\n' 'memory_v1_v5_local_review_refresh_reextract_clone: PASS'
