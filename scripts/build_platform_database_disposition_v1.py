#!/usr/bin/env python3
from __future__ import annotations

"""Build a fail-closed clean-platform disposition from an exact DB audit."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "seebx-platform-database-disposition-v1"
AUDIT_SCHEMA_VERSION = "seebx-platform-database-consumer-audit-v1"
GOVERNANCE_SCHEMA_VERSION = "seebx-database-governance-manifest-v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")

ACTIVE_OPERATIONAL_OBJECTS = {
    "ai_operations.claim_monitor_alert_delivery_v1(p_worker_id uuid)",
    "ai_operations.complete_monitor_alert_delivery_v1(p_delivery_id uuid, p_worker_id uuid, p_outcome text, p_provider_message_id text, p_error_code text)",
    "ai_operations.enforce_telemetry_retention_v1()",
    "ai_operations.record_monitor_observation_v1(p_monitor_name text, p_status text, p_severity text, p_is_drill boolean, p_reason_codes text[], p_window_hours integer, p_request_count bigint, p_completed_count bigint, p_fail_closed_count bigint, p_relevance_fail_closed_count bigint, p_dependency_failure_count bigint, p_fail_closed_rate numeric, p_observed_at timestamp with time zone)",
}
FORMS_OBJECTS = {
    "public.vb_form_entries",
    "public.vb_form_templates",
    "public.vb_form_versions",
}
LEGACY_PUBLIC_OBJECTS = {
    "public.chat_messages",
    "public.chat_sessions",
    "public.feedback_signals",
    "public.feedback_signals_id_seq",
    "public.vantage_answer_trace",
    "public.vs_profiles",
}
USAGE_REGISTRY_OBJECTS = {
    "lifeswitch_usage.ai_actor_registry_v1",
    "lifeswitch_usage.protect_ai_actor_registry_v1()",
}
REQUIRED_EXPLICIT_OBJECTS = (
    ACTIVE_OPERATIONAL_OBJECTS
    | FORMS_OBJECTS
    | LEGACY_PUBLIC_OBJECTS
    | USAGE_REGISTRY_OBJECTS
)

EXTENSION_DECISIONS = {
    "citext": (
        "exclude_platform_extension",
        "exclude",
        "no_nonextension_platform_object_dependency",
    ),
    "pg_trgm": (
        "exclude_platform_extension",
        "exclude",
        "only_duplicate_catalog_indexes_depend",
    ),
    "pgcrypto": (
        "retained_extension",
        "include",
        "retained_ai_operations_digest_dependency",
    ),
    "plpgsql": ("retained_extension", "include", "retained_procedural_language"),
    "unaccent": (
        "exclude_platform_extension",
        "exclude",
        "only_retired_catalog_and_memory_functions_depend",
    ),
}

ROLE_TARGETS = {
    "brains_app": (
        "replace_platform_role",
        "replace",
        "seebx_platform_app_v1",
        "service_login_requires_explicit_target_identity",
    ),
    "ai_operations_store_v1": (
        "retained_no_login_owner",
        "include",
        "ai_operations_store_v1",
        "canonical_operations_owner",
    ),
    "lifeswitch_usage_admin_v1": (
        "replace_platform_role",
        "replace",
        "seebx_usage_admin_v1",
        "remove_product_name_from_platform_role",
    ),
    "lifeswitch_usage_writer_v1": (
        "replace_platform_role",
        "replace",
        "seebx_usage_writer_v1",
        "remove_product_name_from_platform_role",
    ),
    "sage": (
        "source_admin_only",
        "exclude",
        "",
        "superuser_login_not_service_or_clean_baseline_role",
    ),
}
REQUIRED_ROLES = set(ROLE_TARGETS)

PUBLIC_TARGET_SCHEMAS = {
    "public.active_thread_selection": "conversation",
    "public.chat_attachments": "conversation",
    "public.chat_log": "conversation",
    "public.guard_canonical_owner()": "conversation",
    "public.guard_chat_log_immutable()": "conversation",
    "public.telemetry_event": "telemetry",
    "public.threads": "conversation",
    "public.voice_session_lease": "voice",
}
SOURCE_SCHEMA_TARGETS = {
    "ai_operations": ("ai_operations", "ai_operations_store_v1"),
    "chat_history_private": ("conversation_private", "seebx_platform_owner_v1"),
    "chat_integrity": ("conversation_integrity", "seebx_platform_owner_v1"),
    "lifeswitch_usage": ("usage", "seebx_platform_owner_v1"),
    "trusted_web": ("trusted_web", "seebx_trusted_web_owner_v1"),
    "user_settings": ("user_settings", "seebx_platform_owner_v1"),
}
TARGET_SCHEMA_CONTRACTS = (
    {
        "name": "ai_operations",
        "owner": "ai_operations_store_v1",
        "source": "ai_operations",
    },
    {
        "name": "conversation",
        "owner": "seebx_platform_owner_v1",
        "source": "public",
    },
    {
        "name": "conversation_integrity",
        "owner": "seebx_platform_owner_v1",
        "source": "chat_integrity",
    },
    {
        "name": "conversation_private",
        "owner": "seebx_platform_owner_v1",
        "source": "chat_history_private",
    },
    {
        "name": "telemetry",
        "owner": "seebx_platform_owner_v1",
        "source": "public",
    },
    {
        "name": "trusted_web",
        "owner": "seebx_trusted_web_owner_v1",
        "source": "trusted_web",
    },
    {
        "name": "usage",
        "owner": "seebx_platform_owner_v1",
        "source": "lifeswitch_usage",
    },
    {
        "name": "user_settings",
        "owner": "seebx_platform_owner_v1",
        "source": "user_settings",
    },
    {
        "name": "voice",
        "owner": "seebx_platform_owner_v1",
        "source": "public",
    },
)
TARGET_ROLE_CONTRACTS = (
    {
        "can_login": False,
        "name": "ai_operations_store_v1",
        "purpose": "ai_operations_owner",
    },
    {
        "can_login": True,
        "name": "seebx_platform_app_v1",
        "purpose": "runtime_application_login",
    },
    {
        "can_login": False,
        "name": "seebx_platform_owner_v1",
        "purpose": "general_platform_object_owner",
    },
    {
        "can_login": False,
        "name": "seebx_trusted_web_owner_v1",
        "purpose": "trusted_web_object_owner",
    },
    {
        "can_login": False,
        "name": "seebx_usage_admin_v1",
        "purpose": "separate_usage_administration",
    },
    {
        "can_login": False,
        "name": "seebx_usage_writer_v1",
        "purpose": "runtime_owner_scoped_usage_writer",
    },
)
TARGET_MEMBERSHIPS = {
    ("seebx_platform_app_v1", "seebx_usage_writer_v1"),
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


def _verified_governance(audit: dict[str, Any]) -> dict[str, Any]:
    governance = audit.get("governance_manifest")
    if not isinstance(governance, dict):
        raise DispositionError("governance_manifest_missing")
    if governance.get("schema_version") != GOVERNANCE_SCHEMA_VERSION:
        raise DispositionError("governance_schema_invalid")
    expected = str(governance.get("manifest_sha256") or "")
    if SHA256.fullmatch(expected) is None:
        raise DispositionError("governance_sha256_invalid")
    body = {key: value for key, value in governance.items() if key != "manifest_sha256"}
    if sha256_bytes(canonical_bytes(body)) != expected:
        raise DispositionError("governance_sha256_mismatch")
    sections = governance.get("sections")
    section_hashes = governance.get("section_hashes")
    if not isinstance(sections, dict) or not isinstance(section_hashes, dict):
        raise DispositionError("governance_sections_invalid")
    for name, rows in sections.items():
        if not isinstance(rows, list):
            raise DispositionError("governance_section_invalid")
        if section_hashes.get(name) != sha256_bytes(canonical_bytes(rows)):
            raise DispositionError("governance_section_sha256_mismatch")
    return governance


def _object_decision(raw: dict[str, Any]) -> tuple[str, str, str]:
    identity = str(raw.get("identity") or "")
    classification = str(raw.get("classification") or "")
    extension = str(raw.get("extension") or "")
    schema = identity.split(".", 1)[0]
    if classification == "extension_owned":
        decision = EXTENSION_DECISIONS.get(extension)
        if decision is None:
            raise DispositionError("unknown_extension_object")
        return decision
    if schema == "memory":
        return "archive_legacy_memory", "exclude", "retired_custom_memory_schema"
    if schema == "memory_ingest_private":
        return "archive_legacy_ingest", "exclude", "retired_custom_memory_ingest_schema"
    if schema == "catalog_dev":
        return "consolidate_lifeswitch_catalog", "exclude", "duplicate_platform_catalog"
    if identity in FORMS_OBJECTS:
        return "migrate_lifeswitch_forms", "exclude", "owner_bound_forms_reconciliation"
    if identity in LEGACY_PUBLIC_OBJECTS:
        return "archive_legacy_public", "exclude", "unmounted_legacy_public_surface"
    if identity in USAGE_REGISTRY_OBJECTS:
        return "retire_disconnected_usage_registry", "exclude", "disconnected_reporting_registry"
    if identity in ACTIVE_OPERATIONAL_OBJECTS:
        return "retained_operations", "include", "active_timer_consumer_verified"
    if classification == "application_direct":
        return "retained_active", "include", "runtime_consumer_verified"
    if classification == "database_internal_reachable":
        return "retained_dependency", "include", "runtime_dependency_verified"
    raise DispositionError("unresolved_object_without_exact_disposition")


def _role_decision(role: dict[str, Any]) -> tuple[str, str, str, str]:
    name = str(role.get("name") or "")
    explicit = ROLE_TARGETS.get(name)
    if explicit is not None:
        return explicit
    if name.startswith("memory_") or name.startswith("governed_memory_"):
        return "retire_legacy_memory_role", "exclude", "", "legacy_memory_authority"
    if name.startswith("lifeswitch_chat_") or name == "lifeswitch_training_observation_owner":
        return "remove_cross_database_role", "exclude", "", "belongs_only_in_isolated_lifeswitch_database"
    raise DispositionError("unknown_role_without_exact_disposition")


def _object_target(
    identity: str,
    classification: str,
    baseline_action: str,
) -> tuple[str | None, str | None]:
    if baseline_action != "include":
        return None, None
    if classification == "extension_owned":
        return identity, "extension"
    source_schema, separator, remainder = identity.partition(".")
    if not separator or not remainder:
        raise DispositionError("retained_object_identity_invalid")
    if source_schema == "public":
        target_schema = PUBLIC_TARGET_SCHEMAS.get(identity)
        if target_schema is None:
            raise DispositionError("public_retained_object_target_missing")
        return f"{target_schema}.{remainder}", "seebx_platform_owner_v1"
    target = SOURCE_SCHEMA_TARGETS.get(source_schema)
    if target is None:
        raise DispositionError("retained_schema_target_missing")
    target_schema, owner = target
    return f"{target_schema}.{remainder}", owner


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
    if scope.get("governance_manifest_included") is not True:
        raise DispositionError("audit_governance_scope_invalid")
    governance = _verified_governance(audit)
    objects = audit.get("objects")
    if not isinstance(objects, list) or len(objects) != audit.get("object_count"):
        raise DispositionError("audit_objects_invalid")

    counts_by_relation = {
        f"{row['schema']}.{row['relation']}": int(row["row_count"])
        for row in governance["sections"]["table_counts"]
    }
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_targets: set[str] = set()
    legacy_ingest_dependencies: list[str] = []
    for raw in objects:
        if not isinstance(raw, dict):
            raise DispositionError("audit_object_invalid")
        identity = str(raw.get("identity") or "")
        classification = str(raw.get("classification") or "")
        if not identity or identity in seen:
            raise DispositionError("audit_object_identity_invalid")
        seen.add(identity)
        disposition, baseline_action, reason = _object_decision(raw)
        target_identity, target_owner = _object_target(
            identity,
            classification,
            baseline_action,
        )
        if target_identity is not None:
            if target_identity in seen_targets:
                raise DispositionError("target_object_identity_duplicate")
            seen_targets.add(target_identity)
        if identity.startswith("memory_ingest_private.") and classification == "database_internal_reachable":
            legacy_ingest_dependencies.append(identity)
        entry = {
            "baseline_action": baseline_action,
            "classification": classification,
            "disposition": disposition,
            "extension": str(raw.get("extension") or "") or None,
            "identity": identity,
            "object_type": str(raw.get("object_type") or ""),
            "reason_code": reason,
            "target_identity": target_identity,
            "target_owner": target_owner,
        }
        if identity in counts_by_relation:
            entry["exact_row_count"] = counts_by_relation[identity]
        entries.append(entry)
    if not REQUIRED_EXPLICIT_OBJECTS.issubset(seen):
        raise DispositionError("required_explicit_object_missing")
    if set(counts_by_relation) - seen:
        raise DispositionError("table_count_object_missing")

    roles = governance["sections"].get("roles")
    if not isinstance(roles, list):
        raise DispositionError("governance_roles_invalid")
    role_entries: list[dict[str, Any]] = []
    seen_roles: set[str] = set()
    role_targets: dict[str, str] = {}
    for role in roles:
        if not isinstance(role, dict):
            raise DispositionError("governance_role_invalid")
        name = str(role.get("name") or "")
        if not name or name in seen_roles:
            raise DispositionError("governance_role_identity_invalid")
        seen_roles.add(name)
        disposition, action, target, reason = _role_decision(role)
        role_targets[name] = target
        role_entries.append({
            "action": action,
            "can_login": bool(role.get("can_login")),
            "disposition": disposition,
            "name": name,
            "reason_code": reason,
            "source_bypass_rls": bool(role.get("bypass_rls")),
            "source_superuser": bool(role.get("superuser")),
            "target_role": target or None,
        })
    if not REQUIRED_ROLES.issubset(seen_roles):
        raise DispositionError("required_role_missing")

    memberships = governance["sections"].get("memberships")
    if not isinstance(memberships, list):
        raise DispositionError("governance_memberships_invalid")
    membership_entries: list[dict[str, Any]] = []
    rebuilt_memberships: set[tuple[str, str]] = set()
    for membership in memberships:
        if not isinstance(membership, dict):
            raise DispositionError("governance_membership_invalid")
        member = str(membership.get("member") or "")
        granted = str(membership.get("granted") or "")
        if member not in seen_roles or granted not in seen_roles:
            raise DispositionError("governance_membership_role_missing")
        target_member = role_targets.get(member) or ""
        target_granted = role_targets.get(granted) or ""
        target_pair = (target_member, target_granted)
        rebuild = target_pair in TARGET_MEMBERSHIPS
        if rebuild:
            rebuilt_memberships.add(target_pair)
        membership_entries.append({
            "action": "rebuild" if rebuild else "exclude",
            "admin_option": bool(membership.get("admin_option")),
            "granted": granted,
            "member": member,
            "target_granted": target_granted or None,
            "target_member": target_member or None,
        })
    if rebuilt_memberships != TARGET_MEMBERSHIPS:
        raise DispositionError("required_target_membership_missing")

    extensions = governance["sections"].get("extensions")
    if not isinstance(extensions, list):
        raise DispositionError("governance_extensions_invalid")
    observed_extensions = {str(item.get("name") or "") for item in extensions}
    if observed_extensions != set(EXTENSION_DECISIONS):
        raise DispositionError("extension_set_changed")
    extension_entries = [
        {
            "action": EXTENSION_DECISIONS[str(item["name"])][1],
            "disposition": EXTENSION_DECISIONS[str(item["name"])][0],
            "name": str(item["name"]),
            "reason_code": EXTENSION_DECISIONS[str(item["name"])][2],
            "version": str(item.get("version") or ""),
        }
        for item in extensions
    ]

    entries.sort(key=lambda item: item["identity"])
    role_entries.sort(key=lambda item: item["name"])
    membership_entries.sort(key=lambda item: (item["member"], item["granted"]))
    extension_entries.sort(key=lambda item: item["name"])
    disposition_counts: dict[str, int] = {}
    table_rows_by_disposition: dict[str, dict[str, int]] = {}
    for entry in entries:
        disposition = entry["disposition"]
        disposition_counts[disposition] = disposition_counts.get(disposition, 0) + 1
        if "exact_row_count" in entry:
            summary = table_rows_by_disposition.setdefault(disposition, {"tables": 0, "rows": 0})
            summary["tables"] += 1
            summary["rows"] += int(entry["exact_row_count"])
    held_extensions = sorted(
        item["name"] for item in extension_entries if item["action"] == "hold"
    )
    blockers: list[dict[str, Any]] = []
    if legacy_ingest_dependencies:
        blockers.append({
            "code": "legacy_ingest_dependencies_present",
            "count": len(legacy_ingest_dependencies),
            "object_set_sha256": sha256_bytes(canonical_bytes(sorted(legacy_ingest_dependencies))),
        })
    if held_extensions:
        blockers.append({
            "code": "extension_dependencies_unresolved",
            "extensions": held_extensions,
        })
    return {
        "baseline_blockers": blockers,
        "baseline_generation_allowed": not blockers,
        "cutover_requirements": [
            "chat_clear_zep_outbox_migration_verified",
            "forms_valid_quarantine_reconciliation_verified",
            "legacy_memory_backup_and_disposable_restore_verified",
            "retained_platform_clean_install_and_owner_rls_verified",
        ],
        "deletion_authority": False,
        "disposition_counts": dict(sorted(disposition_counts.items())),
        "extensions": extension_entries,
        "memberships": membership_entries,
        "object_count": len(entries),
        "objects": entries,
        "production_change_authority": False,
        "roles": role_entries,
        "schema_version": SCHEMA_VERSION,
        "source_audit": {
            "candidate_commit": str(audit.get("candidate_commit") or ""),
            "governance_manifest_sha256": str(governance.get("manifest_sha256") or ""),
            "object_catalog_sha256": str(audit.get("object_catalog_sha256") or ""),
            "report_sha256": audit_sha256,
        },
        "status": "candidate_baseline_ready" if not blockers else "candidate_review_gate",
        "table_rows_by_disposition": dict(sorted(table_rows_by_disposition.items())),
        "target_memberships": [
            {"granted": granted, "member": member}
            for member, granted in sorted(TARGET_MEMBERSHIPS)
        ],
        "target_roles": list(TARGET_ROLE_CONTRACTS),
        "target_schemas": list(TARGET_SCHEMA_CONTRACTS),
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
    print(json.dumps({
        "baseline_blockers": manifest["baseline_blockers"],
        "baseline_generation_allowed": manifest["baseline_generation_allowed"],
        "disposition_counts": manifest["disposition_counts"],
        "manifest_sha256": sha256_bytes(canonical_bytes(manifest) + b"\n"),
        "object_count": manifest["object_count"],
        "status": "pass",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
