#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs and applies the exact one-record V5.2
# life-preference re-extraction selector on a disposable production clone.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

container=brains-postgres-1
production=memory
clone="memory_v5_2_life_preference_reextract_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
evidence=51b03105-4acc-5ded-b703-e139a892db9d
content_sha=524bda462b5ece2b507249a11ea68c02ae0ecd4d7148d21ccec4df7bbf675ec7
prior_packet=83598662-cb38-5e30-abe6-1ed8e57b0d87
prior_storage=ea1ece347f77e1eb013c6d98078cf2c61893e54027d9094997ceeca53c5bc6a1
operation=5ae6faaa-c1b9-5f05-827e-bd5ceaa5ba15
job=8de420a2-ab1f-5830-9f75-d1f4caa7747c
terminal=108a811e-2850-52f0-9071-71e251006f39
selector=20260725_v5_2_life_preference_reextract_v1
provider_sha=12531e9617f6d77017d6500ace01b4821599ebdd833e6a4d44a9eb8ceb5e3e73
manifest_path=manifests/memory_v1_v5_2_life_preference_reextract_20260725.json
manifest_sha=e2ca4a9e693909f5b22eb1327b1df31ee7733dd20b25255106c30be2fb08d422
migration=ops/sql/20260725_memory_v1_v5_2_life_preference_reextract.sql
test_sql=tests/memory_v1_v5_2_life_preference_reextract.sql
backup=$(mktemp /tmp/memory-v5-2-life-preference-reextract.XXXXXX.dump)
chmod 0600 "$backup"

cleanup() {
  rc=$?
  trap - EXIT
  docker exec "$container" dropdb -U sage --if-exists --force "$clone" \
    >/dev/null 2>&1 || true
  rm -f "$backup"
  exit "$rc"
}
trap cleanup EXIT

scalar() {
  local database=$1
  local query=$2
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

[[ "$(sha256sum "$manifest_path" | awk '{print $1}')" == "$manifest_sha" ]]
while IFS=$'\t' read -r path expected; do
  [[ "$path" =~ ^(scripts|ops/sql|tests)/[A-Za-z0-9_.-]+$ ]]
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]]
  [[ "$(sha256sum "$path" | awk '{print $1}')" == "$expected" ]]
done < <(
  jq -r '.artifacts | to_entries[] | [.key,.value] | @tsv' \
    "$manifest_path"
)

production_before=$(scalar "$production" "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(
    to_jsonb(value)::text,E'\\n' ORDER BY to_jsonb(value)::text
  ),''),'UTF8'),'sha256'),'hex')
  FROM (
    SELECT evidence_id,content_sha256,status,source_system
    FROM memory.evidence
    WHERE owner_user_id='$owner'::uuid
      AND evidence_id='$evidence'::uuid
  ) AS value")
selector_before=$(scalar "$production" "
  SELECT count(*)
  FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")
qdrant_before=$(qdrant_signature)

docker exec "$container" pg_dump -U sage -d "$production" \
  -Fc --no-owner --no-privileges >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"
docker exec "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 -c \
  'GRANT USAGE ON SCHEMA memory TO
     brains_app,memory_v5_local_reextract_maintainer;
   GRANT SELECT ON
     memory.evidence,
     memory.evidence_extraction_packet_v5_local,
     memory.evidence_extraction_job,
     memory.evidence_intake_terminal,
     memory.evidence_extraction_event
     TO memory_v5_local_reextract_maintainer;
   GRANT INSERT ON
     memory.evidence_extraction_job,
     memory.evidence_intake_terminal,
     memory.evidence_extraction_event
     TO memory_v5_local_reextract_maintainer' >/dev/null
docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 <"$migration" >/dev/null

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 \
  -v target_owner="$owner" \
  -v other_owner="$other" \
  -v evidence_id="$evidence" \
  -v content_sha256="$content_sha" \
  -v prior_packet_id="$prior_packet" \
  -v prior_packet_storage_sha256="$prior_storage" \
  -v operation_id="$operation" \
  -v job_id="$job" \
  -v terminal_id="$terminal" \
  -v manifest_sha256="$manifest_sha" \
  -v provider_source_sha256="$provider_sha" \
  <"$test_sql" >/dev/null

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 >/dev/null <<SQL
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT *
FROM memory.enqueue_owner_v5_2_life_preference_reextract_v1(
  '$operation',
  '$job',
  '$terminal',
  '$evidence',
  '$content_sha',
  '$prior_packet',
  '$prior_storage',
  '$manifest_sha',
  '$provider_sha'
);
SELECT *
FROM memory.enqueue_owner_v5_2_life_preference_reextract_v1(
  '$operation',
  '$job',
  '$terminal',
  '$evidence',
  '$content_sha',
  '$prior_packet',
  '$prior_storage',
  '$manifest_sha',
  '$provider_sha'
);
COMMIT;
SQL

[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.evidence_extraction_job
  WHERE owner_user_id='$owner'::uuid
    AND job_id='$job'::uuid
    AND evidence_id='$evidence'::uuid
    AND selector_version='$selector'
    AND status='pending'
    AND attempts=0")" == 1 ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.evidence_intake_terminal
  WHERE owner_user_id='$owner'::uuid
    AND terminal_id='$terminal'::uuid
    AND evidence_id='$evidence'::uuid
    AND selector_version='$selector'
    AND details->>'manifest_sha256'='$manifest_sha'
    AND details->>'provider_source_sha256'='$provider_sha'")" == 1 ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.evidence_extraction_event
  WHERE owner_user_id='$owner'::uuid
    AND job_id='$job'::uuid
    AND operation_id='$operation'::uuid
    AND event_type='queued'")" == 1 ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.evidence_extraction_packet_v5_local
  WHERE owner_user_id='$owner'::uuid
    AND job_id='$job'::uuid")" == 0 ]]
[[ "$(scalar "$clone" "
  SELECT count(*)
  FROM memory.evidence_extraction_job
  WHERE owner_user_id<>'$owner'::uuid
    AND selector_version='$selector'")" == 0 ]]

[[ "$(scalar "$production" "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(
    to_jsonb(value)::text,E'\\n' ORDER BY to_jsonb(value)::text
  ),''),'UTF8'),'sha256'),'hex')
  FROM (
    SELECT evidence_id,content_sha256,status,source_system
    FROM memory.evidence
    WHERE owner_user_id='$owner'::uuid
      AND evidence_id='$evidence'::uuid
  ) AS value")" == "$production_before" ]]
[[ "$(scalar "$production" "
  SELECT count(*)
  FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" == "$selector_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

printf '%s\n' \
  'memory_v1_v5_2_life_preference_reextract_clone: PASS'
