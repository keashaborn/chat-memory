#!/usr/bin/env python3
from __future__ import annotations

"""Build a fail-closed disposition manifest from a content-free DB audit."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "seebx-lifeswitch-database-object-disposition-v1"
AUDIT_SCHEMA_VERSION = "seebx-lifeswitch-database-consumer-audit-v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")

PROVEN_DISPOSITIONS = {
    "application_direct": ("retained_active", "include", "runtime_consumer_verified"),
    "database_internal_reachable": (
        "retained_dependency",
        "include",
        "runtime_dependency_verified",
    ),
    "extension_owned": ("retained_extension", "include", "extension_owned"),
}

EXPLICIT_DISPOSITIONS = {
    "catalog_dev.exercise_muscle": (
        "unproven",
        "retained_canonical_product_data",
        "include",
        "approved_normalized_muscle_relationship_authority",
    ),
    "catalog_dev.exercise_muscle_rule": (
        "unproven",
        "archive_candidate",
        "exclude",
        "empty_rule_surface_unconsumed",
    ),
    "catalog_dev.muscle": (
        "unproven",
        "retained_canonical_product_data",
        "include",
        "approved_normalized_muscle_identity_authority",
    ),
    "catalog_dev.muscle_alias": (
        "unproven",
        "retained_canonical_product_data",
        "include",
        "approved_normalized_muscle_alias_authority",
    ),
    "catalog_dev.food_alias": (
        "migration_only",
        "archive_candidate",
        "exclude",
        "dormant_internal_food_search_model",
    ),
    "catalog_dev.food_nutrient": (
        "migration_only",
        "archive_candidate",
        "exclude",
        "dormant_internal_food_search_model",
    ),
    "catalog_dev.food_portion": (
        "migration_only",
        "archive_candidate",
        "exclude",
        "dormant_internal_food_search_model",
    ),
    "catalog_dev.nutrient": (
        "migration_only",
        "archive_candidate",
        "exclude",
        "dormant_internal_food_search_model",
    ),
    "catalog_dev.search_foods(q text, max_results integer, p_locale text)": (
        "migration_only",
        "archive_candidate",
        "exclude",
        "retired_internal_food_search_function",
    ),
    "lifeswitch_snapshot.personalization_source_row": (
        "unproven",
        "recovery_only",
        "exclude",
        "migration_recovery_evidence",
    ),
    "lifeswitch_snapshot.analysis_source_row": (
        "operational_reference_only",
        "recovery_only",
        "exclude",
        "restore_verifier_denial_evidence",
    ),
}


class DispositionError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_audit(path: Path, expected_sha256: str) -> tuple[dict[str, Any], str]:
    if SHA256.fullmatch(expected_sha256) is None:
        raise DispositionError("audit_sha256_invalid")
    if path.is_symlink() or not path.is_file():
        raise DispositionError("audit_not_regular")
    data = path.read_bytes()
    actual = sha256_bytes(data)
    if actual != expected_sha256:
        raise DispositionError("audit_sha256_mismatch")
    try:
        audit = json.loads(data)
    except json.JSONDecodeError as error:
        raise DispositionError("audit_json_invalid") from error
    if not isinstance(audit, dict):
        raise DispositionError("audit_shape_invalid")
    return audit, actual


def build_manifest(audit: dict[str, Any], audit_sha256: str) -> dict[str, Any]:
    if audit.get("schema_version") != AUDIT_SCHEMA_VERSION or audit.get("status") != "pass":
        raise DispositionError("audit_contract_invalid")
    scope = audit.get("scope")
    if not isinstance(scope, dict):
        raise DispositionError("audit_scope_invalid")
    if scope.get("deletion_authority") is not False:
        raise DispositionError("audit_deletion_authority_invalid")
    if scope.get("operational_references_are_retention_seeds") is not False:
        raise DispositionError("audit_operational_seed_contract_invalid")
    objects = audit.get("objects")
    if not isinstance(objects, list) or len(objects) != audit.get("object_count"):
        raise DispositionError("audit_objects_invalid")

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in objects:
        if not isinstance(raw, dict):
            raise DispositionError("audit_object_invalid")
        identity = str(raw.get("identity") or "")
        classification = str(raw.get("classification") or "")
        if not identity or identity in seen:
            raise DispositionError("audit_object_identity_invalid")
        seen.add(identity)
        if classification in PROVEN_DISPOSITIONS:
            disposition, baseline_action, reason = PROVEN_DISPOSITIONS[classification]
        else:
            decision = EXPLICIT_DISPOSITIONS.get(identity)
            if decision is None or decision[0] != classification:
                raise DispositionError("unresolved_object_without_exact_disposition")
            _, disposition, baseline_action, reason = decision
        entries.append(
            {
                "baseline_action": baseline_action,
                "classification": classification,
                "disposition": disposition,
                "identity": identity,
                "object_type": str(raw.get("object_type") or ""),
                "reason_code": reason,
            }
        )

    expected_explicit = set(EXPLICIT_DISPOSITIONS)
    if not expected_explicit.issubset(seen):
        raise DispositionError("expected_explicit_object_missing")
    entries.sort(key=lambda item: item["identity"])
    counts: dict[str, int] = {}
    for entry in entries:
        key = entry["disposition"]
        counts[key] = counts.get(key, 0) + 1
    holds = counts.get("review_hold", 0)
    return {
        "baseline_generation_allowed": holds == 0,
        "deletion_authority": False,
        "disposition_counts": dict(sorted(counts.items())),
        "object_count": len(entries),
        "objects": entries,
        "production_change_authority": False,
        "schema_version": SCHEMA_VERSION,
        "source_audit": {
            "candidate_commit": str(audit.get("candidate_commit") or ""),
            "object_catalog_sha256": str(audit.get("object_catalog_sha256") or ""),
            "report_sha256": audit_sha256,
            "source_manifest_sha256": str(audit.get("source_manifest_sha256") or ""),
        },
        "status": "candidate_baseline_ready" if holds == 0 else "candidate_review_gate",
    }


def write_exclusive(path: Path, manifest: dict[str, Any]) -> None:
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise DispositionError("output_parent_invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise DispositionError("output_create_failed") from error
    try:
        data = canonical_bytes(manifest) + b"\n"
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--audit-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        audit, audit_sha256 = read_audit(arguments.audit, arguments.audit_sha256)
        manifest = build_manifest(audit, audit_sha256)
        write_exclusive(arguments.output, manifest)
    except DispositionError as error:
        print(json.dumps({"error": str(error), "status": "failed"}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "baseline_generation_allowed": manifest["baseline_generation_allowed"],
                "disposition_counts": manifest["disposition_counts"],
                "manifest_sha256": sha256_bytes(canonical_bytes(manifest) + b"\n"),
                "object_count": manifest["object_count"],
                "status": "pass",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
