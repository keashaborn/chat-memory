#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Installs the exact-seven compiler-v9 selector on a
# disposable production clone. All selector writes are rolled back.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
container=brains-postgres-1
production=memory
clone="memory_v5_2_semantic_compiler_v9_reextract_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
other=557ea042-cb82-48f8-9429-472e96c957ef
selector=20260730_v5_2_semantic_compiler_v9_reextract_v1
manifest=dab07b531eb987fba80b67cbc4c106a4d1fe3375add7cb9bb0de7842eaa6b91e
compiler=738cc80f374e3c7e441fd03f964b77d01401422d286a9bc52205c6e060767ae6
migration=ops/sql/20260730_memory_v1_v5_2_semantic_compiler_v9_reextract.sql
test_sql=tests/memory_v1_v5_2_semantic_compiler_v9_reextract.sql
backup=$(mktemp /tmp/memory-v5-2-semantic-compiler-v9-reextract.XXXXXX.dump)
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

target_ids_sql="'541d60a4-3486-4e41-a34b-aad653d4ac89'::uuid,
  '61d4fb6f-b211-491e-8edc-d160efefe17e'::uuid,
  '6ce40aba-1e7c-589e-87cd-cb8b028bce8f'::uuid,
  '8f3f097c-34e7-4fdf-a492-35addb2ab8f5'::uuid,
  'a6b5a5ad-6756-540d-b83a-6f25ef4d073b'::uuid,
  'ad0aeacd-f419-592b-94a6-94693399cd8c'::uuid,
  'f2db61f9-ec1a-5694-8e18-5ca5953aecce'::uuid"

