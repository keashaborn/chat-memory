#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
production=memory
clone="memory_v5_2_compiler_v7_reextract_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
manifest=f1d2e4ec4cf51740ea3e53aa639aa5c2a304f9b5a9a56f0ad84c1c9809955056
compiler=a4387aad59445f65fdfc8413f48567b8f560a352cb6fcf8de415769b8752bfbc
migration=ops/sql/20260724_memory_v1_v5_2_compiler_v7_review_reextract.sql
test_sql=tests/memory_v1_v5_2_compiler_v7_review_reextract.sql
backup=$(mktemp /tmp/memory-v5-2-compiler-v7-reextract.XXXXXX.dump)
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

production_signature() {
  docker exec "$container" psql -U sage -d "$production" -X -Atqc "
    SELECT encode(public.digest(convert_to(coalesce(string_agg(
      to_jsonb(value)::text,E'\n' ORDER BY to_jsonb(value)::text
    ),''),'UTF8'),'sha256'),'hex')
    FROM memory.evidence AS value
    WHERE evidence_id IN (
      '33126656-fc5a-5fc1-a035-246b14576ee5'::uuid,
      'fea59e7e-30f5-4139-b634-97b291c88e14'::uuid,
      'dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5'::uuid
    )"
}

qdrant_signature() {
  curl --fail --silent --show-error --max-time 30 \
    -H 'content-type: application/json' \
    -d '{"limit":10000,"with_payload":true,"with_vector":true}' \
    http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll \
    | jq -cS '.result.points | sort_by(.id|tostring)' \
    | sha256sum | awk '{print $1}'
}

before_evidence=$(production_signature)
before_qdrant=$(qdrant_signature)
before_jobs=$(docker exec "$container" psql -U sage -d "$production" -X -Atqc "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='20260724_v5_2_compiler_v7_review_v1'")

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
   GRANT SELECT ON memory.evidence
     TO memory_v5_local_reextract_maintainer;
   GRANT SELECT,INSERT ON
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
  -v evidence_id=33126656-fc5a-5fc1-a035-246b14576ee5 \
  -v content_sha256=be7f19c9986565d34b72d5928301443e61122a5208e7ca8135e348ec2e6711c4 \
  -v operation_id=9dc7a342-524c-549f-92c4-f03daf845a6f \
  -v job_id=db94d835-9598-5805-9101-f320e3c87f0f \
  -v terminal_id=dea158c8-82e8-5b4c-83fe-7bbd13a633b5 \
  -v manifest_sha256="$manifest" \
  -v compiler_sha256="$compiler" \
  <"$test_sql" >/dev/null

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 >/dev/null <<SQL
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT * FROM
memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
    '9dc7a342-524c-549f-92c4-f03daf845a6f'::uuid,
    'db94d835-9598-5805-9101-f320e3c87f0f'::uuid,
    'dea158c8-82e8-5b4c-83fe-7bbd13a633b5'::uuid,
    '33126656-fc5a-5fc1-a035-246b14576ee5'::uuid,
    'be7f19c9986565d34b72d5928301443e61122a5208e7ca8135e348ec2e6711c4',
    '$manifest','$compiler'
  );

SELECT * FROM
memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
    '6479fbda-6b53-55fe-8b72-829eed81efda'::uuid,
    '97211776-4362-5ede-9b22-777c5617e205'::uuid,
    '5c6d918e-15d1-521c-88c3-ebc2981b9967'::uuid,
    'fea59e7e-30f5-4139-b634-97b291c88e14'::uuid,
    '895146b94431f7e0ec3292e757e30fc4c782af222bd39598e610654adf08ccec',
    '$manifest','$compiler'
  );

SELECT * FROM
memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
    '89fa062d-5fdc-5aa1-b1b8-7d8c833912cf'::uuid,
    'aecb3efc-48f6-5587-99cc-0aaa60af9662'::uuid,
    '08e35644-957b-5fc8-a259-3dc0cb169ea8'::uuid,
    'dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5'::uuid,
    'fb65fe592059bace88a4daea571ac1ba7df2b5aeb5136233dd01071cb6085182',
    '$manifest','$compiler'
  );

RESET SESSION AUTHORIZATION;
DO \$verify_all_three\$
BEGIN
  IF (
    SELECT count(*) FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND selector_version='20260724_v5_2_compiler_v7_review_v1'
      AND status='pending'
  )<>3 THEN
    RAISE EXCEPTION 'exact three-job re-extraction bound failed';
  END IF;
END
\$verify_all_three\$;

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT * FROM
memory.enqueue_owner_v5_2_compiler_v7_review_reextract_v1(
    '9dc7a342-524c-549f-92c4-f03daf845a6f'::uuid,
    'db94d835-9598-5805-9101-f320e3c87f0f'::uuid,
    'dea158c8-82e8-5b4c-83fe-7bbd13a633b5'::uuid,
    '33126656-fc5a-5fc1-a035-246b14576ee5'::uuid,
    'be7f19c9986565d34b72d5928301443e61122a5208e7ca8135e348ec2e6711c4',
    '$manifest','$compiler'
  );
RESET SESSION AUTHORIZATION;
ROLLBACK;
SQL

[[ "$(production_signature)" == "$before_evidence" ]]
[[ "$(qdrant_signature)" == "$before_qdrant" ]]
[[ "$(docker exec "$container" psql -U sage -d "$production" -X -Atqc "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='20260724_v5_2_compiler_v7_review_v1'")" \
  == "$before_jobs" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

printf '%s\n' \
  'memory_v1_v5_2_compiler_v7_review_reextract_clone: PASS'
