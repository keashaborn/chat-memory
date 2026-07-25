#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Reads three exact owner-scoped evidence rows from a
# disposable production clone and exercises private V5.2 inference. Production
# Postgres, Qdrant, Redis, retrieval, and prompts remain read-only.

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
set -a
source "${MEMORY_V1_ENV_FILE:-/opt/chat-memory/.env}"
set +a

python_bin=/opt/chat-memory/venv/bin/python
container=brains-postgres-1
production=memory
clone="memory_v5_v5_2_semantic_compiler_v8_${$}"
owner=1240822d-ac9a-4096-95aa-e2b24d36ef50
verifier=scripts/memory_v1_v5_2_semantic_compiler_v8_clone_verify.py
backup=$(mktemp /tmp/memory-v5-2-semantic-compiler-v8.XXXXXX.dump)
report_dir=/home/ubuntu/memory-v1-reviews
report="$report_dir/semantic-compiler-v8-clone-$(git rev-parse --short HEAD).json"
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

target_fingerprint() {
  docker exec "$container" psql -U sage -d "$production" -X -Atqc "
    SELECT encode(
      public.digest(
        convert_to(
          coalesce(string_agg(
            evidence_id::text || ':' || content_sha256 || ':' || status::text,
            ',' ORDER BY evidence_id
          ), ''),
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    )
    FROM memory.evidence
    WHERE owner_user_id='$owner'::uuid
      AND evidence_id IN (
        '33126656-fc5a-5fc1-a035-246b14576ee5'::uuid,
        'fea59e7e-30f5-4139-b634-97b291c88e14'::uuid,
        'dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5'::uuid
      )"
}

before_target=$(target_fingerprint)
before_qdrant=$(curl -fsS \
  http://127.0.0.1:6333/collections/memory_claim_v1 \
  | jq -r '.result.points_count')

docker exec "$container" pg_dump -U sage -d "$production" -Fc >"$backup"
[[ -s "$backup" ]]
docker exec "$container" createdb -U sage -T template0 "$clone"
docker exec -i "$container" pg_restore -U sage -d "$clone" \
  --exit-on-error <"$backup"

clone_dsn=$("$python_bin" -c \
  'import sys; from urllib.parse import urlsplit,urlunsplit; u=urlsplit(sys.argv[1]); print(urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment)))' \
  "$POSTGRES_DSN" "$clone")
mkdir -p "$report_dir"
chmod 0700 "$report_dir"
export POSTGRES_DSN="$clone_dsn"
export MEMORY_V1_REPO_ROOT="$repo_root"
export MEMORY_V1_LOCAL_INFERENCE_API_KEY
MEMORY_V1_LOCAL_INFERENCE_API_KEY=$(cat \
  /etc/memory-v1-local-inference/api-key)

PYTHONPATH="$repo_root" "$python_bin" "$verifier" >"$report"
chmod 0600 "$report"
chown ubuntu:ubuntu "$report"

jq -e '
  .passed == true and
  .compiler_version == "memory_v1_semantic_policy_compiler_v8" and
  .owner_isolation_visible_rows == 0 and
  .external_model_calls == 0 and
  .local_model_calls == 1 and
  (.cases | length) == 3 and
  .effects == {
    "clone_database_writes":0,
    "production_database_writes":0,
    "prompt_influence":0,
    "qdrant_writes":0,
    "redis_writes":0
  }
' "$report" >/dev/null

after_target=$(target_fingerprint)
after_qdrant=$(curl -fsS \
  http://127.0.0.1:6333/collections/memory_claim_v1 \
  | jq -r '.result.points_count')
[[ "$before_target" == "$after_target" ]]
[[ "$before_qdrant" == "$after_qdrant" ]]
[[ "$(systemctl is-active brains.service)" == active ]]
[[ "$(curl -sS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:8088/docs)" == 200 ]]

printf 'REPORT=%s\n' "$report"
printf '%s\n' 'memory_v1_v5_2_semantic_compiler_v8_production_clone: PASS'
