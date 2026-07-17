#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path

from scripts.memory_v1_relational_extraction_v5_external_preflight import (
    EXPECTED_ZERO_EFFECTS,
    load_manifest,
    preflight,
)
from scripts.memory_v1_relational_extraction_v5_provider import canonical_json


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expect_error(callback, label: str) -> None:
    try:
        callback()
    except Exception:
        return
    raise AssertionError(f"{label} was accepted")


def temporary_manifest(value: dict) -> tuple[tempfile.TemporaryDirectory, Path]:
    directory = tempfile.TemporaryDirectory(
        prefix="memory-v1-external-preflight-"
    )
    path = Path(directory.name) / "manifest.json"
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return directory, path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    manifest_path = (
        repo_root
        / "ops"
        / "manifests"
        / "memory_v1_relational_extraction_v5_external_preflight_20260716.json"
    )
    manifest_sha256 = file_sha256(manifest_path)
    manifest, loaded_sha256 = load_manifest(
        manifest_path,
        manifest_sha256,
    )
    report = preflight(
        repo_root=repo_root,
        manifest=manifest,
        manifest_sha256=loaded_sha256,
    )
    if report["ready_for_authorization"] is not True:
        raise AssertionError("preflight is not ready for authorization")
    if report["external_call_authorized"] is not False:
        raise AssertionError("preflight authorized an external call")
    if report["effects"] != EXPECTED_ZERO_EFFECTS:
        raise AssertionError("preflight zero-effect proof changed")
    if report["sdk"]["openai_version"] != "2.6.1":
        raise AssertionError("preflight OpenAI SDK version changed")
    if report["sdk"]["pydantic_version"] != "2.12.3":
        raise AssertionError("preflight Pydantic version changed")
    if report["provider"]["store"] is not False:
        raise AssertionError("preflight request is stateful")
    if len(report["provider"]["request_sha256"]) != 64:
        raise AssertionError("preflight request fingerprint is invalid")
    encoded = canonical_json(report)
    for forbidden in (
        manifest["source"]["content"],
        manifest["source"]["job_id"],
        manifest["source"]["source_external_id"],
        manifest["source"]["content_sha256"],
        "owner_user_id",
        "vantage_id",
    ):
        if forbidden in encoded:
            raise AssertionError("preflight report leaked source/owner data")

    expect_error(
        lambda: load_manifest(manifest_path, "0" * 64),
        "manifest hash mismatch",
    )

    changed = copy.deepcopy(manifest)
    changed["external_call_authorized"] = True
    directory, path = temporary_manifest(changed)
    try:
        expect_error(
            lambda: load_manifest(path, file_sha256(path)),
            "preflight external authorization",
        )
    finally:
        directory.cleanup()

    changed = copy.deepcopy(manifest)
    changed["provider"]["store"] = True
    directory, path = temporary_manifest(changed)
    try:
        loaded, digest = load_manifest(path, file_sha256(path))
        expect_error(
            lambda: preflight(
                repo_root=repo_root,
                manifest=loaded,
                manifest_sha256=digest,
            ),
            "stateful provider request",
        )
    finally:
        directory.cleanup()

    changed = copy.deepcopy(manifest)
    changed["source"]["content"] = "Changed synthetic content."
    directory, path = temporary_manifest(changed)
    try:
        loaded, digest = load_manifest(path, file_sha256(path))
        expect_error(
            lambda: preflight(
                repo_root=repo_root,
                manifest=loaded,
                manifest_sha256=digest,
            ),
            "changed synthetic source",
        )
    finally:
        directory.cleanup()

    changed = copy.deepcopy(manifest)
    changed["artifacts"][0]["sha256"] = "0" * 64
    directory, path = temporary_manifest(changed)
    try:
        loaded, digest = load_manifest(path, file_sha256(path))
        expect_error(
            lambda: preflight(
                repo_root=repo_root,
                manifest=loaded,
                manifest_sha256=digest,
            ),
            "artifact hash mismatch",
        )
    finally:
        directory.cleanup()

    print("memory_v1_relational_extraction_v5_external_preflight_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
