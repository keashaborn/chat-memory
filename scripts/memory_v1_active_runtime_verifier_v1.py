#!/usr/bin/env python3
"""Content-free, fail-closed verifier for the governed Memory V1 runtime."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any


CONTRACT_VERSION = "memory_v1_active_runtime_verifier_v1"
MANIFEST_CONTRACT = "memory_v1_active_runtime_manifest_v1"
BINDING_CONTRACT = "memory_v1_active_runtime_release_binding_v1"
DEFAULT_BINDING = Path(
    "/etc/chat-memory/memory-v1-active-runtime-release-binding-v1.json"
)
MAX_JSON_BYTES = 65_536
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
    commit = run_fixed(["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD"])
    tree = run_fixed(["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD^{tree}"])
    status = run_fixed(
        [
            "/usr/bin/git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ]
    )
    required_git_oid(commit, label="repository commit")
    required_git_oid(tree, label="repository tree")
    if status:
        raise RuntimeVerificationError("repository tracked state is not clean")
    return {"commit": commit, "tree": tree, "tracked_clean": True}


def systemd_unit_state(unit: str) -> dict[str, str]:
    if not unit or "/" in unit or unit.startswith("."):
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


def absent_path(path: Path, *, label: str) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise RuntimeVerificationError(f"{label} cannot be inspected") from exc
    return False


def load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    value, _ = read_regular(path, label="runtime manifest")
    manifest = decode_json(value, label="runtime manifest")
    if manifest.get("contract_version") != MANIFEST_CONTRACT:
        raise RuntimeVerificationError("runtime manifest contract is invalid")
    if manifest.get("activation_state") != "candidate_only_inactive":
        raise RuntimeVerificationError("runtime manifest is not the inactive candidate policy")
    verification = manifest.get("verification")
    if not isinstance(verification, dict) or verification.get("contract_version") != CONTRACT_VERSION:
        raise RuntimeVerificationError("runtime verification policy is invalid")
    return manifest, sha256_bytes(value)


def probe_live(manifest: dict[str, Any], *, root: Path) -> dict[str, Any]:
    verification = manifest["verification"]
    units: dict[str, dict[str, str]] = {}
    installed_hashes: dict[str, str] = {}
    source_hashes: dict[str, str] = {}
    sentinels: dict[str, str] = {}
    for service in manifest.get("services", []):
        if not isinstance(service, dict):
            raise RuntimeVerificationError("runtime service declaration is invalid")
        for key, hash_key in (("unit", "source_sha256"), ("timer", "timer_source_sha256")):
            unit = service.get(key)
            if not isinstance(unit, str):
                raise RuntimeVerificationError("runtime unit declaration is invalid")
            expected = required_sha256(service.get(hash_key), label=f"{unit} source hash")
            observed = sha256_file(root / "ops/systemd" / unit, label=f"{unit} source")
            if observed != expected:
                raise RuntimeVerificationError(f"{unit} source hash changed")
            source_hashes[unit] = observed
            state = systemd_unit_state(unit)
            units[unit] = state
            if state["load_state"] == "loaded":
                fragment = Path(state["fragment_path"])
                if not fragment.is_absolute():
                    raise RuntimeVerificationError(f"{unit} fragment path is invalid")
                installed_hashes[unit] = sha256_file(
                    fragment,
                    label=f"{unit} installed fragment",
                    require_root_mode=0o644,
                )
        sentinel = Path(str(service.get("timer_enable_sentinel", "")))
        if not sentinel.is_absolute():
            raise RuntimeVerificationError("timer sentinel path is invalid")
        sentinels[str(sentinel)] = "absent" if absent_path(sentinel, label="timer sentinel") else "present"

    legacy: dict[str, dict[str, str]] = {}
    for item in manifest.get("legacy_exclusivity", []):
        if not isinstance(item, dict) or "required_activation_state" not in item:
            continue
        component = item.get("component")
        if not isinstance(component, str):
            raise RuntimeVerificationError("legacy unit declaration is invalid")
        legacy[component] = systemd_unit_state(component)

    config = Path(str(verification.get("runtime_config", "")))
    if not config.is_absolute():
        raise RuntimeVerificationError("runtime configuration path is invalid")
    config_state: dict[str, Any]
    if absent_path(config, label="runtime configuration"):
        config_state = {"state": "absent"}
    else:
        value, info = read_regular(
            config, label="runtime configuration", require_root_0600=True
        )
        config_state = {
            "sha256": sha256_bytes(value),
            "state": "root_owned_0600_regular_single_link",
            "size": info.st_size,
        }

    python_path = Path(str(verification.get("python_executable", "")))
    if not python_path.is_absolute():
        raise RuntimeVerificationError("Python executable path is invalid")
    resolved_python = python_path.resolve(strict=True)
    python_sha256 = sha256_file(resolved_python, label="Python executable")
    try:
        sdk_version = importlib.metadata.version("openai")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeVerificationError("OpenAI SDK package is absent") from exc

    return {
        "config": config_state,
        "installed_unit_sha256": installed_hashes,
        "legacy_units": legacy,
        "openai_sdk_version": sdk_version,
        "python_executable_sha256": python_sha256,
        "repository": git_identity(root),
        "sentinels": sentinels,
        "source_unit_sha256": source_hashes,
        "units": units,
    }


def load_binding(path: Path) -> tuple[dict[str, Any], str]:
    if path != DEFAULT_BINDING:
        raise RuntimeVerificationError("release binding path is not fixed")
    value, _ = read_regular(
        path, label="release binding", require_root_0600=True
    )
    binding = decode_json(value, label="release binding")
    allowed = {
        "contract_version",
        "manifest_sha256",
        "openai_sdk_version",
        "phase",
        "python_executable_sha256",
        "repository_commit",
        "repository_tree",
        "runtime_config_sha256",
        "unit_sha256",
    }
    if set(binding) != allowed or binding.get("contract_version") != BINDING_CONTRACT:
        raise RuntimeVerificationError("release binding fields or contract are invalid")
    if value != stable_json(binding).encode("utf-8"):
        raise RuntimeVerificationError("release binding is not canonical JSON")
    return binding, sha256_bytes(value)


def verify_snapshot(
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    manifest_sha256: str,
    phase: str,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policies = manifest["verification"].get("supported_phases")
    if not isinstance(policies, dict) or phase not in policies:
        raise RuntimeVerificationError("runtime verification phase is unsupported")
    policy = policies[phase]
    if not isinstance(policy, dict):
        raise RuntimeVerificationError("runtime verification phase policy is invalid")
    binding_required = policy.get("binding_required") is True
    if binding_required != (binding is not None):
        raise RuntimeVerificationError("runtime release binding disposition is invalid")

    if snapshot.get("config", {}).get("state") != policy.get("runtime_config_state"):
        raise RuntimeVerificationError("runtime configuration state does not match policy")
    if snapshot.get("openai_sdk_version") != manifest["verification"].get("openai_sdk_version"):
        raise RuntimeVerificationError("OpenAI SDK version does not match policy")
    if snapshot.get("repository", {}).get("tracked_clean") is not True:
        raise RuntimeVerificationError("repository is not clean")

    for service in manifest["services"]:
        for kind in ("unit", "timer"):
            unit = service[kind]
            state = snapshot.get("units", {}).get(unit)
            if not isinstance(state, dict):
                raise RuntimeVerificationError("runtime unit state is absent")
            prefix = "service" if kind == "unit" else "timer"
            for field in ("load_state", "active_state", "enabled_state"):
                if state.get(field) != policy.get(f"{prefix}_{field}"):
                    raise RuntimeVerificationError(f"{unit} {field} does not match policy")
        sentinel = service["timer_enable_sentinel"]
        if snapshot.get("sentinels", {}).get(sentinel) != policy.get("sentinel_state"):
            raise RuntimeVerificationError("timer sentinel state does not match policy")

    for item in manifest["legacy_exclusivity"]:
        if "required_activation_state" not in item:
            continue
        component = item["component"]
        state = snapshot.get("legacy_units", {}).get(component)
        if not isinstance(state, dict):
            raise RuntimeVerificationError("legacy runtime state is absent")
        if state.get("active_state") != item["required_activation_state"]:
            raise RuntimeVerificationError("legacy runtime is active")
        expected_enabled = item.get("required_enabled_state")
        if expected_enabled is not None and state.get("enabled_state") != expected_enabled:
            raise RuntimeVerificationError("legacy runtime enabled state is invalid")

    binding_sha256 = None
    if binding is not None:
        if binding.get("phase") != phase:
            raise RuntimeVerificationError("release binding phase is invalid")
        exact_pairs = (
            ("manifest_sha256", manifest_sha256),
            ("repository_commit", snapshot["repository"]["commit"]),
            ("repository_tree", snapshot["repository"]["tree"]),
            ("runtime_config_sha256", snapshot["config"].get("sha256")),
            ("python_executable_sha256", snapshot["python_executable_sha256"]),
            ("openai_sdk_version", snapshot["openai_sdk_version"]),
        )
        for field, observed in exact_pairs:
            if binding.get(field) != observed:
                raise RuntimeVerificationError(f"release binding {field} does not match")
        unit_sha256 = binding.get("unit_sha256")
        installed_unit_sha256 = snapshot.get("installed_unit_sha256")
        if installed_unit_sha256 != snapshot.get("source_unit_sha256"):
            raise RuntimeVerificationError("installed unit hashes differ from source")
        if unit_sha256 != installed_unit_sha256:
            raise RuntimeVerificationError("release binding unit hashes do not match")
        for field in (
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
        if not isinstance(unit_sha256, dict) or any(
            required_sha256(value, label="release binding unit hash") != value
            for value in unit_sha256.values()
        ):
            raise RuntimeVerificationError("release binding unit hash map is invalid")
        binding_sha256 = sha256_bytes(stable_json(binding).encode("utf-8"))

    return {
        "binding_sha256": binding_sha256,
        "contract_version": CONTRACT_VERSION,
        "legacy_exclusivity_verified": True,
        "manifest_sha256": manifest_sha256,
        "phase": phase,
        "provider_calls": 0,
        "repository_commit": snapshot["repository"]["commit"],
        "repository_tree": snapshot["repository"]["tree"],
        "runtime_matches_manifest": True,
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the content-free installed state for governed Memory V1."
    )
    parser.add_argument(
        "--phase", choices=("preactivation", "installed_inactive"), required=True
    )
    parser.add_argument("--binding", type=Path)
    return parser.parse_args()


def main() -> int:
    args = arguments()
    try:
        manifest, manifest_sha256 = load_manifest(DEFAULT_MANIFEST)
        snapshot = probe_live(manifest, root=ROOT)
        binding = None
        binding_file_sha256 = None
        if args.phase == "installed_inactive":
            if args.binding is None:
                raise RuntimeVerificationError("installed runtime requires a release binding")
            binding, binding_file_sha256 = load_binding(args.binding)
        elif args.binding is not None:
            raise RuntimeVerificationError("preactivation does not accept a release binding")
        report = verify_snapshot(
            manifest,
            snapshot,
            manifest_sha256=manifest_sha256,
            phase=args.phase,
            binding=binding,
        )
        if binding_file_sha256 is not None:
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