production_before=$(scalar "$production" "
  SELECT encode(public.digest(convert_to(coalesce(string_agg(
    to_jsonb(value)::text,E'\\n' ORDER BY to_jsonb(value)::text
  ),''),'UTF8'),'sha256'),'hex')
  FROM (
    SELECT evidence_id,content_sha256,status,source_system
    FROM memory.evidence
    WHERE owner_user_id='$owner'::uuid
      AND evidence_id IN ($target_ids_sql)
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
  -v evidence_id=541d60a4-3486-4e41-a34b-aad653d4ac89 \
  -v content_sha256=2a3e2770b0eea1a587c9dd14fafd6a4cc62906a02792849566468cc0a31d2cfd \
  -v prior_packet_id=0f5fca46-1885-5c5d-aa9a-bb814ed56e43 \
  -v prior_packet_storage_sha256=41ad10f8b1be318b6d59e4d408ef00d811db70a0173cba8eec4654e04d4b0e68 \
  -v operation_id=1072c369-2745-5ea5-a43f-f21a8a1e0ba2 \
  -v job_id=bcab0f19-5f3c-54b3-97e1-deac045021f0 \
  -v terminal_id=1c9280ec-e637-5378-8010-bbfab2b0d42a \
  -v manifest_sha256="$manifest" \
  -v compiler_sha256="$compiler" \
  <"$test_sql" >/dev/null

docker exec -i "$container" psql -U sage -d "$clone" -X \
  -v ON_ERROR_STOP=1 >/dev/null <<SQL
BEGIN;
SELECT set_config('app.user_id','$owner',true);
SET LOCAL SESSION AUTHORIZATION brains_app;

SELECT result.*
FROM (VALUES
  ('1072c369-2745-5ea5-a43f-f21a8a1e0ba2'::uuid,'bcab0f19-5f3c-54b3-97e1-deac045021f0'::uuid,'1c9280ec-e637-5378-8010-bbfab2b0d42a'::uuid,'541d60a4-3486-4e41-a34b-aad653d4ac89'::uuid,'2a3e2770b0eea1a587c9dd14fafd6a4cc62906a02792849566468cc0a31d2cfd','0f5fca46-1885-5c5d-aa9a-bb814ed56e43'::uuid,'41ad10f8b1be318b6d59e4d408ef00d811db70a0173cba8eec4654e04d4b0e68'),
  ('dbf6e9be-138c-5048-ae29-7aef64efc6dc'::uuid,'ad81be18-93b0-5907-aefa-9a03e00e79cf'::uuid,'b7e4ddcc-0af1-5bbd-9ba7-eefa0f519c85'::uuid,'61d4fb6f-b211-491e-8edc-d160efefe17e'::uuid,'966b7a3d4c47e77bae6b7eaea87d8c8a88d268d3d48dcf3fa9c20dbca3dc41e8','083917f3-d95d-5408-a7e0-358997d28444'::uuid,'e8d39e7ae945d9b0f2ae9bc0587ee536a2b9953756fc0a8e38cff13dd4dc56c6'),
  ('cfd48636-04bf-5230-b738-0ce0206a529d'::uuid,'fa71da24-d450-5447-8f4d-766c4d4ab446'::uuid,'1279f066-6e56-5c86-ab73-e27023121547'::uuid,'6ce40aba-1e7c-589e-87cd-cb8b028bce8f'::uuid,'c5ceb3f3008c9308b61e4244f9203c44ddf37a74f5bd65714907c4eef7a1fd73','0b3b9f75-b141-5028-99c0-132e3dc27499'::uuid,'5e942fb90b770d51a76896a895570177ec6d534ae5e2f39c160d361341d7c187'),
  ('753cafeb-f198-5d96-9311-e422059568e9'::uuid,'93c0eec3-8968-5aa7-b5de-e81f27aac982'::uuid,'37b49e89-e6b5-569f-bea3-663f33532dd7'::uuid,'8f3f097c-34e7-4fdf-a492-35addb2ab8f5'::uuid,'48df291f0de8b18aba122e0ed0db915f0c98d3de33f478ff41822899d62c2977','ea8f9293-2c08-5713-a56e-2d5ab592f562'::uuid,'aa27d3b6bc059100f6782e1d3e7177bbb75b4c40a16b421f4c73cdcbabe7f31f'),
  ('eaa8f4cc-6da0-519f-b064-a0e397215edb'::uuid,'18525bf1-a1d6-579e-8252-d12296f64b6d'::uuid,'0e5fd9da-b8b6-55fa-95a9-2019fe1e49ad'::uuid,'a6b5a5ad-6756-540d-b83a-6f25ef4d073b'::uuid,'3bdca19ebc481a49ab7c7e00510e1d178b31ebf73fc7d0e2165cfc3e7bab06bf','296d260b-c0a8-5cf5-9861-ff587ad70b7d'::uuid,'e6dcdfd316dadb1455adf3aa5b613df5420911572b1f03d9a45860526aca14df'),
  ('4469cc65-b7fe-581f-b2fd-7cd249fa53e7'::uuid,'46b47a61-68d6-587c-8daf-f009e2de76f6'::uuid,'670b0089-f011-5d64-8b14-a89ec6c98762'::uuid,'ad0aeacd-f419-592b-94a6-94693399cd8c'::uuid,'841bcbb1937b5b6f610bb81a769ee62954aff803e125b572884091c50fa07ad2','21969e61-ab42-5058-831d-6ca3ba865d3d'::uuid,'d9f4cdd8462aec43ffaa734386a0844ce60c7d095ba843000b9ab31b4cc2888a'),
  ('dd80c39c-475a-5876-adc4-e64bfd677bdb'::uuid,'547a1438-e984-538c-8739-4a4dc53b283c'::uuid,'040dd3d9-15a7-5f5d-b525-bba19c1c5de1'::uuid,'f2db61f9-ec1a-5694-8e18-5ca5953aecce'::uuid,'8fe05a934b55a77ac6f34945a1e3d9f9e75d5a996c6e8032bc9ac506ef3d1a13','fa1231ed-b88c-5283-b234-d36b7904cea0'::uuid,'096a107b04a864b206cb1fb5d9a56c8501e92dd9d230b5026191b1d96a1e2e28')
) AS item(operation_id,job_id,terminal_id,evidence_id,content_sha256,prior_packet_id,prior_storage_sha256)
CROSS JOIN LATERAL memory.enqueue_owner_v5_2_semantic_compiler_v9_reextract_v1(
  item.operation_id,item.job_id,item.terminal_id,item.evidence_id,
  item.content_sha256,item.prior_packet_id,item.prior_storage_sha256,
  '$manifest','$compiler'
) AS result;
RESET SESSION AUTHORIZATION;

DO \$verify_exact_seven\$
BEGIN
  IF (
    SELECT count(*) FROM memory.evidence_extraction_job
    WHERE owner_user_id='$owner'::uuid
      AND selector_version='$selector'
      AND status='pending'
      AND attempts=0
  )<>7
  OR (
    SELECT count(*) FROM memory.evidence_intake_terminal
    WHERE owner_user_id='$owner'::uuid
      AND selector_version='$selector'
      AND details->>'policy_compiler_sha256'='$compiler'
      AND details ? 'prior_packet_id'
      AND details ? 'prior_packet_storage_sha256'
  )<>7 THEN
    RAISE EXCEPTION 'exact-seven compiler-v9 binding failed';
  END IF;
END
\$verify_exact_seven\$;

SET LOCAL SESSION AUTHORIZATION brains_app;
SELECT * FROM memory.enqueue_owner_v5_2_semantic_compiler_v9_reextract_v1(
  'dd80c39c-475a-5876-adc4-e64bfd677bdb',
  '547a1438-e984-538c-8739-4a4dc53b283c',
  '040dd3d9-15a7-5f5d-b525-bba19c1c5de1',
  'f2db61f9-ec1a-5694-8e18-5ca5953aecce',
  '8fe05a934b55a77ac6f34945a1e3d9f9e75d5a996c6e8032bc9ac506ef3d1a13',
  'fa1231ed-b88c-5283-b234-d36b7904cea0',
  '096a107b04a864b206cb1fb5d9a56c8501e92dd9d230b5026191b1d96a1e2e28',
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
      AND evidence_id IN ($target_ids_sql)
    ORDER BY evidence_id
  ) AS value")" == "$production_before" ]]
[[ "$(scalar "$production" "
  SELECT count(*) FROM memory.evidence_extraction_job
  WHERE selector_version='$selector'")" == "$selector_before" ]]
[[ "$(qdrant_signature)" == "$qdrant_before" ]]
[[ "$(systemctl is-active brains.service)" == active ]]

printf '%s\n' \
  'memory_v1_v5_2_semantic_compiler_v9_reextract_clone: PASS'
