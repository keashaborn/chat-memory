#!/usr/bin/env python3
"""Content-free, fail-closed verifier for the complete governed Memory path."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import sys
from typing import Any


CONTRACT_VERSION = "memory_v1_active_runtime_verifier_v1"
MANIFEST_CONTRACT = "memory_v1_active_runtime_manifest_v1"
BINDING_CONTRACT = "memory_v1_active_runtime_release_binding_v1"
TARGET_ACTIVATION_STATE = (
    "installed_inactive_with_compatibility_active"
)
DEFAULT_BINDING = Path(
    "/etc/chat-memory/memory-v1-active-runtime-release-binding-v1.json"
)
MAX_JSON_BYTES = 262_144
MAX_HASHED_FILE_BYTES = 134_217_728
SHA256_KEYS = set("0123456789abcdef")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "ops/systemd/memory-v1-active-runtime-manifest-v1.json"
FIXED_ENV = {
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
}
UNIT_NAME = re.compile(r"^[a-z0-9][a-z0-9_.@:-]*\.(?:service|timer)$")
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


class RuntimeVerificationError(RuntimeError):
    pass


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeVerificationError("JSON contains a duplicate key")
        value[key] = item
    return value


def decode_json(value: bytes, *, label: str) -> dict[str, Any]:
    if not value or len(value) > MAX_JSON_BYTES or value.startswith(b"\xef\xbb\xbf"):
        raise RuntimeVerificationError(f"{label} bytes are invalid")
    try:
        text = value.decode("utf-8")
        decoded = json.loads(text, object_pairs_hook=_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeVerificationError(f"{label} JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise RuntimeVerificationError(f"{label} must be an object")
    return decoded


def read_regular(
    path: Path,
    *,
    label: str,
    require_root_0600: bool = False,
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeVerificationError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeVerificationError(f"{label} is not a regular single-link file")
        if require_root_0600 and (
            before.st_uid != 0
            or before.st_gid != 0
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise RuntimeVerificationError(f"{label} ownership or mode is invalid")
        if before.st_size < 1 or before.st_size > MAX_JSON_BYTES:
            raise RuntimeVerificationError(f"{label} size is invalid")
        chunks: list[bytes] = []
        remaining = MAX_JSON_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_mode,
            before.st_uid,
            before.st_gid,
            before.st_nlink,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
        )
        if identity_before != identity_after or len(value) != before.st_size:
            raise RuntimeVerificationError(f"{label} changed while it was read")
        return value, before
    finally:
        os.close(descriptor)


def sha256_file(
    path: Path,
    *,
    label: str,
    require_root_mode: int | None = None,
) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeVerificationError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 1
            or before.st_size > MAX_HASHED_FILE_BYTES
        ):
            raise RuntimeVerificationError(f"{label} file identity is invalid")
        if require_root_mode is not None and (
            before.st_uid != 0
            or before.st_gid != 0
            or stat.S_IMODE(before.st_mode) != require_root_mode
        ):
            raise RuntimeVerificationError(f"{label} ownership or mode is invalid")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_mode,
            before.st_uid,
            before.st_gid,
            before.st_nlink,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
        )
        if identity_before != identity_after or size != before.st_size:
            raise RuntimeVerificationError(f"{label} changed while it was hashed")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def required_sha256(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in SHA256_KEYS for character in value)
    ):
        raise RuntimeVerificationError(f"{label} is not lowercase SHA-256")
    return value


def required_git_oid(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in SHA256_KEYS for character in value)
    ):
        raise RuntimeVerificationError(f"{label} is not a full Git object ID")
    return value


def required_relative_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeVerificationError(f"{label} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise RuntimeVerificationError(f"{label} is not a safe relative path")
    return value


def run_fixed(arguments: list[str], *, allowed_codes: set[int] = {0}) -> str:
    completed = subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
        env=FIXED_ENV,
    )
    if completed.returncode not in allowed_codes:
        raise RuntimeVerificationError("fixed runtime probe failed")
    if len(completed.stdout) > MAX_JSON_BYTES or len(completed.stderr) > MAX_JSON_BYTES:
        raise RuntimeVerificationError("fixed runtime probe exceeded its output bound")
    try:
        return completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeVerificationError("fixed runtime probe returned non-UTF-8") from exc


def git_identity(root: Path) -> dict[str, Any]:
    prefix = [
        "/usr/bin/git",
        "-c",
        f"safe.directory={root}",
        "-C",
        str(root),
    ]
    commit = run_fixed([*prefix, "rev-parse", "HEAD"])
    tree = run_fixed([*prefix, "rev-parse", "HEAD^{tree}"])
    status = run_fixed(
        [*prefix, "status", "--porcelain=v1", "--untracked-files=all"]
    )
    required_git_oid(commit, label="repository commit")
    required_git_oid(tree, label="repository tree")
    if status:
        raise RuntimeVerificationError("repository tracked state is not clean")
    return {"commit": commit, "tree": tree, "tracked_clean": True}


def systemd_unit_state(unit: str) -> dict[str, str]:
    if not UNIT_NAME.fullmatch(unit):
        raise RuntimeVerificationError("systemd unit name is invalid")
    properties = run_fixed(
        [
            "/usr/bin/systemctl",
            "show",
            unit,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=FragmentPath",
            "--no-pager",
        ]
    )
    values: dict[str, str] = {}
    for line in properties.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in values:
            raise RuntimeVerificationError("systemd property output is ambiguous")
        values[key] = value
    if set(values) != {"LoadState", "ActiveState", "FragmentPath"}:
        raise RuntimeVerificationError("systemd property output is incomplete")
    enabled = run_fixed(
        ["/usr/bin/systemctl", "is-enabled", unit], allowed_codes={0, 1, 3, 4}
    )
    if enabled not in {
        "enabled",
        "disabled",
        "static",
        "indirect",
        "masked",
        "not-found",
        "generated",
        "transient",
    }:
        raise RuntimeVerificationError("systemd enabled state is unknown")
    return {
        "active_state": values["ActiveState"],
        "enabled_state": enabled,
        "fragment_path": values["FragmentPath"],
        "load_state": values["LoadState"],
    }


def discovered_memory_units() -> list[str]:
    output = run_fixed(
        [
            "/usr/bin/systemctl",
            "list-unit-files",
            "--no-legend",
            "--no-pager",
            "memory-v1-*",
        ]
    )
    names: list[str] = []
    for line in output.splitlines():
        fields = line.split()
        if not fields or not UNIT_NAME.fullmatch(fields[0]):
            raise RuntimeVerificationError("installed Memory unit inventory is invalid")
        names.append(fields[0])
    if len(names) != len(set(names)):
        raise RuntimeVerificationError("installed Memory unit inventory is duplicated")
    return sorted(names)


def absent_path(path: Path, *, label: str) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise RuntimeVerificationError(f"{label} cannot be inspected") from exc
    return False


def parse_environment_file(value: bytes) -> dict[str, str]:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeVerificationError("runtime configuration is not UTF-8") from exc
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.partition("=")
        if not separator or not ENV_KEY.fullmatch(key) or key in result:
            raise RuntimeVerificationError("runtime configuration line is invalid")
        try:
            values = shlex.split(raw_value, comments=False, posix=True)
        except ValueError as exc:
            raise RuntimeVerificationError("runtime configuration quoting is invalid") from exc
        if len(values) != 1:
            raise RuntimeVerificationError("runtime configuration value is ambiguous")
        result[key] = values[0]
    return result


def _manifest_catalog_functions(manifest: dict[str, Any]) -> list[dict[str, str]]:
    contract = manifest.get("catalog_contract")
    if not isinstance(contract, dict) or not isinstance(contract.get("functions"), list):
        raise RuntimeVerificationError("catalog function contract is invalid")
    functions: list[dict[str, str]] = []
    signatures: set[str] = set()
    for item in contract["functions"]:
        if not isinstance(item, dict):
            raise RuntimeVerificationError("catalog function declaration is invalid")
        signature = item.get("signature")
        if not isinstance(signature, str) or not signature.startswith("memory."):
            raise RuntimeVerificationError("catalog function signature is invalid")
        if signature in signatures:
            raise RuntimeVerificationError("catalog function signature is duplicated")
        signatures.add(signature)
        required_sha256(item.get("sha256"), label=f"{signature} definition hash")
        functions.append(item)
    return functions


async def probe_catalog(
    manifest: dict[str, Any], *, dsn: str
) -> dict[str, Any]:
    if not dsn:
        raise RuntimeVerificationError("POSTGRES_DSN is absent")
    try:
        import asyncpg
    except ImportError as exc:
        raise RuntimeVerificationError("asyncpg is unavailable") from exc
    connection = await asyncpg.connect(dsn, command_timeout=10)
    try:
        role = await connection.fetchrow(
            """
            SELECT rolname, rolsuper, rolbypassrls
              FROM pg_catalog.pg_roles
             WHERE rolname = current_user
            """
        )
        if role is None:
            raise RuntimeVerificationError("catalog actor role is absent")
        function_hashes: dict[str, str] = {}
        for item in _manifest_catalog_functions(manifest):
            definition = await connection.fetchval(
                "SELECT pg_catalog.pg_get_functiondef(pg_catalog.to_regprocedure($1))",
                item["signature"],
            )
            if not isinstance(definition, str):
                raise RuntimeVerificationError("catalog function is absent")
            function_hashes[item["signature"]] = sha256_bytes(
                definition.encode("utf-8")
            )
        relation_states: dict[str, dict[str, bool]] = {}
        relations = manifest["catalog_contract"].get("forced_rls_relations")
        if not isinstance(relations, list):
            raise RuntimeVerificationError("catalog relation contract is invalid")
        for qualified in relations:
            if not isinstance(qualified, str) or qualified.count(".") != 1:
                raise RuntimeVerificationError("catalog relation name is invalid")
            schema_name, relation_name = qualified.split(".", 1)
            row = await connection.fetchrow(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity
                  FROM pg_catalog.pg_class AS c
                  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = $1
                   AND c.relname = $2
                   AND c.relkind IN ('r','p')
                """,
                schema_name,
                relation_name,
            )
            if row is None:
                raise RuntimeVerificationError("catalog relation is absent")
            relation_states[qualified] = {
                "forced_rls": bool(row["relforcerowsecurity"]),
                "rls": bool(row["relrowsecurity"]),
            }
        return {
            "actor_role": {
                "bypass_rls": bool(role["rolbypassrls"]),
                "name": str(role["rolname"]),
                "superuser": bool(role["rolsuper"]),
            },
            "function_sha256": function_hashes,
            "relations": relation_states,
        }
    finally:
        await connection.close()


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("contract_version") != MANIFEST_CONTRACT:
        raise RuntimeVerificationError("runtime manifest contract is invalid")
    if manifest.get("activation_state") != TARGET_ACTIVATION_STATE:
        raise RuntimeVerificationError("runtime manifest activation state is invalid")
    verification = manifest.get("verification")
    if (
        not isinstance(verification, dict)
        or verification.get("contract_version") != CONTRACT_VERSION
    ):
        raise RuntimeVerificationError("runtime verification policy is invalid")
    phases = verification.get("supported_phases")
    if not isinstance(phases, dict) or set(phases) != {"installed_inactive"}:
        raise RuntimeVerificationError("runtime verification phases are invalid")
    runtime_config = Path(str(verification.get("runtime_config", "")))
    catalog_environment = Path(
        str(verification.get("catalog_environment", ""))
    )
    if (
        not runtime_config.is_absolute()
        or not catalog_environment.is_absolute()
        or runtime_config == catalog_environment
    ):
        raise RuntimeVerificationError("runtime environment paths are invalid")

    source_ids: set[str] = set()
    source_paths: set[str] = set()
    for item in manifest.get("source_components", []):
        if not isinstance(item, dict):
            raise RuntimeVerificationError("source component is invalid")
        component_id = item.get("component_id")
        if not isinstance(component_id, str) or component_id in source_ids:
            raise RuntimeVerificationError("source component identity is invalid")
        source_ids.add(component_id)
        path = required_relative_path(item.get("path"), label="source component path")
        if path in source_paths:
            raise RuntimeVerificationError("source component path is duplicated")
        source_paths.add(path)
        required_sha256(item.get("sha256"), label=f"{path} source hash")

    unit_names: set[str] = set()
    memory_units: list[str] = []
    for item in manifest.get("systemd_units", []):
        if not isinstance(item, dict):
            raise RuntimeVerificationError("systemd unit declaration is invalid")
        name = item.get("name")
        if not isinstance(name, str) or not UNIT_NAME.fullmatch(name) or name in unit_names:
            raise RuntimeVerificationError("systemd unit identity is invalid")
        unit_names.add(name)
        if name.startswith("memory-v1-"):
            memory_units.append(name)
        required_sha256(item.get("installed_sha256"), label=f"{name} installed hash")
        if "source_path" in item:
            required_relative_path(item["source_path"], label=f"{name} source path")
            required_sha256(item.get("source_sha256"), label=f"{name} source hash")
            if item["source_sha256"] != item["installed_sha256"]:
                raise RuntimeVerificationError("source-bound unit hashes disagree")
        expected = item.get("expected")
        if not isinstance(expected, dict) or set(expected) != {"installed_inactive"}:
            raise RuntimeVerificationError("systemd unit phase declaration is invalid")
        required_state = expected["installed_inactive"]
        if not isinstance(required_state, dict) or set(required_state) != {
            "active_state",
            "enabled_state",
            "load_state",
        }:
            raise RuntimeVerificationError("systemd unit state declaration is invalid")
    if manifest.get("exact_installed_memory_unit_set") != sorted(memory_units):
        raise RuntimeVerificationError("exact installed Memory unit set is invalid")

    _manifest_catalog_functions(manifest)
    relations = manifest["catalog_contract"].get("forced_rls_relations")
    if (
        not isinstance(relations, list)
        or not relations
        or len(relations) != len(set(relations))
    ):
        raise RuntimeVerificationError("forced-RLS relation contract is invalid")
    blockers = manifest.get("blockers")
    required_blockers = {
        "review_to_claim_admission_manual_pilot_authorization_required",
        "user_claim_lifecycle_authenticated_validation_pending",
        "governed_owner_activation_refresh_required",
    }
    if not isinstance(blockers, list):
        raise RuntimeVerificationError("runtime manifest blockers are invalid")
    blocker_ids: set[str] = set()
    for item in blockers:
        if not isinstance(item, dict):
            raise RuntimeVerificationError("runtime manifest blocker is invalid")
        blocker_id = item.get("id")
        status = item.get("status")
        if (
            not isinstance(blocker_id, str)
            or blocker_id in blocker_ids
            or not isinstance(status, str)
            or not status
            or not any(
                item.get(field) is True
                for field in (
                    "blocks_acceptance",
                    "blocks_activation",
                    "blocks_governed_claim",
                )
            )
        ):
            raise RuntimeVerificationError("runtime manifest blocker is malformed")
        blocker_ids.add(blocker_id)
    if blocker_ids != required_blockers:
        raise RuntimeVerificationError("runtime manifest hides a required blocker")

    handoffs = manifest.get("runtime_handoffs")
    required_handoffs = {
        "chat_capture",
        "contextual_intake",
        "eligibility_disposition",
        "provider_extraction",
        "openai_packet_routing",
        "review_to_claim_admission",
        "derived_projection",
        "chat_b_retrieval",
        "answer_binding",
        "user_provenance_and_lifecycle",
    }
    if not isinstance(handoffs, list):
        raise RuntimeVerificationError("runtime handoff manifest is invalid")
    handoff_stages: set[str] = set()
    for item in handoffs:
        if not isinstance(item, dict):
            raise RuntimeVerificationError("runtime handoff is invalid")
        stage = item.get("stage")
        if (
            not isinstance(stage, str)
            or stage in handoff_stages
            or not isinstance(item.get("producer"), str)
            or not isinstance(item.get("consumer"), str)
            or not isinstance(item.get("status"), str)
        ):
            raise RuntimeVerificationError("runtime handoff is malformed")
        handoff_stages.add(stage)
    if handoff_stages != required_handoffs:
        raise RuntimeVerificationError("runtime manifest hides a required handoff")


