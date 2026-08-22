#!/usr/bin/env python3
from __future__ import annotations

"""Compile an exact clean SeeBx platform schema from disposable evidence."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

try:
    from build_platform_database_disposition_v1 import (
        EXTENSION_DECISIONS,
        PUBLIC_TARGET_SCHEMAS,
        ROLE_TARGETS,
        SOURCE_SCHEMA_TARGETS,
        TARGET_MEMBERSHIPS,
        TARGET_ROLE_CONTRACTS,
        TARGET_SCHEMA_CONTRACTS,
    )
except ModuleNotFoundError:
    from scripts.build_platform_database_disposition_v1 import (
        EXTENSION_DECISIONS,
        PUBLIC_TARGET_SCHEMAS,
        ROLE_TARGETS,
        SOURCE_SCHEMA_TARGETS,
        TARGET_MEMBERSHIPS,
        TARGET_ROLE_CONTRACTS,
        TARGET_SCHEMA_CONTRACTS,
    )


SCHEMA_VERSION = "seebx-platform-clean-baseline-receipt-v1"
DISPOSITION_VERSION = "seebx-platform-database-disposition-v1"
PG_RESTORE = "/usr/bin/pg_restore"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

ROLE_NAME_MAP = {
    "brains_app": "seebx_platform_app_v1",
    "lifeswitch_usage_admin_v1": "seebx_usage_admin_v1",
    "lifeswitch_usage_writer_v1": "seebx_usage_writer_v1",
}
RETAINED_EXTENSIONS = {"pgcrypto": "1.3", "plpgsql": "1.0"}
FORBIDDEN_SCHEMAS = {
    "catalog_dev",
    "memory",
    "memory_ingest_private",
}
FORBIDDEN_PUBLIC_OBJECTS = {
    "chat_messages",
    "chat_sessions",
    "feedback_signals",
    "feedback_signals_id_seq",
    "vantage_answer_trace",
    "vb_form_entries",
    "vb_form_templates",
    "vb_form_versions",
    "vs_profiles",
}
FORBIDDEN_ROLE_PREFIXES = (
    "governed_memory_",
    "lifeswitch_chat_",
    "memory_",
)


class BaselineContractError(RuntimeError):
    pass


class BaselineExecutionError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_hash_bound(path: Path, expected_sha256: str, *, label: str) -> bytes:
    if SHA256.fullmatch(expected_sha256) is None:
        raise BaselineContractError(f"{label}_sha256_invalid")
    try:
        item = path.lstat()
    except OSError as error:
        raise BaselineContractError(f"{label}_unavailable") from error
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
        raise BaselineContractError(f"{label}_not_regular")
    data = path.read_bytes()
    if sha256_bytes(data) != expected_sha256:
        raise BaselineContractError(f"{label}_sha256_mismatch")
    return data


def read_disposition(path: Path, expected_sha256: str) -> dict[str, Any]:
    data = _read_hash_bound(path, expected_sha256, label="disposition")
    try:
        value = json.loads(data)
    except json.JSONDecodeError as error:
        raise BaselineContractError("disposition_json_invalid") from error
    if not isinstance(value, dict):
        raise BaselineContractError("disposition_shape_invalid")
    return value


def _target_for_object(item: dict[str, Any]) -> tuple[str | None, str | None]:
    if item.get("baseline_action") != "include":
        return None, None
    if item.get("classification") == "extension_owned":
        return None, None
    identity = str(item.get("identity") or "")
    source_schema, separator, remainder = identity.partition(".")
    if not separator or not remainder:
        raise BaselineContractError("retained_identity_invalid")
    if source_schema == "public":
        target_schema = PUBLIC_TARGET_SCHEMAS.get(identity)
        if target_schema is None:
            raise BaselineContractError("retained_public_target_missing")
        return f"{target_schema}.{remainder}", "seebx_platform_owner_v1"
    target = SOURCE_SCHEMA_TARGETS.get(source_schema)
    if target is None:
        raise BaselineContractError("retained_schema_target_missing")
    target_schema, owner = target
    return f"{target_schema}.{remainder}", owner


def _validate_source_roles(manifest: dict[str, Any]) -> None:
    raw_roles = manifest.get("roles")
    if not isinstance(raw_roles, list):
        raise BaselineContractError("disposition_roles_invalid")
    observed: dict[str, dict[str, Any]] = {}
    for raw in raw_roles:
        if not isinstance(raw, dict):
            raise BaselineContractError("disposition_role_invalid")
        name = str(raw.get("name") or "")
        if not name or name in observed:
            raise BaselineContractError("disposition_role_identity_invalid")
        observed[name] = raw
    for source, expected in ROLE_TARGETS.items():
        if source not in observed:
            raise BaselineContractError("required_source_role_missing")
        disposition, action, target, reason = expected
        raw = observed[source]
        if (
            raw.get("disposition") != disposition
            or raw.get("action") != action
            or raw.get("target_role") != (target or None)
            or raw.get("reason_code") != reason
        ):
            raise BaselineContractError("source_role_disposition_changed")
    target_names = {str(item["name"]) for item in TARGET_ROLE_CONTRACTS}
    if target_names != {
        "ai_operations_store_v1",
        "seebx_platform_app_v1",
        "seebx_platform_owner_v1",
        "seebx_trusted_web_owner_v1",
        "seebx_usage_admin_v1",
        "seebx_usage_writer_v1",
    }:
        raise BaselineContractError("target_role_contract_changed")


def _validate_source_extensions(manifest: dict[str, Any]) -> None:
    raw_extensions = manifest.get("extensions")
    if not isinstance(raw_extensions, list):
        raise BaselineContractError("disposition_extensions_invalid")
    observed = {str(item.get("name") or ""): item for item in raw_extensions if isinstance(item, dict)}
    if set(observed) != set(EXTENSION_DECISIONS):
        raise BaselineContractError("source_extension_set_changed")
    for name, expected in EXTENSION_DECISIONS.items():
        disposition, action, reason = expected
        item = observed[name]
        if (
            item.get("disposition") != disposition
            or item.get("action") != action
            or item.get("reason_code") != reason
        ):
            raise BaselineContractError("source_extension_disposition_changed")
    retained = {name: str(observed[name].get("version") or "") for name in observed if observed[name]["action"] == "include"}
    if retained != RETAINED_EXTENSIONS:
        raise BaselineContractError("retained_extension_contract_changed")


def _validate_writer_membership(manifest: dict[str, Any]) -> None:
    memberships = manifest.get("memberships")
    if not isinstance(memberships, list):
        raise BaselineContractError("disposition_memberships_invalid")
    source_pairs = {
        (str(item.get("member") or ""), str(item.get("granted") or ""))
        for item in memberships
        if isinstance(item, dict)
    }
    if ("brains_app", "lifeswitch_usage_writer_v1") not in source_pairs:
        raise BaselineContractError("source_usage_writer_membership_missing")
    if TARGET_MEMBERSHIPS != {("seebx_platform_app_v1", "seebx_usage_writer_v1")}:
        raise BaselineContractError("target_membership_contract_changed")


def validate_disposition(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("schema_version") != DISPOSITION_VERSION:
        raise BaselineContractError("disposition_version_invalid")
    if manifest.get("status") != "candidate_baseline_ready":
        raise BaselineContractError("disposition_status_invalid")
    if manifest.get("baseline_generation_allowed") is not True:
        raise BaselineContractError("baseline_generation_not_allowed")
    if manifest.get("baseline_blockers") != []:
        raise BaselineContractError("baseline_blockers_present")
    if manifest.get("deletion_authority") is not False:
        raise BaselineContractError("deletion_authority_invalid")
    if manifest.get("production_change_authority") is not False:
        raise BaselineContractError("production_change_authority_invalid")
    objects = manifest.get("objects")
    if not isinstance(objects, list) or len(objects) != manifest.get("object_count"):
        raise BaselineContractError("disposition_objects_invalid")
    _validate_source_roles(manifest)
    _validate_source_extensions(manifest)
    _validate_writer_membership(manifest)

    source_relations: list[str] = []
    source_functions: list[str] = []
    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    seen_targets: set[str] = set()
    included_count = 0
    extension_owned_count = 0
    for raw in objects:
        if not isinstance(raw, dict):
            raise BaselineContractError("disposition_object_invalid")
        identity = str(raw.get("identity") or "")
        if not identity or identity in seen:
            raise BaselineContractError("disposition_identity_invalid")
        seen.add(identity)
        action = raw.get("baseline_action")
        if action not in {"include", "exclude"}:
            raise BaselineContractError("disposition_action_invalid")
        if action != "include":
            if raw.get("target_identity") is not None or raw.get("target_owner") is not None:
                raise BaselineContractError("excluded_object_has_target")
            continue
        included_count += 1
        if raw.get("classification") == "extension_owned":
            extension_owned_count += 1
            continue
        object_type = str(raw.get("object_type") or "")
        if object_type not in {"function", "relation"}:
            raise BaselineContractError("retained_object_type_invalid")
        target_identity, target_owner = _target_for_object(raw)
        assert target_identity is not None and target_owner is not None
        if target_identity in seen_targets:
            raise BaselineContractError("target_identity_duplicate")
        seen_targets.add(target_identity)
        if raw.get("target_identity") not in {None, target_identity}:
            raise BaselineContractError("recorded_target_identity_changed")
        if raw.get("target_owner") not in {None, target_owner}:
            raise BaselineContractError("recorded_target_owner_changed")
        if object_type == "relation":
            source_relations.append(identity)
        else:
            source_functions.append(identity)
        targets.append({
            "object_type": object_type,
            "source_identity": identity,
            "target_identity": target_identity,
            "target_owner": target_owner,
        })
    if included_count != 74 or extension_owned_count != 36:
        raise BaselineContractError("retained_object_count_changed")
    if len(source_relations) != 21 or len(source_functions) != 17:
        raise BaselineContractError("retained_product_object_count_changed")
    function_bases = [identity.split("(", 1)[0] for identity in source_functions]
    if len(function_bases) != len(set(function_bases)):
        raise BaselineContractError("overloaded_product_function_unsupported")
    return {
        "extensions": dict(sorted(RETAINED_EXTENSIONS.items())),
        "source_functions": sorted(source_functions),
        "source_relations": sorted(source_relations),
        "target_memberships": [
            {"granted": granted, "member": member}
            for member, granted in sorted(TARGET_MEMBERSHIPS)
        ],
        "target_roles": list(TARGET_ROLE_CONTRACTS),
        "target_schemas": list(TARGET_SCHEMA_CONTRACTS),
        "targets": sorted(targets, key=lambda item: item["target_identity"]),
    }


def _toc_payload(line: str) -> str | None:
    match = re.match(r"^\d+;\s+\d+\s+\d+\s+(.*)$", line.strip())
    return match.group(1) if match else None


def _source_bases(plan: dict[str, Any], key: str) -> set[tuple[str, str]]:
    values: set[tuple[str, str]] = set()
    for identity in plan[key]:
        base = identity.split("(", 1)[0]
        schema, name = base.split(".", 1)
        values.add((schema, name))
    return values


def extract_index_relation_map(
    raw_toc: str,
    post_data_sql: str,
) -> dict[tuple[str, str], tuple[str, str]]:
    expected: set[tuple[str, str]] = set()
    for raw_line in raw_toc.splitlines():
        payload = _toc_payload(raw_line.strip())
        if payload is None:
            continue
        match = re.match(r"^INDEX (\S+) (\S+)(?:\s+.*)?$", payload)
        if match:
            expected.add((match.group(1), match.group(2)))
    observed: dict[tuple[str, str], tuple[str, str]] = {}
    for match in re.finditer(
        r"(?im)^CREATE\s+(?:UNIQUE\s+)?INDEX\s+"
        r"([a-z_][a-z0-9_]*)\s+ON\s+(?:ONLY\s+)?"
        r"([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b",
        post_data_sql,
    ):
        key = (match.group(2), match.group(1))
        relation = (match.group(2), match.group(3))
        if key in observed and observed[key] != relation:
            raise BaselineContractError("archive_index_identity_ambiguous")
        observed[key] = relation
    if set(observed) != expected:
        raise BaselineContractError("archive_index_relation_map_incomplete")
    return observed


def filter_restore_list(
    raw: str,
    plan: dict[str, Any],
    index_relations: Mapping[tuple[str, str], tuple[str, str]] | None = None,
) -> tuple[str, dict[str, int]]:
    relations = _source_bases(plan, "source_relations")
    functions = _source_bases(plan, "source_functions")
    seen_relations: set[tuple[str, str]] = set()
    seen_functions: set[tuple[str, str]] = set()
    entries: list[str] = []
    ignored = 0
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        payload = _toc_payload(line)
        if payload is None:
            raise BaselineContractError("archive_toc_line_invalid")
        upper = f" {payload.upper()} "
        if " TABLE DATA " in upper or " SEQUENCE SET " in upper or payload.startswith("BLOB "):
            raise BaselineContractError("archive_contains_row_data")
        if payload.startswith("ACL ") or " ACL " in upper:
            raise BaselineContractError("archive_contains_privileges")

        keep = False
        match = re.match(r"^FUNCTION (\S+) ([^\s(]+)\(", payload)
        if match:
            key = (match.group(1), match.group(2))
            keep = key in functions
            if keep:
                seen_functions.add(key)
        if not keep:
            match = re.match(r"^COMMENT (\S+) FUNCTION ([^\s(]+)\(", payload)
            if match:
                keep = (match.group(1), match.group(2)) in functions

        if not keep:
            match = re.match(r"^(?:TABLE|VIEW|MATERIALIZED VIEW|FOREIGN TABLE) (\S+) (\S+)", payload)
            if match:
                key = (match.group(1), match.group(2))
                keep = key in relations
                if keep:
                    seen_relations.add(key)
        if not keep:
            match = re.match(
                r"^COMMENT (\S+) (?:TABLE|VIEW|MATERIALIZED VIEW|FOREIGN TABLE|COLUMN) (\S+)",
                payload,
            )
            if match:
                keep = (match.group(1), match.group(2).split(".", 1)[0]) in relations
        if not keep:
            match = re.match(r"^INDEX (\S+) (\S+)(?:\s+.*)?$", payload)
            if match:
                if index_relations is None:
                    raise BaselineContractError("archive_index_relation_map_missing")
                relation = index_relations.get((match.group(1), match.group(2)))
                if relation is None:
                    raise BaselineContractError("archive_index_relation_mapping_missing")
                keep = relation in relations
        if not keep:
            match = re.match(
                r"^(?:CONSTRAINT|FK CONSTRAINT|TRIGGER|POLICY|ROW SECURITY|DEFAULT|RULE) (\S+) (\S+)",
                payload,
            )
            if match:
                keep = (match.group(1), match.group(2)) in relations
        if keep:
            entries.append(line)
        else:
            ignored += 1
    if seen_relations != relations:
        raise BaselineContractError("retained_relation_missing_from_archive")
    if seen_functions != functions:
        raise BaselineContractError("retained_function_missing_from_archive")
    return "\n".join(entries) + "\n", {
        "ignored_toc_entries": ignored,
        "retained_index_entries": sum(
            1
            for line in entries
            if (_toc_payload(line) or "").startswith("INDEX ")
        ),
        "retained_toc_entries": len(entries),
    }


def build_retained_index_map(
    plan: dict[str, Any],
    index_relations: Mapping[tuple[str, str], tuple[str, str]],
) -> dict[str, Any]:
    relation_targets = {
        tuple(str(item["source_identity"]).split(".", 1)): str(item["target_identity"])
        for item in plan["targets"]
        if item["object_type"] == "relation"
    }
    indexes = []
    for (source_schema, index_name), relation in sorted(index_relations.items()):
        target_relation = relation_targets.get(relation)
        if target_relation is None:
            continue
        target_schema = target_relation.split(".", 1)[0]
        indexes.append({
            "source_identity": f"{source_schema}.{index_name}",
            "source_relation": ".".join(relation),
            "target_identity": f"{target_schema}.{index_name}",
            "target_relation": target_relation,
        })
    return {
        "indexes": indexes,
        "schema_version": "seebx-platform-clean-index-map-v1",
    }


def _replace_identifier(text: str, source: str, target: str) -> str:
    return re.sub(rf"(?<![A-Za-z0-9_]){re.escape(source)}(?![A-Za-z0-9_])", target, text)


def _replace_policy_admin_grantee(text: str) -> str:
    owners = {
        str(item["name"]): str(item["owner"])
        for item in TARGET_SCHEMA_CONTRACTS
    }
    policy = re.compile(
        r"(?im)(^\s*CREATE\s+POLICY\b.*?\bON\s+"
        r"(?P<schema>[a-z_][a-z0-9_]*)\.[a-z_][a-z0-9_]*\b.*?\bTO\s+)"
        r"(?P<roles>[a-z_][a-z0-9_]*(?:\s*,\s*[a-z_][a-z0-9_]*)*)"
        r"(?=\s+(?:USING|WITH\s+CHECK)\b|\s*;)"
    )

    def replace(match: re.Match[str]) -> str:
        roles = [item.strip() for item in match.group("roles").split(",")]
        if "sage" not in roles:
            return match.group(0)
        owner = owners.get(match.group("schema").lower())
        if owner is None:
            raise BaselineContractError("source_admin_policy_schema_invalid")
        rewritten = [owner if item == "sage" else item for item in roles]
        return match.group(1) + ", ".join(dict.fromkeys(rewritten))

    return policy.sub(replace, text)


def canonicalize_schema_sql(source_sql: str, plan: dict[str, Any]) -> tuple[str, dict[str, str]]:
    if re.search(r"(?im)^\s*COPY\s", source_sql):
        raise BaselineContractError("plain_schema_contains_row_data")
    text = source_sql
    for source_identity, target_schema in sorted(PUBLIC_TARGET_SCHEMAS.items(), key=lambda item: -len(item[0])):
        target_identity = f"{target_schema}.{source_identity.split('.', 1)[1]}"
        text = _replace_identifier(text, source_identity, target_identity)
    for source_schema, (target_schema, _) in sorted(SOURCE_SCHEMA_TARGETS.items(), key=lambda item: -len(item[0])):
        if source_schema != target_schema:
            text = _replace_identifier(text, source_schema, target_schema)
    text = _replace_policy_admin_grantee(text)
    for source_role, target_role in sorted(ROLE_NAME_MAP.items(), key=lambda item: -len(item[0])):
        text = _replace_identifier(text, source_role, target_role)

    lowered = text.lower()
    for schema in FORBIDDEN_SCHEMAS | {"chat_history_private", "chat_integrity", "lifeswitch_usage"}:
        if re.search(rf"\b{re.escape(schema)}\s*\.\s*[a-z_]", lowered):
            raise BaselineContractError("legacy_schema_remains_in_baseline")
    for name in FORBIDDEN_PUBLIC_OBJECTS:
        if re.search(rf"\bpublic\s*\.\s*{re.escape(name)}\b", lowered):
            raise BaselineContractError("excluded_public_object_remains_in_baseline")
    for role in ROLE_NAME_MAP:
        if re.search(rf"\b{re.escape(role)}\b", lowered):
            raise BaselineContractError("legacy_role_remains_in_baseline")
    if re.search(r"\bsage\b", lowered):
        raise BaselineContractError("source_admin_role_remains_in_baseline")
    for prefix in FORBIDDEN_ROLE_PREFIXES:
        if re.search(rf"\b{re.escape(prefix)}[a-z0-9_]*\b", lowered):
            raise BaselineContractError("retired_role_remains_in_baseline")
    if re.search(r"(?im)^\s*CREATE\s+(?:SCHEMA|EXTENSION|ROLE)\b", text):
        raise BaselineContractError("schema_archive_contains_bootstrap_ddl")

    relation_kinds: dict[str, str] = {}
    for match in re.finditer(
        r"(?im)^\s*CREATE\s+(TABLE|VIEW|MATERIALIZED\s+VIEW|FOREIGN\s+TABLE)\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\b",
        text,
    ):
        identity = match.group(2).lower()
        if identity in relation_kinds:
            raise BaselineContractError("duplicate_relation_definition")
        relation_kinds[identity] = " ".join(match.group(1).upper().split())
    function_bases = {
        match.group(1).lower()
        for match in re.finditer(
            r"(?im)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\s*\(",
            text,
        )
    }
    expected_relations = {
        str(item["target_identity"]).split("(", 1)[0]
        for item in plan["targets"]
        if item["object_type"] == "relation"
    }
    expected_functions = {
        str(item["target_identity"]).split("(", 1)[0]
        for item in plan["targets"]
        if item["object_type"] == "function"
    }
    if set(relation_kinds) != expected_relations:
        raise BaselineContractError("canonical_relation_definition_set_mismatch")
    if function_bases != expected_functions:
        raise BaselineContractError("canonical_function_definition_set_mismatch")
    return text, relation_kinds


def build_roles_sql() -> str:
    statements = ["\\set ON_ERROR_STOP on"]
    for role in TARGET_ROLE_CONTRACTS:
        name = str(role["name"])
        if IDENTIFIER.fullmatch(name) is None:
            raise BaselineContractError("target_role_name_invalid")
        login = "LOGIN" if role["can_login"] else "NOLOGIN"
        statements.append(
            f"CREATE ROLE {name} {login} NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;"
        )
    for member, granted in sorted(TARGET_MEMBERSHIPS):
        statements.append(f"GRANT {granted} TO {member};")
    return "\n".join(statements) + "\n"


def build_namespaces_sql() -> str:
    statements = [
        "\\set ON_ERROR_STOP on",
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
        "DO $plpgsql$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname='plpgsql' AND extversion='1.0') THEN RAISE EXCEPTION 'plpgsql_extension_contract_invalid'; END IF; END $plpgsql$;",
        "CREATE EXTENSION pgcrypto WITH SCHEMA public VERSION '1.3';",
    ]
    for item in TARGET_SCHEMA_CONTRACTS:
        statements.append(f"CREATE SCHEMA {item['name']} AUTHORIZATION {item['owner']};")
    return "\n".join(statements) + "\n"


def build_owners_sql(plan: dict[str, Any], relation_kinds: dict[str, str]) -> str:
    statements = ["\\set ON_ERROR_STOP on"]
    for item in plan["targets"]:
        target = str(item["target_identity"])
        owner = str(item["target_owner"])
        if item["object_type"] == "function":
            statements.append(f"ALTER FUNCTION {target} OWNER TO {owner};")
            continue
        kind = relation_kinds[target]
        alter_kind = "MATERIALIZED VIEW" if kind == "MATERIALIZED VIEW" else "VIEW" if kind == "VIEW" else "FOREIGN TABLE" if kind == "FOREIGN TABLE" else "TABLE"
        statements.append(f"ALTER {alter_kind} {target} OWNER TO {owner};")
    return "\n".join(statements) + "\n"


def build_privileges_sql(plan: dict[str, Any], relation_kinds: dict[str, str]) -> str:
    app = "seebx_platform_app_v1"
    writer = "seebx_usage_writer_v1"
    admin = "seebx_usage_admin_v1"
    statements = ["\\set ON_ERROR_STOP on"]
    for schema in sorted({str(item["name"]) for item in TARGET_SCHEMA_CONTRACTS}):
        statements.append(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC;")
    for schema in ("ai_operations", "conversation", "conversation_integrity", "conversation_private", "telemetry", "trusted_web", "user_settings", "voice"):
        statements.append(f"GRANT USAGE ON SCHEMA {schema} TO {app};")
    statements.extend([
        f"GRANT USAGE ON SCHEMA usage TO {writer};",
        f"GRANT USAGE ON SCHEMA usage TO {admin};",
    ])

    for item in plan["targets"]:
        identity = str(item["target_identity"])
        schema = identity.split(".", 1)[0]
        if item["object_type"] == "function":
            statements.append(f"REVOKE ALL ON FUNCTION {identity} FROM PUBLIC;")
            if schema in {"ai_operations", "conversation", "conversation_private"}:
                statements.append(f"GRANT EXECUTE ON FUNCTION {identity} TO {app};")
            elif schema == "usage":
                statements.append(f"GRANT EXECUTE ON FUNCTION {identity} TO {writer};")
            continue
        statements.append(f"REVOKE ALL ON TABLE {identity} FROM PUBLIC;")
        if schema == "conversation":
            statements.append(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {identity} TO {app};")
        elif schema == "conversation_integrity":
            statements.append(f"GRANT SELECT, INSERT ON TABLE {identity} TO {app};")
        elif schema == "usage":
            statements.append(f"GRANT SELECT, INSERT ON TABLE {identity} TO {writer};")
            statements.append(f"GRANT SELECT ON TABLE {identity} TO {admin};")
        elif schema == "telemetry":
            statements.append(f"GRANT SELECT, INSERT ON TABLE {identity} TO {app};")
        elif schema in {"voice", "user_settings"}:
            statements.append(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {identity} TO {app};")
        elif schema == "trusted_web":
            if relation_kinds[identity] in {"VIEW", "MATERIALIZED VIEW"}:
                statements.append(f"GRANT SELECT ON TABLE {identity} TO {app};")
            else:
                statements.append(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {identity} TO {app};")
    return "\n".join(statements) + "\n"


def _run_text(command: list[str], *, label: str, timeout: int = 300) -> str:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BaselineExecutionError(f"{label}_execution_failed") from error
    if completed.returncode != 0:
        raise BaselineExecutionError(f"{label}_failed")
    return completed.stdout


def _write_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _prepare_output(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise BaselineContractError("output_already_exists")
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise BaselineContractError("output_parent_invalid")
    path.mkdir(mode=0o700)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise BaselineContractError("output_mode_invalid")
    return path


def execute(
    *,
    disposition_path: Path,
    disposition_sha256: str,
    schema_archive: Path,
    schema_archive_sha256: str,
    candidate_commit: str,
    output: Path,
) -> dict[str, Any]:
    if COMMIT.fullmatch(candidate_commit) is None:
        raise BaselineContractError("candidate_commit_invalid")
    manifest = read_disposition(disposition_path, disposition_sha256)
    plan = validate_disposition(manifest)
    _read_hash_bound(schema_archive, schema_archive_sha256, label="schema_archive")
    output = _prepare_output(output)
    index_map_path = output / "platform-clean-index-map-v1.json"
    restore_list_path = output / "platform-clean-restore-v1.list"
    source_sql_path = output / "platform-clean-source-schema-v1.sql"
    schema_sql_path = output / "platform-clean-schema-v1.sql"
    roles_path = output / "platform-clean-roles-v1.sql"
    namespaces_path = output / "platform-clean-namespaces-v1.sql"
    owners_path = output / "platform-clean-owners-v1.sql"
    privileges_path = output / "platform-clean-privileges-v1.sql"
    install_path = output / "platform-clean-install-v1.sql"
    receipt_path = output / "platform-clean-baseline-receipt-v1.json"

    raw_list = _run_text([PG_RESTORE, "--list", str(schema_archive)], label="archive_list")
    post_data_sql = _run_text(
        [
            PG_RESTORE,
            "--schema-only",
            "--section=post-data",
            "--no-owner",
            "--no-privileges",
            "--file=-",
            str(schema_archive),
        ],
        label="archive_post_data_render",
    )
    index_relations = extract_index_relation_map(raw_list, post_data_sql)
    retained_index_map = build_retained_index_map(plan, index_relations)
    _write_exclusive(index_map_path, canonical_bytes(retained_index_map) + b"\n")
    filtered, toc_summary = filter_restore_list(raw_list, plan, index_relations)
    if toc_summary["retained_index_entries"] != len(retained_index_map["indexes"]):
        raise BaselineContractError("retained_index_count_mismatch")
    _write_exclusive(restore_list_path, filtered.encode())
    rendered = _run_text(
        [
            PG_RESTORE,
            "--no-owner",
            "--no-privileges",
            "--use-list=" + str(restore_list_path),
            "--file=-",
            str(schema_archive),
        ],
        label="schema_render",
    )
    _write_exclusive(source_sql_path, rendered.encode())
    canonical_sql, relation_kinds = canonicalize_schema_sql(rendered, plan)
    _write_exclusive(schema_sql_path, canonical_sql.encode())
    _write_exclusive(roles_path, build_roles_sql().encode())
    _write_exclusive(namespaces_path, build_namespaces_sql().encode())
    _write_exclusive(owners_path, build_owners_sql(plan, relation_kinds).encode())
    _write_exclusive(privileges_path, build_privileges_sql(plan, relation_kinds).encode())
    install_sql = """\\set ON_ERROR_STOP on
