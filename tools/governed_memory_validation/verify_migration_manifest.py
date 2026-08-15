#!/usr/bin/env python3
"""Fail closed unless the governed Memory migration package bytes match."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


EXPECTED_PACKAGES = (
    "0001_foundation/package.json",
    "0003_owner_claim_detail/package.json",
    "0004_pilot_marker/package.json",
    "0005_bounded_auto_admission/package.json",
    "0006_source_erasure_projection_recovery/package.json",
    "0007_personal_object_location_admission/package.json",
    "0002_conversation_bridge/package.json",
)
EXPECTED_FILES = {
    "roles_preflight.pgsql",
    "schema_contract.json",
    "predicate_catalog.json",
    "0001_foundation/package.json",
    "0001_foundation/forward.pgsql",
    "0001_foundation/rollback.pgsql",
    "0003_owner_claim_detail/package.json",
    "0003_owner_claim_detail/forward.pgsql",
    "0003_owner_claim_detail/rollback.pgsql",
    "0004_pilot_marker/package.json",
    "0004_pilot_marker/forward.pgsql",
    "0004_pilot_marker/rollback.pgsql",
    "0005_bounded_auto_admission/package.json",
    "0005_bounded_auto_admission/forward.pgsql",
    "0005_bounded_auto_admission/rollback.pgsql",
    "0006_source_erasure_projection_recovery/package.json",
    "0006_source_erasure_projection_recovery/forward.pgsql",
    "0006_source_erasure_projection_recovery/rollback.pgsql",
    "0007_personal_object_location_admission/package.json",
    "0007_personal_object_location_admission/disposable_proof_receipt.json",
    "0007_personal_object_location_admission/live_activation_receipt.json",
    "0007_personal_object_location_admission/forward.pgsql",
    "0007_personal_object_location_admission/rollback.pgsql",
    "0002_conversation_bridge/package.json",
    "0002_conversation_bridge/forward.pgsql",
    "0002_conversation_bridge/rollback.pgsql",
}
EXPECTED_EXECUTION_ORDER = [
    "roles_preflight.pgsql",
    "0001_foundation/forward.pgsql",
    "0003_owner_claim_detail/forward.pgsql",
    "0004_pilot_marker/forward.pgsql",
    "0005_bounded_auto_admission/forward.pgsql",
    "0006_source_erasure_projection_recovery/forward.pgsql",
    "0007_personal_object_location_admission/forward.pgsql",
    "0002_conversation_bridge/forward.pgsql",
]
EXPECTED_ROLLBACK_ORDER = [
    "0002_conversation_bridge/rollback.pgsql",
    "0007_personal_object_location_admission/rollback.pgsql",
    "0006_source_erasure_projection_recovery/rollback.pgsql",
    "0005_bounded_auto_admission/rollback.pgsql",
    "0004_pilot_marker/rollback.pgsql",
    "0003_owner_claim_detail/rollback.pgsql",
    "0001_foundation/rollback.pgsql",
]
VALIDATED_STATUS = "isolated_candidate_disposable_validated_not_production_applied"
PERSONAL_ACTIVE_STATUS = "production_database_applied_live_acceptance_pending"
PERSONAL_MANIFEST_STATUS = (
    "personal_object_location_production_applied_live_acceptance_pending"
)
EXPECTED_PACKAGE_CONTRACTS = {
    "0001_foundation/package.json": {
        "status": VALIDATED_STATUS,
        "rollback_empty_only": True,
        "activation": {
            "production_authorized": False,
            "production_services_changed": False,
            "production_database_applied": False,
            "production_qdrant_changed": False,
            "disposable_database_validated": True,
            "disposable_qdrant_validated": True,
        },
    },
    "0003_owner_claim_detail/package.json": {
        "status": VALIDATED_STATUS,
        "rollback_empty_only": False,
        "rollback_data_mutation": False,
        "activation": {
            "production_authorized": False,
            "production_services_changed": False,
            "production_database_applied": False,
            "disposable_database_validated": True,
        },
    },
    "0004_pilot_marker/package.json": {
        "status": VALIDATED_STATUS,
        "rollback_empty_only": True,
        "activation": {
            "production_authorized": False,
            "production_database_applied": False,
            "production_services_changed": False,
            "disposable_database_validated": True,
        },
    },
    "0005_bounded_auto_admission/package.json": {
        "status": "authorized_candidate_pending_live_application",
        "rollback_empty_only": False,
        "activation": {
            "production_authorized": True,
            "production_database_applied": False,
            "production_services_changed": False,
            "live_acceptance_pending": True,
        },
    },
    "0006_source_erasure_projection_recovery/package.json": {
        "status": "authorized_candidate_pending_live_application",
        "rollback_empty_only": False,
        "activation": {
            "production_authorized": True,
            "production_database_applied": False,
            "production_services_changed": False,
            "live_acceptance_pending": True,
        },
    },
    "0007_personal_object_location_admission/package.json": {
        "status": PERSONAL_ACTIVE_STATUS,
        "rollback_empty_only": False,
        "activation": {
            "production_authorized": True,
            "production_database_applied": True,
            "production_services_changed": False,
            "disposable_database_validated": True,
            "worker_timer_quiesced_and_restored": True,
            "live_catalog_verified": True,
            "live_acceptance_pending": True,
        },
    },
    "0002_conversation_bridge/package.json": {
        "status": VALIDATED_STATUS,
        "rollback_empty_only": True,
        "activation": {
            "production_authorized": False,
            "production_writer_membership_granted": False,
            "production_services_changed": False,
            "production_database_applied": False,
            "disposable_writer_membership_validated": True,
            "disposable_database_validated": True,
            "historical_phase7c_disposable_validation_retained": True,
            "blockers": [
                "lifeswitch_chat_answer_binding_provenance_and_owner_context_"
                "erasure_not_implemented_or_verified",
                "lifeswitch_prior_provenance_view_not_migrated_to_chat_integrity",
                "vantage_answer_trace_erasure_or_retention_disposition_not_"
                "decided_or_verified",
                "telemetry_payload_erasure_or_retention_disposition_not_"
                "decided_or_verified",
                "conversation_erasure_auxiliary_deleted_object_counts_not_"
                "implemented_or_verified",
                "successor_answer_binding_chat_transaction_recovery_not_implemented",
            ],
        },
    },
}
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def expected_package_contracts() -> dict[str, dict[str, object]]:
    return {
        relative: {
            **contract,
            "activation": {
                key: list(value) if isinstance(value, list) else value
                for key, value in contract["activation"].items()
            },
        }
        for relative, contract in EXPECTED_PACKAGE_CONTRACTS.items()
    }


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, object]:
    def reject_nonfinite(value: str) -> object:
        raise ValueError(f"non-finite JSON value: {value}")

    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_nonfinite,
    )
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def checked_path(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise ValueError(f"unsafe manifest path: {relative!r}")
    candidate = (root / relative).resolve(strict=True)
    candidate.relative_to(root)
    if not candidate.is_file():
        raise ValueError(f"manifest entry is not a file: {relative}")
    return candidate


def verify(root: Path) -> dict[str, object]:
    root = root.resolve(strict=True)
    manifest_path = root / "manifest.json"
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != "governed-memory-migration-manifest-v2":
        raise ValueError("unexpected migration manifest schema")
    candidate_id = manifest.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("missing migration candidate id")
    if manifest.get("status") != PERSONAL_MANIFEST_STATUS:
        raise ValueError("unexpected migration candidate status")
    expected_authority = {
        "production_apply_authorized": True,
        "production_service_change_authorized": True,
        "production_provider_call_authorized": False,
        "production_qdrant_change_authorized": False,
        "legacy_import_authorized": False,
        "disposable_validation_authorized": False,
        "bounded_automatic_admission_authorized": True,
        "personal_object_location_admission_candidate_authorized": True,
    }
    if manifest.get("authority") != expected_authority:
        raise ValueError("unexpected migration authority contract")
    if manifest.get("execution_order") != EXPECTED_EXECUTION_ORDER:
        raise ValueError("unexpected migration execution order")
    if manifest.get("rollback_order") != EXPECTED_ROLLBACK_ORDER:
        raise ValueError("unexpected migration rollback order")
    expected_safety = {
        "migration_runner_transaction_required": True,
        "migration_runner_timeouts_required": True,
        "migration_runner_advisory_lock_required": True,
        "rollback_empty_only": False,
        "claim_detail_rollback_data_mutation": False,
        "pilot_marker_rollback_empty_only": True,
        "cascade_ddl_allowed": False,
        "disposable_database_execution_performed": True,
        "production_database_execution_performed": True,
        "production_checkout_files_changed": True,
        "production_data_read": False,
        "provider_external_calls": 0,
        "bounded_automatic_admission_live_acceptance_pending": True,
        "personal_object_location_admission_synthetic_tests_performed": True,
        "personal_object_location_admission_disposable_database_execution_performed": True,
        "personal_object_location_admission_production_database_execution_performed": True,
        "personal_object_location_admission_live_acceptance_pending": True,
    }
    if manifest.get("safety") != expected_safety:
        raise ValueError("unexpected migration safety contract")

    expected: dict[str, str] = {}
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("migration manifest files must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ValueError("invalid migration manifest file entry")
        relative = entry.get("path")
        wanted = entry.get("sha256")
        if not isinstance(relative, str):
            raise ValueError("invalid migration manifest path")
        if relative in expected:
            raise ValueError(f"duplicate manifest path: {relative}")
        if not isinstance(wanted, str) or HEX_SHA256.fullmatch(wanted) is None:
            raise ValueError(f"invalid manifest digest: {relative}")
        expected[relative] = wanted
        actual = sha256_file(checked_path(root, relative))
        if actual != wanted:
            raise ValueError(
                f"migration byte mismatch: {relative}: expected {wanted}, got {actual}"
            )
    if set(expected) != EXPECTED_FILES:
        raise ValueError("migration manifest file set is not exact")
    observed = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if observed != EXPECTED_FILES:
        raise ValueError("migration directory contains an undeclared file")

    schema_contract = load_json(root / "schema_contract.json")
    if schema_contract.get("status") != VALIDATED_STATUS:
        raise ValueError("unexpected schema contract status")
    validation_scope = schema_contract.get("validation_scope")
    if validation_scope != {
        "scope": "phase8g_current_candidate",
        "environment": "disposable_postgresql_qdrant_synthetic_only",
        "current_candidate_disposable_validated": True,
        "historical_phase7c_disposable_proof_retained": True,
        "disposable_revalidation_required": False,
        "production_data_read": False,
        "provider_external_calls": 0,
        "production_state_changed": False,
    }:
        raise ValueError("unexpected schema validation scope")

    package_contracts = expected_package_contracts()
    for package_relative in EXPECTED_PACKAGES:
        if package_relative not in expected:
            raise ValueError(f"package missing from manifest: {package_relative}")
        package_path = checked_path(root, package_relative)
        package = load_json(package_path)
        if package.get("schema_version") != "governed-memory-migration-package-v2":
            raise ValueError(f"unexpected package schema: {package_relative}")
        expected_contract = package_contracts[package_relative]
        if package.get("status") != expected_contract["status"]:
            raise ValueError(f"unexpected package status: {package_relative}")
        if package.get("transaction") != {
            "required": True,
            "runner_supplies_begin_commit": True,
            "runner_supplies_timeouts": True,
            "runner_supplies_advisory_lock": True,
        }:
            raise ValueError(f"unsafe transaction contract: {package_relative}")
        rollback = package.get("rollback")
        if not isinstance(rollback, dict) or rollback.get("empty_only") is not (
            expected_contract["rollback_empty_only"]
        ):
            raise ValueError(f"unexpected rollback contract: {package_relative}")
        if "rollback_data_mutation" in expected_contract and rollback.get(
            "data_mutation"
        ) is not expected_contract["rollback_data_mutation"]:
            raise ValueError(
                f"unexpected rollback data-mutation contract: {package_relative}"
            )
        object_contract = package.get("object_contract")
        if not isinstance(object_contract, dict) or object_contract.get(
            "cascade_ddl"
        ) is not False:
            raise ValueError(f"CASCADE is not prohibited: {package_relative}")
        if package.get("activation") != expected_contract["activation"]:
            raise ValueError(f"unexpected activation contract: {package_relative}")
        package_dir = Path(package_relative).parent
        for direction in ("forward", "rollback"):
            contract = package.get(direction, {})
            if not isinstance(contract, dict) or contract.get("path") != (
                f"{direction}.pgsql"
            ):
                raise ValueError(
                    f"unexpected package path: {package_relative}:{direction}"
                )
            relative = (package_dir / contract["path"]).as_posix()
            wanted = contract.get("sha256")
            if expected.get(relative) != wanted:
                raise ValueError(
                    f"package/manifest digest disagreement: {package_relative}:{direction}"
                )
            actual = sha256_file(checked_path(root, relative))
            if actual != wanted:
                raise ValueError(
                    f"package byte mismatch: {package_relative}:{direction}"
                )
            sql = checked_path(root, relative).read_text(encoding="utf-8")
            if re.search(
                r"\bDROP\b[^;]*\bCASCADE\b",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            ):
                raise ValueError(f"CASCADE DDL present: {relative}")

    proof_relative = (
        "0007_personal_object_location_admission/disposable_proof_receipt.json"
    )
    proof_path = checked_path(root, proof_relative)
    proof_file_sha256 = sha256_file(proof_path)
    personal_package = load_json(
        root / "0007_personal_object_location_admission/package.json"
    )
    if personal_package.get("disposable_proof") != {
        "path": "disposable_proof_receipt.json",
        "sha256": proof_file_sha256,
    }:
        raise ValueError("personal object disposable proof binding differs")
    if expected.get(proof_relative) != proof_file_sha256:
        raise ValueError("personal object disposable proof manifest differs")
    proof = load_json(proof_path)
    if set(proof) != {
        "schema_version", "proof_scope", "proof_receipt_canonicalization",
        "proof_receipt_canonical_sha256", "proof_receipt",
        "closing_verification",
    }:
        raise ValueError("personal object disposable proof shape differs")
    if (
        proof.get("schema_version")
        != "governed-memory-personal-object-location-disposable-proof-v2"
        or proof.get("proof_scope")
        != "inactive_personal_object_location_admission_migration_0007_with_activation_cutoff"
        or proof.get("proof_receipt_canonicalization")
        != "utf8_json_sorted_keys_compact_no_newline_v1"
    ):
        raise ValueError("personal object disposable proof contract differs")
    receipt = proof.get("proof_receipt")
    if not isinstance(receipt, dict) or set(receipt) != {
        "candidate_base_head", "forward_sha256", "rollback_sha256",
        "image_id", "migration_chain", "activation_cutoff",
        "prior_full_pipeline_proof",
        "production_data_read", "production_state_changed", "provider_calls",
        "result",
    }:
        raise ValueError("personal object disposable receipt shape differs")
    if canonical_json_sha256(receipt) != proof.get(
        "proof_receipt_canonical_sha256"
    ):
        raise ValueError("personal object disposable receipt hash differs")
    package_forward = personal_package.get("forward")
    package_rollback = personal_package.get("rollback")
    policy = personal_package.get("policy")
    cutoff = receipt.get("activation_cutoff")
    prior_pipeline = receipt.get("prior_full_pipeline_proof")
    if (
        not isinstance(package_forward, dict)
        or not isinstance(package_rollback, dict)
        or not isinstance(policy, dict)
        or not isinstance(cutoff, dict)
        or not isinstance(prior_pipeline, dict)
        or receipt.get("candidate_base_head")
        != "be543dc10c5b6f65495cdc9068501c9f125c92ae"
        or receipt.get("forward_sha256") != package_forward.get("sha256")
        or receipt.get("rollback_sha256") != package_rollback.get("sha256")
        or policy.get("pre_activation_proposals_eligible") is not False
        or policy.get("activation_boundary_source")
        != "migration_transaction_timestamp"
        or receipt.get("result") != "pass"
        or receipt.get("production_data_read") is not False
        or receipt.get("production_state_changed") is not False
        or receipt.get("provider_calls") != 0
        or cutoff != {
            "policy_rows": 1,
            "pre_activation_eligible": False,
            "post_activation_eligible": True,
            "selector_gate_present": True,
            "worker_policy_table_select": False,
            "api_policy_table_select": False,
            "existing_self_reason": "automatic_low_risk_owner_assertion",
            "personal_object_location_reason":
                "automatic_low_risk_personal_object_location",
            "clean_rollback": "policy-absent|helper-absent|legacy-restored",
            "reapply": "policy-present|policy-rows-1|helper-present",
        }
        or prior_pipeline.get("proof_promotion_head")
        != "be543dc10c5b6f65495cdc9068501c9f125c92ae"
        or prior_pipeline.get("proof_file_sha256")
        != "82403108a5461fb517dd061eb687977cdc240edaba72676af98cd4a98a665b2b"
        or prior_pipeline.get("proof_receipt_canonical_sha256")
        != "6d02304831fcb662801fc866d312ccb240b7d7a4d7d4d920eee3de2cc99cf92a"
        or prior_pipeline.get("admission_outcome") != "admitted"
        or prior_pipeline.get("owner_a_visible_claims") != 1
        or prior_pipeline.get("owner_b_visible_claims") != 0
        or prior_pipeline.get("projection_operation") != "upsert"
        or prior_pipeline.get("projection_state") != "pending"
        or prior_pipeline.get("reason")
        != "automatic_low_risk_personal_object_location"
    ):
        raise ValueError("personal object disposable receipt evidence differs")
    closing = proof.get("closing_verification")
    if not isinstance(closing, dict) or closing != {
        "disposable_resources_absent": True,
        "live_head": "38b978208b70f670bf2e9416de57e35ac8b905a9",
        "live_tree": "d7d6b9bb979c1f5d21feac9383d6322e445efbdb",
        "production_checkout_changed": False,
        "production_claims_or_conversations_read": False,
        "temporary_validation_artifacts_absent": True,
    }:
        raise ValueError("personal object disposable closing proof differs")

    live_relative = (
        "0007_personal_object_location_admission/live_activation_receipt.json"
    )
    live_path = checked_path(root, live_relative)
    live_file_sha256 = sha256_file(live_path)
    if personal_package.get("live_activation_proof") != {
        "path": "live_activation_receipt.json",
        "sha256": live_file_sha256,
    }:
        raise ValueError("personal object live activation proof binding differs")
    if expected.get(live_relative) != live_file_sha256:
        raise ValueError("personal object live activation proof manifest differs")
    live = load_json(live_path)
    if live != {
        "schema_version": "governed-memory-personal-object-location-live-activation-v1",
        "migration_id": "governed_memory_personal_object_location_admission_0007",
        "server": "seebx",
        "database": "governed_memory",
        "result": "pass_live_acceptance_pending",
        "activated_at": "2026-08-15T23:06:17.801691Z",
        "source": {
            "live_head": "31a3ccbd93317b859c05f01445980514d19feef4",
            "live_tree": "3fcbf0b5de7a45049515de8c8d0c696b880cb690",
            "forward_sha256": "0e065edcce8d301c5cbcdc83d7718d598c2f18dd4fbf2693472cd2894c4c05e1",
            "manifest_sha256": "17c7b1e3e10a51e22dee1d41d1233d5014d3d21cd25d55649ef7a2ca8140113c",
        },
        "database_proof": {
            "transaction_committed": True,
            "execution_session_role": "governed_memory_bootstrap",
            "execution_current_role": "governed_memory_owner",
            "advisory_lock_used": True,
            "policy_rows": 1,
            "activation_timestamp_matches_migration_transaction": True,
            "helper_present": True,
            "worker_execute": True,
            "reason_constraints_present": 3,
            "pre_activation_pending_location_proposals": 1,
            "post_activation_pending_location_proposals": 0,
            "admitted_location_proposals": 0,
            "location_claim_evidence_rows": 0,
        },
        "service_proof": {
            "worker_timer_before": "enabled_active_waiting",
            "worker_timer_during": "inactive_dead",
            "worker_timer_after": "enabled_active_waiting",
            "worker_after": "inactive_success",
            "http_after": "active",
            "brains_after": "active",
            "persistent_service_configuration_changed": False,
        },
        "authority_boundary": {
            "provider_calls_performed": 0,
            "production_claim_or_conversation_content_read": False,
            "production_aggregate_counts_read": True,
            "qdrant_direct_changes": False,
            "runtime_rebuilt": False,
            "frontend_changed": False,
            "provider_secrets_read": False,
        },
        "rollback": {
            "executed_live": False,
            "currently_schema_eligible": True,
            "becomes_guarded_after_first_location_admission": True,
        },
    }:
        raise ValueError("personal object live activation receipt differs")

    return {
        "file_count": len(expected),
        "manifest_sha256": sha256_file(manifest_path),
        "migration_package_id_sha256": hashlib.sha256(
            candidate_id.encode("utf-8")
        ).hexdigest(),
        "result": "artifact_integrity_verified",
        "schema_version": "governed-memory-migration-verification-v9",
        "validation_state": PERSONAL_MANIFEST_STATUS,
        "current_disposable_validation_complete": True,
        "disposable_revalidation_required": False,
        "historical_phase7c_proof_reusable_for_current_candidate": False,
        "production_database_applied": True,
        "production_state_changed": False,
    }


def main() -> int:
    arguments = list(sys.argv[1:])
    if len(arguments) > 1:
        raise SystemExit("usage: verify_migration_manifest.py [migration_root]")
    default_root = Path(__file__).resolve().parents[2] / "governed-memory-migrations"
    root = Path(arguments[0]) if arguments else default_root
    try:
        receipt = verify(root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"MIGRATION_MANIFEST_INVALID={error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