def load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    value, _ = read_regular(path, label="runtime manifest")
    manifest = decode_json(value, label="runtime manifest")
    validate_manifest(manifest)
    return manifest, sha256_bytes(value)


def probe_live(manifest: dict[str, Any], *, root: Path) -> dict[str, Any]:
    validate_manifest(manifest)
    source_hashes: dict[str, str] = {}
    for item in manifest["source_components"]:
        path = item["path"]
        observed = sha256_file(root / path, label=f"{path} source")
        if observed != item["sha256"]:
            raise RuntimeVerificationError(f"{path} source hash changed")
        source_hashes[path] = observed

    units: dict[str, dict[str, str]] = {}
    installed_hashes: dict[str, str] = {}
    for item in manifest["systemd_units"]:
        name = item["name"]
        if "source_path" in item:
            observed_source = sha256_file(
                root / item["source_path"], label=f"{name} source"
            )
            if observed_source != item["source_sha256"]:
                raise RuntimeVerificationError(f"{name} source hash changed")
            source_hashes[item["source_path"]] = observed_source
        unit_state = systemd_unit_state(name)
        units[name] = unit_state
        if unit_state["load_state"] == "loaded":
            fragment = Path(unit_state["fragment_path"])
            if not fragment.is_absolute():
                raise RuntimeVerificationError(f"{name} fragment path is invalid")
            installed_hashes[name] = sha256_file(
                fragment,
                label=f"{name} installed fragment",
                require_root_mode=0o644,
            )

    sentinels: dict[str, str] = {}
    for item in manifest.get("timer_sentinels", []):
        path = Path(str(item.get("path", "")))
        if not path.is_absolute():
            raise RuntimeVerificationError("timer sentinel path is invalid")
        sentinels[str(path)] = (
            "absent" if absent_path(path, label="timer sentinel") else "present"
        )

    verification = manifest["verification"]
    config_path = Path(str(verification.get("runtime_config", "")))
    if not config_path.is_absolute():
        raise RuntimeVerificationError("runtime configuration path is invalid")
    config_value, config_info = read_regular(
        config_path, label="runtime configuration", require_root_0600=True
    )
    parse_environment_file(config_value)
    catalog_environment_path = Path(
        str(verification.get("catalog_environment", ""))
    )
    if not catalog_environment_path.is_absolute():
        raise RuntimeVerificationError("catalog environment path is invalid")
    catalog_environment_value, catalog_environment_info = read_regular(
        catalog_environment_path,
        label="catalog environment",
        require_root_0600=True,
    )
    catalog_environment = parse_environment_file(catalog_environment_value)
    catalog = asyncio.run(
        probe_catalog(
            manifest,
            dsn=catalog_environment.get("POSTGRES_DSN", ""),
        )
    )
    config_state = {
        "sha256": sha256_bytes(config_value),
        "state": "root_owned_0600_regular_single_link",
        "size": config_info.st_size,
    }
    catalog_environment_state = {
        "sha256": sha256_bytes(catalog_environment_value),
        "state": "root_owned_0600_regular_single_link",
        "size": catalog_environment_info.st_size,
    }

    python_path = Path(str(verification.get("python_executable", "")))
    if not python_path.is_absolute():
        raise RuntimeVerificationError("Python executable path is invalid")
    python_sha256 = sha256_file(
        python_path.resolve(strict=True), label="Python executable"
    )
    try:
        sdk_version = importlib.metadata.version("openai")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeVerificationError("OpenAI SDK package is absent") from exc

    return {
        "catalog": catalog,
        "catalog_environment": catalog_environment_state,
        "config": config_state,
        "discovered_memory_units": discovered_memory_units(),
        "installed_unit_sha256": installed_hashes,
        "openai_sdk_version": sdk_version,
        "python_executable_sha256": python_sha256,
        "repository": git_identity(root),
        "sentinels": sentinels,
        "source_sha256": source_hashes,
        "units": units,
    }


