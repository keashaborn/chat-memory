#!/usr/bin/env bash
set -euo pipefail

base_url="${1:-http://127.0.0.1:18082}"
key_file="${2:-/etc/memory-v1-local-inference/vs_memory_gpu_api_key}"
noauth_file="$(mktemp)"
auth_file="$(mktemp)"
trap 'rm -f "$noauth_file" "$auth_file"' EXIT

payload='{"model":"qwen3-14b-local-extractor","messages":[{"role":"user","content":"Return exactly OK."}],"temperature":0,"max_tokens":8}'
unauth_code="$(curl -sS -o "$noauth_file" -w '%{http_code}' \
  -H 'Content-Type: application/json' \
  -d "$payload" \
  "$base_url/v1/chat/completions")"

api_key="$(sed -n '1p' "$key_file")"
auth_code="$(curl -sS -o "$auth_file" -w '%{http_code}' \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${api_key}" \
  -d "$payload" \
  "$base_url/v1/chat/completions")"
unset api_key

content="$(jq -r '.choices[0].message.content // .error.message // empty' "$auth_file")"
response_sha256="$(sha256sum "$auth_file" | cut -d' ' -f1)"

printf 'unauthorized=%s authorized=%s content=%s response_sha256=%s\n' \
  "$unauth_code" "$auth_code" "$content" "$response_sha256"

test "$unauth_code" = 401
test "$auth_code" = 200
test "$content" = OK
