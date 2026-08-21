#!/usr/bin/env python3
"""Fail closed when the active immutable backend release drifts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReleaseIntegrityError(RuntimeError):
    pass


def _fail(code: str) -> None:
    raise ReleaseIntegrityError(code)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return hashlib.sha256(raw).hexdigest()


def _content_manifest_sha256(root: Path, excluded: frozenset[str]) -> str:
    resolved_root = root.resolve(strict=True)
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        item = path.lstat()
        mode = f"{stat.S_IMODE(item.st_mode):04o}"
        if stat.S_ISDIR(item.st_mode) and not stat.S_ISLNK(item.st_mode):
            records.append({"mode": mode, "path": relative, "type": "directory"})
        elif stat.S_ISREG(item.st_mode) and not stat.S_ISLNK(item.st_mode):
            records.append({"bytes": item.st_size, "mode": mode, "path": relative, "sha256": _sha256(path), "type": "file"})
        elif stat.S_ISLNK(item.st_mode):
            target = os.readlink(path)
            target_path = Path(target)
            resolved_target = (path.parent / target_path).resolve(strict=False)
            if target_path.is_absolute() or not resolved_target.is_relative_to(resolved_root):
                _fail("release_symlink_escape")
            records.append({"path": relative, "target": target, "type": "symlink"})
        else:
            _fail("release_special_file")
    return _canonical_sha256(records)


def _verify_sealed_tree(root: Path, required_uid: int) -> None:
    paths = [root, *root.rglob("*")]
    for path in paths:
        item = path.lstat()
        if item.st_uid != required_uid:
            _fail("release_owner_drift")
        if stat.S_ISLNK(item.st_mode):
            continue
        if stat.S_IMODE(item.st_mode) & 0o222:
            _fail("release_writable_drift")


def _read_receipt(root: Path, role: str, commit: str) -> Mapping[str, Any]:
    path = root / ".lifeswitch-release.json"
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseIntegrityError("release_receipt_invalid") from error
    required = {"role", "commit", "content_sha256"}
    role_field = "tree" if role == "backend" else "archive_sha256"
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {*required, role_field}
        or receipt.get("role") != role
        or receipt.get("commit") != commit
        or not isinstance(receipt.get(role_field), str)
        or not SHA256_RE.fullmatch(str(receipt.get("content_sha256")))
        or not SHA256_RE.fullmatch(str(receipt.get(role_field)))
    ):
        _fail("release_receipt_invalid")
    if _content_manifest_sha256(root, frozenset({path.name})) != receipt["content_sha256"]:
        _fail("release_content_drift")
    return receipt


def verify_release_integrity(
    commit: str,
    *,
    release_root: Path = Path("/opt/lifeswitch/releases"),
    runtime_root: Path = Path("/opt/lifeswitch/runtimes"),
    dropin: Path = Path("/etc/systemd/system/brains.service.d/zz-immutable-release.conf"),
    required_uid: int = 0,
) -> Mapping[str, str]:
    if not COMMIT_RE.fullmatch(commit):
        _fail("release_commit_invalid")
    release = release_root / commit
    runtime = runtime_root / commit
    for root in (release, runtime):
        item = root.lstat()
        if not stat.S_ISDIR(item.st_mode) or stat.S_ISLNK(item.st_mode):
            _fail("release_root_invalid")
        _verify_sealed_tree(root, required_uid)
    release_receipt = _read_receipt(release, "backend", commit)
    runtime_receipt = _read_receipt(runtime, "backend_runtime", commit)
    dropin_item = dropin.lstat()
    if (
        not stat.S_ISREG(dropin_item.st_mode)
        or stat.S_ISLNK(dropin_item.st_mode)
        or dropin_item.st_uid != required_uid
        or stat.S_IMODE(dropin_item.st_mode) & 0o022
    ):
        _fail("release_dropin_invalid")
    raw = dropin.read_text(encoding="utf-8")
    required_lines = (
        f"WorkingDirectory=/opt/lifeswitch/releases/{commit}",
        f"ExecStart=/opt/lifeswitch/runtimes/{commit}/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8088 --app-dir /opt/lifeswitch/releases/{commit}",
    )
    if "@COMMIT@" in raw or any(line not in raw.splitlines() for line in required_lines):
        _fail("release_dropin_drift")
    return {
        "commit": commit,
        "release_content_sha256": str(release_receipt["content_sha256"]),
        "runtime_content_sha256": str(runtime_receipt["content_sha256"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    arguments = parser.parse_args()
    try:
        evidence = verify_release_integrity(arguments.commit)
    except (OSError, ReleaseIntegrityError) as error:
        print(f"release_integrity_alert code={error}")
        return 1
    print(
        "release_integrity_ok "
        f"commit={evidence['commit']} "
        f"release={evidence['release_content_sha256']} "
        f"runtime={evidence['runtime_content_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
