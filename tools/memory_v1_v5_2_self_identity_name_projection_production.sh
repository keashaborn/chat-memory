#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Projects exactly one reviewed self-name claim, then runs
# owner-only shadow retrieval. It never activates answer or prompt influence.

if [[ "${MEMORY_V1_V5_2_SELF_IDENTITY_NAME_PROJECTION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_SELF_IDENTITY_NAME_PROJECTION=authorized is required' >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
plan="$repo_root/evals/memory_v1_v5_2_self_identity_name_claim_projection_plan.json"
expected_plan_sha=813ba4c1bf130a61a37d9a2dea51d6da49fced8253caef1c5f7ada77f62335db

[[ "$(sha256sum "$plan" | awk '{print $1}')" == "$expected_plan_sha" ]]
[[ "$(jq -er '.contract_version' "$plan")" == \
  memory_v1_v5_2_self_identity_name_claim_projection_plan_v1 ]]
[[ "$(jq -er '.owner_user_id' "$plan")" == \
  1240822d-ac9a-4096-95aa-e2b24d36ef50 ]]
[[ "$(jq -er '.items|length' "$plan")" == 1 ]]
[[ "$(jq -er '.items[0].claim_id' "$plan")" == \
  93f717d1-ebf5-4b19-8271-2bd069e2a8be ]]
[[ "$(jq -er '.items[0].predicate' "$plan")" == identity.name ]]
[[ "$(jq -er '.shadow_queries["identity.name"]' "$plan")" == \
  "What is my name?" ]]

export GIT_OPTIONAL_LOCKS=0
exec env \
  MEMORY_V1_V5_2_CANONICAL_NAME_PROJECTION=authorized \
  MEMORY_V1_V5_2_PROJECTION_PLAN_PATH="$plan" \
  MEMORY_V1_V5_2_PROJECTION_PLAN_SHA256="$expected_plan_sha" \
  MEMORY_V1_V5_2_PROJECTION_PROJECT_RUNNER=scripts/memory_v1_v5_2_self_identity_name_controlled_project.py \
  MEMORY_V1_V5_2_PROJECTION_ARTIFACT_LABEL=self-identity-name-projection \
  MEMORY_V1_V5_2_PROJECTION_REPORT_CONTRACT=memory_v1_v5_2_self_identity_name_projection_report_v1 \
  "$repo_root/tools/memory_v1_v5_2_canonical_name_projection_production.sh"
