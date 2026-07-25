#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the exact two-record compiler-v8 re-extraction
# selector on a disposable production clone and rolls all test writes back.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
production=memory
clone="memory_v5_2_semantic_compiler_v8_reextract_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
selector=20260725_v5_2_semantic_compiler_v8_reextract_v1
manifest=98e36ec66a5136a5bea4c3438be11acda18ceda2865b393cdb58b6bb3def337f
compiler=f82e6f4339dfe4aada7e5c3edb71fde8125a819f33677b3f47b98f3726b60419
migration=ops/sql/20260725_memory_v1_v5_2_semantic_compiler_v8_reextract.sql
test_sql=tests/memory_v1_v5_2_semantic_compiler_v8_reextract.sql
backup=$(mktemp /tmp/memory-v5-2-semantic-compiler-v8-reextract.XXXXXX.dump)
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

production_before=$(scalar "$production" "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(
    to_jsonb(value)::text,E'\\n' ORDER BY to_jsonb(value)::text
  ),''),'UTF8'),'sha256'),'hex')
  FROM (
    SELECT evidence_id,content_sha256,status,source_system
    FROM memory.evidence
    WHERE owner_user_id='$owner'::uuid
      AND evidence_id IN (
        'fea59e7e-30f5-4139-b634-97b291c88e14'::uuid,
        'dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5'::uuid
      )
    ORDER BY evidence_id
  ) AS value")
qdrant_before=$(qdrant_signature)
selector_before=$(scalar "$production" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")

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
  -v evidence_id=fea59e7e-30f5-4139-b634-97b291c88e14 \
  -v content_sha256=895146b94431f7e0ec3292e757e30fc4c782af222bd39598e610654adf08ccec \
  -v prior_packet_id=a7f23e7b-89ff-5d23-bb75-79473be6f57d \
  -v prior_packet_storage_sha256=430264a8f709288562505c0e50c97e2b379af59536ff5a65061352c3b925789e \
  -v operation_id=6d907502-d830-5467-9e61-fe846453383d \
  -v job_id=d0585d84-7386-5b5f-b7ff-ac97949883cc \
  -v terminal_id=25717c16-e87c-54ff-92da-d071770f6ab7 \
  -v manifest_sha256="$manifest" \
  -v compiler_sha256="$compiler" \
  <"$test_sql" >/dev/null

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 >/dev/null <<SQL
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT * FROM
memory.enqueue_owner_v5_2_semantic_compiler_v8_reextract_v1(
  '6d907502-d830-5467-9e61-fe846453383d',
  'd0585d84-7386-5b5f-b7ff-ac97949883cc',
  '25717c16-e87c-54ff-92da-d071770f6ab7',
  'fea59e7e-30f5-4139-b634-97b291c88e14',
  '895146b94431f7e0ec3292e757e30fc4c782af222bd39598e610654adf08ccec',
  'a7f23e7b-89ff-5d23-bb75-79473be6f57d',
  '430264a8f709288562505c0e50c97e2b379af59536ff5a65061352c3b925789e',
  '$manifest','$compiler'
);
SELECT * FROM
memory.enqueue_owner_v5_2_semantic_compiler_v8_reextract_v1(
  '87074bcf-7206-5032-8c25-f98c1b79f31c',
  '360dbb78-69e3-5a3d-ba9b-2512acca21b8',
  '059d58f1-eb02-559a-b30b-f55f51e1af7c',
  'dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5',
  'fb65fe592059bace88a4daea571ac1ba7df2b5aeb5136233dd01071cb6085182',
  '842c2fc5-d2ca-584d-a95c-98655e942732',
  '968611c8092391ffcdaa5ab493dbe7d1eb574c0a8e1748915c3ccb24c22bf029',
  '$manifest','$compiler'
);
RESET SESSION AUTHORIZATION;

DO \$verify_exact_two\$
BEGIN
  IF (
    SELECT count(*) FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND selector_version='$selector'
      AND status='pending'
      AND attempts=0
  )<>2
  OR (
    SELECT count(*) FROM memory.evidence_intake_terminal
    WHERE owner_user_id='$owner'::uuid
      AND selector_version='$selector'
      AND details->>'policy_compiler_sha256'='$compiler'
      AND details ? 'prior_packet_id'
      AND details ? 'prior_packet_storage_sha256'
  )<>2 THEN
    RAISE EXCEPTION
      'exact two-job semantic compiler-v8 binding failed';
  END IF;
END
\$verify_exact_two\$;

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT * FROM
memory.enqueue_owner_v5_2_semantic_compiler_v8_reextract_v1(
  '87074bcf-7206-5032-8c25-f98c1b79f31c',
  '360dbb78-69e3-5a3d-ba9b-2512acca21b8',
  '059d58f1-eb02-559a-b30b-f55f51e1af7c',
  'dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5',
  'fb65fe592059bace88a4daea571ac1ba7df2b5aeb5136233dd01071cb6085182',
  '842c2fc5-d2ca-584d-a95c-98655e942732',
  '968611c8092391ffcdaa5ab493dbe7d1eb574c0a8e1748915c3ccb24c22bf029',
  '$manifest','$compiler'
);
RESET SESSION AUTHORIZATION;
ROLLBACK;
SQL

[[ "$(scalar "$clone" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" == 0 ]]
[[ "$(scalar "$production" "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(
    to_jsonb(value)::text,E'\\n' ORDER BY to_jsonb(value)::text
  ),''),'UTF8'),'sha256'),'hex')
  FROM (
    SELECT evidence_id,content_sha256,status,source_system
    FROM memory.evidence
    WHERE owner_user_id='$owner'::uuid
      AND evidence_id IN (
        'fea59e7e-30f5-4139-b634-97b291c88e14'::uuid,
        'dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5'::uuid
      )
    ORDER BY evidence_id
  ) AS value")" == "$production_before" ]]
[[ "$(scalar "$production" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" == "$selector_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

printf '%s\n' \
  'memory_v1_v5_2_semantic_compiler_v8_reextract_clone: PASS'