BEGIN;
\\ir platform-clean-roles-v1.sql
\\ir platform-clean-namespaces-v1.sql
\\ir platform-clean-schema-v1.sql
\\ir platform-clean-owners-v1.sql
\\ir platform-clean-privileges-v1.sql
COMMIT;
"""
    _write_exclusive(install_path, install_sql.encode())

    artifact_paths = (
        index_map_path,
        restore_list_path,
        source_sql_path,
        schema_sql_path,
        roles_path,
        namespaces_path,
        owners_path,
        privileges_path,
        install_path,
    )
    artifacts = {
        path.name: {
            "bytes": path.stat().st_size,
            "mode": oct(stat.S_IMODE(path.stat().st_mode)),
            "sha256": sha256_file(path),
        }
        for path in artifact_paths
    }
    receipt = {
        "artifacts": artifacts,
        "candidate_commit": candidate_commit,
        "completed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "data_copy_authority": False,
        "deletion_authority": False,
        "disposition_sha256": disposition_sha256,
        "installation_authority": False,
        "object_plan": plan,
        "production_change_authority": False,
        "schema_archive_sha256": schema_archive_sha256,
        "schema_only": True,
        "schema_version": SCHEMA_VERSION,
        "status": "candidate_schema_ready",
        "toc_summary": toc_summary,
    }
    receipt_bytes = canonical_bytes(receipt) + b"\n"
    _write_exclusive(receipt_path, receipt_bytes)
    return {
        "baseline_receipt": str(receipt_path),
        "baseline_receipt_sha256": sha256_bytes(receipt_bytes),
        "candidate_commit": candidate_commit,
        "canonical_schema_sha256": artifacts[schema_sql_path.name]["sha256"],
        "retained_product_objects": len(plan["targets"]),
        "status": "candidate_schema_ready",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--disposition", required=True, type=Path)
    parser.add_argument("--disposition-sha256", required=True)
    parser.add_argument("--schema-archive", required=True, type=Path)
    parser.add_argument("--schema-archive-sha256", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = execute(
            disposition_path=arguments.disposition,
            disposition_sha256=arguments.disposition_sha256,
            schema_archive=arguments.schema_archive,
            schema_archive_sha256=arguments.schema_archive_sha256,
            candidate_commit=arguments.candidate_commit,
            output=arguments.output,
        )
    except (BaselineContractError, BaselineExecutionError) as error:
        print(json.dumps({"error": str(error), "status": "failed"}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
