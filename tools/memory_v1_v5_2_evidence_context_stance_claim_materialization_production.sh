#!/usr/bin/env bash
set -euo pipefail

# seebx backend only. Materializes exactly one reviewed reported-stance claim.
# It does not write Qdrant, activate retrieval, or influence prompts.

if [[ "${MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STANCE_CLAIM_MATERIALIZATION:-}" != authorized ]]; then
  echo 'MEMORY_V1_V5_2_EVIDENCE_CONTEXT_STANCE_CLAIM_MATERIALIZATION=authorized is required' >&2
  exit 1
fi
if [[ "$#" -ne 4 ]]; then
  echo 'usage: stance_claim_materialization_production.sh MANIFEST PREFLIGHT APPLY REPLAY' >&2
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
  0685b77e-de79-5216-b58d-6d7e464166e6 ]]
[[ "$(jq -er '.items[0].predicate' "$manifest")" == stance.reported ]]
[[ "$(jq -er '.items[0].observation_count' "$manifest")" == 1 ]]
[[ "$(jq -er '.expected_insert_rows' "$manifest")" == 11 ]]
[[ "$(jq -er '.expected_mutated_rows' "$manifest")" == 12 ]]
[[ "$(jq -er '.defer_projection_outbox' "$manifest")" == true ]]
[[ "$(jq -er '.assessment.action' "$manifest")" == promote_supported ]]
[[ "$(jq -er '.assessment.reason_codes|index("attributed_belief_only") != null' \
  "$manifest")" == true ]]
[[ "$(jq -er '.review_manifest_sha256' "$manifest")" == \
  0e51eb5ea5c6a250de3e427f5e04237b2d57ce916763102f4ab182cbfb6ea264 ]]
[[ "$(jq -er '.review_result_sha256' "$manifest")" == \
  14a73104e089f643e5eff6e981403509af51936634287924070d7f576e37a32e ]]
[[ "$(jq -er '.items[0].canonical_text_sha256' "$manifest")" == \
  48202a3979224024549a894df7eb71cf4772fc474ab199d5cb93aafa5725ab90 ]]
[[ "$(jq -er '.items[0].observation_id' "$review_manifest")" == \
  87ce1a11-01ae-4d6f-80ea-8e62b5b43cff ]]

export GIT_OPTIONAL_LOCKS=0
exec env \
  MEMORY_V1_V5_2_COMPILER_V8_CLAIM_MATERIALIZATION_APPLY=authorized \
  MEMORY_V1_V5_2_MATERIALIZATION_EXPECTED_ITEMS=1 \
  MEMORY_V1_V5_2_MATERIALIZATION_EXPECTED_PREDICATES=stance.reported \
  "$repo_root/tools/memory_v1_v5_2_compiler_v8_claim_materialization_production.sh" \
  "$@"
