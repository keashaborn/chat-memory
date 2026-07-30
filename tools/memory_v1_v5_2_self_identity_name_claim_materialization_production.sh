#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Materializes exactly one reviewed self-name claim.
# It does not write Qdrant, activate retrieval, or influence prompts.

if [[ "${MEMORY_V1_V5_2_SELF_IDENTITY_NAME_CLAIM_MATERIALIZATION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_SELF_IDENTITY_NAME_CLAIM_MATERIALIZATION=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 4 ]]; then
  echo 'usage: self_identity_name_claim_materialization_production.sh MANIFEST PREFLIGHT APPLY REPLAY' >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
manifest=$(realpath "$1")
review_manifest=$(jq -er '.review_manifest_path' "$manifest")

[[ "$(jq -er '.contract_version' "$manifest")" == \
  memory_v1_claim_projection_apply_batch_manifest_v1 ]]
[[ "$(jq -er '.owner_user_id' "$manifest")" == \
  1240822d-ac9a-4096-95aa-e2b24d36ef50 ]]
[[ "$(jq -er '.items|length' "$manifest")" == 1 ]]
[[ "$(jq -er '.items[0].plan_id' "$manifest")" == \
  0b1a280d-6170-5f6a-a738-adc342188c89 ]]
[[ "$(jq -er '.items[0].predicate' "$manifest")" == identity.name ]]
[[ "$(jq -er '.items[0].observation_count' "$manifest")" == 1 ]]
[[ "$(jq -er '.expected_insert_rows' "$manifest")" == 11 ]]
[[ "$(jq -er '.expected_mutated_rows' "$manifest")" == 12 ]]
[[ "$(jq -er '.defer_projection_outbox' "$manifest")" == true ]]
[[ "$(jq -er '.assessment.action' "$manifest")" == promote_supported ]]
[[ "$(jq -er '.assessment.reason_codes|index("owner_authored_identity") != null' \
  "$manifest")" == true ]]
[[ "$(jq -er '.assessment.reason_codes|index("trusted_owner_self_binding") != null' \
  "$manifest")" == true ]]
[[ "$(jq -er '.review_manifest_sha256' "$manifest")" == \
  e82d1553dc1b0090c50b28f70fbf0a35c6fc08361d9cab92de21fc7635bf5c7f ]]
[[ "$(jq -er '.review_result_sha256' "$manifest")" == \
  f27c9be942022a497a40a88eb64eaf2e51a6e6e89464e0754164dead9e2d9997 ]]
[[ "$(jq -er '.items[0].canonical_text_sha256' "$manifest")" == \
  88f9fa4e3f33c58cd5dae06deb9db8c2ee6a1406b0a4ba55794f1ca89ba1092b ]]
[[ "$(jq -er '.items[0].observation_id' "$review_manifest")" == \
  fc86c43e-3fa6-465e-b348-8656e3a896c0 ]]

export GIT_OPTIONAL_LOCKS=0
exec env \
  MEMORY_V1_V5_2_COMPILER_V8_CLAIM_MATERIALIZATION_APPLY=authorized \
  MEMORY_V1_V5_2_MATERIALIZATION_EXPECTED_ITEMS=1 \
  MEMORY_V1_V5_2_MATERIALIZATION_EXPECTED_PREDICATES=identity.name \
  "$repo_root/tools/memory_v1_v5_2_compiler_v8_claim_materialization_production.sh" \
  "$@"
