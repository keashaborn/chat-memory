from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MANIFEST_CONTRACT = "memory_v1_predicate_runtime_profiles_v2"
MANIFEST_PATH = "specs/memory_v1_predicate_runtime_profiles_v2.json"
MANIFEST_SHA256 = (
    "ba5593ac6fdf3ee906d7f2bc8785186482e5e9c47ceb301d833a11bf7cbc3ce3"
)
PROFILE_NAMES = frozenset({"v5", "v5_1", "v5_2"})
REVIEW_ONLY_PROFILES = ("v5_2",)
PROFILE_KEYS = frozenset(
    {
        "contract_version",
        "lifecycle",
        "registry_artifact_sha256",
        "registry_canonical_sha256",
        "registry_path",
        "registry_version",
        "schema_artifact_sha256",
        "schema_path",
    }
)


@dataclass(frozen=True)
class PredicateRuntimeProfileV2:
    name: str
    lifecycle: str
    contract_version: str
    registry_version: str
    registry_path: Path
    registry_artifact_sha256: str
    registry_canonical_sha256: str
    schema_path: Path
    schema_artifact_sha256: str
    review_only: bool


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _bound_path(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value.startswith("specs/"):
        raise ValueError("runtime profile artifact path is invalid")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("runtime profile artifact path escapes repository")
    path = (root / relative).resolve()
    specs_root = (root / "specs").resolve()
    if path.parent != specs_root:
        raise ValueError("runtime profile artifact must be in specs")
    return path


def load_runtime_profile_v2(
    root: str | Path,
    profile_name: str,
) -> PredicateRuntimeProfileV2:
    root_path = Path(root).resolve()
    if profile_name not in PROFILE_NAMES:
        raise ValueError("predicate runtime profile is not allowlisted")
    manifest_path = (root_path / MANIFEST_PATH).resolve()
    raw_manifest = manifest_path.read_bytes()
    if _sha256_bytes(raw_manifest) != MANIFEST_SHA256:
        raise ValueError("predicate runtime profile manifest SHA-256 mismatch")
    manifest = json.loads(raw_manifest)
    if set(manifest) != {
        "contract_version",
        "profiles",
        "review_only_profiles",
        "scheduler_default_profile",
        "status",
        "target_scheduled_profile",
    }:
        raise ValueError("predicate runtime profile manifest shape changed")
    if (
        manifest["contract_version"] != MANIFEST_CONTRACT
        or manifest["status"] != "proposed"
        or manifest["scheduler_default_profile"] != "v5"
        or manifest["target_scheduled_profile"] != "v5_1"
        or set(manifest["profiles"]) != PROFILE_NAMES
        or tuple(manifest["review_only_profiles"]) != REVIEW_ONLY_PROFILES
    ):
        raise ValueError("predicate runtime profile manifest policy changed")
    value = manifest["profiles"][profile_name]
    if not isinstance(value, dict) or set(value) != PROFILE_KEYS:
        raise ValueError("predicate runtime profile shape changed")
    review_only = profile_name in REVIEW_ONLY_PROFILES
    if review_only != (value["lifecycle"] == "offline_review_only"):
        raise ValueError("review-only lifecycle binding changed")
    registry_path = _bound_path(root_path, value["registry_path"])
    schema_path = _bound_path(root_path, value["schema_path"])
    registry_raw = registry_path.read_bytes()
    schema_raw = schema_path.read_bytes()
    if _sha256_bytes(registry_raw) != value["registry_artifact_sha256"]:
        raise ValueError("predicate runtime registry SHA-256 mismatch")
    if _sha256_bytes(schema_raw) != value["schema_artifact_sha256"]:
        raise ValueError("predicate runtime schema SHA-256 mismatch")
    registry = json.loads(registry_raw)
    schema = json.loads(schema_raw)
    if _canonical_sha256(registry) != value["registry_canonical_sha256"]:
        raise ValueError("predicate runtime registry canonical hash mismatch")
    if (
        registry.get("registry_version") != value["registry_version"]
        or registry.get("contract_version") != value["contract_version"]
    ):
        raise ValueError("predicate runtime registry binding mismatch")
    if (
        schema.get("properties", {})
        .get("contract_version", {})
        .get("const")
        != value["contract_version"]
    ):
        raise ValueError("predicate runtime schema binding mismatch")
    if review_only and (
        registry.get("runtime_active") is not False
        or registry.get("status") != "proposed"
    ):
        raise ValueError("review-only predicate profile became active")
    return PredicateRuntimeProfileV2(
        name=profile_name,
        lifecycle=value["lifecycle"],
        contract_version=value["contract_version"],
        registry_version=value["registry_version"],
        registry_path=registry_path,
        registry_artifact_sha256=value["registry_artifact_sha256"],
        registry_canonical_sha256=value["registry_canonical_sha256"],
        schema_path=schema_path,
        schema_artifact_sha256=value["schema_artifact_sha256"],
        review_only=review_only,
    )


__all__ = [
    "PredicateRuntimeProfileV2",
    "load_runtime_profile_v2",
]