def load_binding(path: Path) -> tuple[dict[str, Any], str]:
    if path != DEFAULT_BINDING:
        raise RuntimeVerificationError("release binding path is not fixed")
    value, _ = read_regular(path, label="release binding", require_root_0600=True)
    binding = decode_json(value, label="release binding")
    allowed = {
        "catalog_environment_sha256",
        "catalog_function_sha256",
        "contract_version",
        "manifest_sha256",
        "openai_sdk_version",
        "phase",
        "python_executable_sha256",
        "repository_commit",
        "repository_tree",
        "runtime_config_sha256",
        "source_sha256",
        "unit_sha256",
    }
    if set(binding) != allowed or binding.get("contract_version") != BINDING_CONTRACT:
        raise RuntimeVerificationError("release binding fields or contract are invalid")
    if value != stable_json(binding).encode("utf-8"):
        raise RuntimeVerificationError("release binding is not canonical JSON")
    return binding, sha256_bytes(value)


def _required_hash_map(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise RuntimeVerificationError(f"{label} is invalid")
    for key, digest in value.items():
        if not isinstance(key, str) or not key:
            raise RuntimeVerificationError(f"{label} key is invalid")
        required_sha256(digest, label=f"{label} value")
    return value


def build_binding(
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    manifest_sha256: str,
    phase: str,
) -> dict[str, Any]:
    """Build the canonical content-free binding for one verified snapshot."""
    validate_manifest(manifest)
    if phase not in manifest["verification"]["supported_phases"]:
        raise RuntimeVerificationError("runtime verification phase is unsupported")
    catalog = snapshot.get("catalog")
    if not isinstance(catalog, dict):
        raise RuntimeVerificationError("catalog snapshot is absent")
    binding = {
        "catalog_environment_sha256": snapshot.get(
            "catalog_environment", {}
        ).get("sha256"),
        "catalog_function_sha256": catalog.get("function_sha256"),
        "contract_version": BINDING_CONTRACT,
        "manifest_sha256": manifest_sha256,
        "openai_sdk_version": snapshot.get("openai_sdk_version"),
        "phase": phase,
        "python_executable_sha256": snapshot.get(
            "python_executable_sha256"
        ),
        "repository_commit": snapshot.get("repository", {}).get("commit"),
        "repository_tree": snapshot.get("repository", {}).get("tree"),
        "runtime_config_sha256": snapshot.get("config", {}).get("sha256"),
        "source_sha256": snapshot.get("source_sha256"),
        "unit_sha256": snapshot.get("installed_unit_sha256"),
    }
    verify_snapshot(
        manifest,
        snapshot,
        manifest_sha256=manifest_sha256,
        phase=phase,
        binding=binding,
    )
    return binding


def verify_snapshot(
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    manifest_sha256: str,
    phase: str,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_manifest(manifest)
    policies = manifest["verification"]["supported_phases"]
    if phase not in policies:
        raise RuntimeVerificationError("runtime verification phase is unsupported")
    policy = policies[phase]
    if (policy.get("binding_required") is True) != (binding is not None):
        raise RuntimeVerificationError("runtime release binding disposition is invalid")
    if snapshot.get("config", {}).get("state") != policy.get("runtime_config_state"):
        raise RuntimeVerificationError("runtime configuration state does not match policy")
    if snapshot.get("catalog_environment", {}).get("state") != policy.get(
        "catalog_environment_state"
    ):
        raise RuntimeVerificationError(
            "catalog environment state does not match policy"
        )
    if snapshot.get("openai_sdk_version") != manifest["verification"].get(
        "openai_sdk_version"
    ):
        raise RuntimeVerificationError("OpenAI SDK version does not match policy")
    if snapshot.get("repository", {}).get("tracked_clean") is not True:
        raise RuntimeVerificationError("repository is not clean")

    source_hashes = snapshot.get("source_sha256")
    if not isinstance(source_hashes, dict):
        raise RuntimeVerificationError("source hash snapshot is absent")
    expected_source: dict[str, str] = {
        item["path"]: item["sha256"] for item in manifest["source_components"]
    }
    for item in manifest["systemd_units"]:
        if "source_path" in item:
            expected_source[item["source_path"]] = item["source_sha256"]
    if source_hashes != expected_source:
        raise RuntimeVerificationError("source hashes do not match manifest")

    units = snapshot.get("units")
    installed_hashes = snapshot.get("installed_unit_sha256")
    if not isinstance(units, dict) or not isinstance(installed_hashes, dict):
        raise RuntimeVerificationError("systemd snapshot is incomplete")
    for item in manifest["systemd_units"]:
        name = item["name"]
        state_value = units.get(name)
        if state_value != item["expected"][phase]:
            raise RuntimeVerificationError(f"{name} state does not match policy")
        if installed_hashes.get(name) != item["installed_sha256"]:
            raise RuntimeVerificationError(f"{name} installed hash does not match")
    expected_names = {item["name"] for item in manifest["systemd_units"]}
    if set(units) != expected_names or set(installed_hashes) != expected_names:
        raise RuntimeVerificationError("systemd snapshot contains omitted or extra units")
    if snapshot.get("discovered_memory_units") != manifest[
        "exact_installed_memory_unit_set"
    ]:
        raise RuntimeVerificationError("installed Memory unit set changed")

    for item in manifest.get("timer_sentinels", []):
        if snapshot.get("sentinels", {}).get(item["path"]) != item["expected"][phase]:
            raise RuntimeVerificationError("timer sentinel state does not match policy")

    catalog = snapshot.get("catalog")
    if not isinstance(catalog, dict):
        raise RuntimeVerificationError("catalog snapshot is absent")
    if catalog.get("actor_role") != manifest["catalog_contract"]["actor_role"]:
        raise RuntimeVerificationError("catalog actor role does not match")
    expected_function_hashes = {
        item["signature"]: item["sha256"]
        for item in manifest["catalog_contract"]["functions"]
    }
    if catalog.get("function_sha256") != expected_function_hashes:
        raise RuntimeVerificationError("catalog function hashes do not match")
    expected_relations = {
        name: {"forced_rls": True, "rls": True}
        for name in manifest["catalog_contract"]["forced_rls_relations"]
    }
    if catalog.get("relations") != expected_relations:
        raise RuntimeVerificationError("catalog forced-RLS contract does not match")

    binding_sha256 = None
    if binding is not None:
        exact_pairs = (
            ("phase", phase),
            ("manifest_sha256", manifest_sha256),
            ("repository_commit", snapshot["repository"]["commit"]),
            ("repository_tree", snapshot["repository"]["tree"]),
            (
                "catalog_environment_sha256",
                snapshot["catalog_environment"].get("sha256"),
            ),
            ("runtime_config_sha256", snapshot["config"].get("sha256")),
            ("python_executable_sha256", snapshot["python_executable_sha256"]),
            ("openai_sdk_version", snapshot["openai_sdk_version"]),
            ("source_sha256", source_hashes),
            ("unit_sha256", installed_hashes),
            ("catalog_function_sha256", expected_function_hashes),
        )
        for field, observed in exact_pairs:
            if binding.get(field) != observed:
                raise RuntimeVerificationError(f"release binding {field} does not match")
        for field in (
            "catalog_environment_sha256",
            "manifest_sha256",
            "runtime_config_sha256",
            "python_executable_sha256",
        ):
            required_sha256(binding.get(field), label=f"release binding {field}")
        required_git_oid(
            binding.get("repository_commit"), label="release binding repository_commit"
        )
        required_git_oid(
            binding.get("repository_tree"), label="release binding repository_tree"
        )
        _required_hash_map(binding.get("source_sha256"), label="binding source hashes")
        _required_hash_map(binding.get("unit_sha256"), label="binding unit hashes")
        _required_hash_map(
            binding.get("catalog_function_sha256"), label="binding function hashes"
        )
        binding_sha256 = sha256_bytes(stable_json(binding).encode("utf-8"))

    active_compatibility = sum(
        1
        for item in manifest["systemd_units"]
        if "compatibility" in item["classification"]
        and item["expected"][phase]["active_state"] == "active"
    )
    return {
        "binding_sha256": binding_sha256,
        "blocker_count": len(manifest["blockers"]),
        "catalog_matches_manifest": True,
        "compatibility_active_count": active_compatibility,
        "contract_version": CONTRACT_VERSION,
        "installed_memory_unit_count": len(
            manifest["exact_installed_memory_unit_set"]
        ),
        "manifest_sha256": manifest_sha256,
        "phase": phase,
        "provider_calls": 0,
        "repository_commit": snapshot["repository"]["commit"],
        "repository_tree": snapshot["repository"]["tree"],
        "runtime_matches_manifest": True,
        "successor_exclusivity_verified": True,
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the content-free complete-path state for governed Memory V1."
    )
    parser.add_argument("--phase", choices=("installed_inactive",), required=True)
    disposition = parser.add_mutually_exclusive_group(required=True)
    disposition.add_argument("--binding", type=Path)
    disposition.add_argument("--emit-binding", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    try:
        manifest, manifest_sha256 = load_manifest(DEFAULT_MANIFEST)
        snapshot = probe_live(manifest, root=ROOT)
        if args.emit_binding:
            binding = build_binding(
                manifest,
                snapshot,
                manifest_sha256=manifest_sha256,
                phase=args.phase,
            )
            print(stable_json(binding))
            return 0
        binding, binding_file_sha256 = load_binding(args.binding)
        report = verify_snapshot(
            manifest,
            snapshot,
            manifest_sha256=manifest_sha256,
            phase=args.phase,
            binding=binding,
        )
        report["binding_file_sha256"] = binding_file_sha256
        print(stable_json(report))
        return 0
    except Exception as exc:
        print(
            stable_json(
                {
                    "contract_version": CONTRACT_VERSION,
                    "error_class": type(exc).__name__,
                    "error_sha256": sha256_bytes(str(exc).encode("utf-8")),
                    "provider_calls": 0,
                    "runtime_matches_manifest": False,
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
