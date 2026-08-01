#!/usr/bin/env python3
"""Sanitize the repository's schema/migration surface without emitting SQL."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
from collections import Counter, defaultdict

from source_record_projection import project_source_record, sanitize_text


SCHEMA_VERSION = "repository-schema-source-inventory-v1"
CANONICALIZATION = "json-sort-keys-utf8-ensure-ascii-no-floats-lf-v1"
GIT = "/usr/bin/git"
MAX_BLOB = 64 * 1024 * 1024
COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def sanitize_value(value: object) -> object:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_value(item) for key, item in value.items()}
    return value


def sanitize_output(value: dict[str, object]) -> dict[str, object]:
    output = sanitize_value(value)
    if not isinstance(output, dict):
        raise RuntimeError("sanitized source inventory is malformed")
    for item in output["sources"]:
        item["unsafe_categories"] = sorted(set(item["unsafe_categories"]))
        item["declared_objects"] = sorted(set(item["declared_objects"]))
    output["sources"] = sorted(output["sources"], key=lambda item: item["path"])
    for item in output["runtime_paths"]:
        item["signals"] = sorted(set(item["signals"]))
        item["referenced_sql_paths"] = sorted(set(item["referenced_sql_paths"]))
    output["runtime_paths"] = sorted(output["runtime_paths"], key=lambda item: item["path"])
    for item in output["duplicate_hashes"]:
        item["paths"] = sorted(set(item["paths"]))
    output["duplicate_hashes"] = sorted(output["duplicate_hashes"], key=lambda item: item["sha256"])
    output["systemd_units"] = sorted(output["systemd_units"], key=lambda item: item["unit"])
    output["scheduled_jobs"] = sorted(output["scheduled_jobs"], key=lambda item: item["unit"])
    output["cron_jobs"] = sorted(output["cron_jobs"], key=lambda item: item["name"])
    return output


def canonical_git_inventory(source: str) -> tuple[dict[str, object], bytes, bytes]:
    refs_raw = run([GIT, "--no-optional-locks", "--no-pager", "-C", source, "for-each-ref", "--format=%(refname)%09%(objectname)%09%(objecttype)", "refs/heads", "refs/tags", "refs/remotes"])
    ref_records: list[tuple[bytes, bytes, bytes]] = []
    for line in refs_raw.splitlines():
        parts = line.split(b"\t")
        if len(parts) != 3 or not parts[0] or not COMMIT_RE.fullmatch(parts[1].decode("ascii", "strict")) or not parts[2]:
            raise RuntimeError("Git ref inventory is malformed")
        ref_records.append((parts[0], parts[1], parts[2]))
    refs = b"".join(name + b"\t" + oid + b"\t" + kind_name + b"\n" for name, oid, kind_name in sorted(ref_records))
    worktrees_raw = run([GIT, "--no-optional-locks", "--no-pager", "-C", source, "worktree", "list", "--porcelain", "-z"])
    records: list[dict[str, object]] = []
    current: dict[bytes, bytes | None] = {}

    def finish() -> None:
        nonlocal current
        if not current:
            return
        raw_path = current.get(b"worktree")
        raw_head = current.get(b"HEAD")
        if raw_path is None or raw_head is None:
            raise RuntimeError("Git worktree record is incomplete")
        path = os.fsdecode(raw_path)
        pure = pathlib.PurePosixPath(path)
        head = raw_head.decode("ascii", "strict")
        if not pure.is_absolute() or pure.as_posix() != path or ".." in pure.parts or not COMMIT_RE.fullmatch(head):
            raise RuntimeError("Git worktree identity is unsafe")
        record: dict[str, object] = {"HEAD": head, "worktree": path}
        branch = current.get(b"branch")
        states = int(branch is not None) + int(b"detached" in current) + int(b"bare" in current)
        if states != 1:
            raise RuntimeError("Git worktree state is malformed")
        if branch is not None:
            if not branch:
                raise RuntimeError("Git worktree branch is empty")
            record["branch"] = os.fsdecode(branch)
        if b"detached" in current:
            record["detached"] = True
        if b"bare" in current:
            record["bare"] = True
        for marker in (b"locked", b"prunable"):
            if marker in current:
                reason = current[marker]
                record[marker.decode("ascii")] = os.fsdecode(reason) if reason else True
        records.append(record)
        current = {}

    allowed = {b"worktree", b"HEAD", b"branch", b"detached", b"bare", b"locked", b"prunable"}
    for token in worktrees_raw.split(b"\0"):
        if not token:
            finish()
            continue
        key, separator, value = token.partition(b" ")
        if key == b"worktree" and current:
            finish()
        if key not in allowed or key in current:
            raise RuntimeError("Git worktree field is malformed")
        current[key] = value if separator else None
    finish()
    worktrees = b"".join((json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8") for record in sorted(records, key=lambda item: os.fsencode(str(item["worktree"]))))
    return {"ref_count": len(ref_records), "ref_sha256": digest(refs), "worktree_count": len(records), "worktree_sha256": digest(worktrees)}, refs, worktrees


def run(argv: list[str], timeout: int = 120) -> bytes:
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=timeout,
        env={"LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
    )
    if result.returncode != 0 or len(result.stdout) > 128 * 1024 * 1024 or len(result.stderr) > 1024 * 1024:
        raise RuntimeError("read-only Git command failed")
    return result.stdout


RUNTIME_SIGNALS = {
    "psql": re.compile(rb"(?:^|[^A-Za-z0-9_])psql(?:[^A-Za-z0-9_]|$)", re.I),
    "sql_path_reference": re.compile(rb"(?:ops/sql|tests|sql)/[A-Za-z0-9_./-]+\.sql", re.I),
    "inline_ddl": re.compile(rb"\b(?:CREATE|ALTER|DROP)\s+(?:TABLE|SCHEMA|VIEW|FUNCTION|TYPE|INDEX|POLICY|TRIGGER)\b", re.I),
    "schema_bootstrap": re.compile(rb"\b(?:bootstrap|schema[_ -]?install|migration)\b", re.I),
    "database_driver": re.compile(rb"\b(?:psycopg|asyncpg|sqlalchemy)\b", re.I),
}

SQL_REFERENCE = re.compile(rb"(?:ops/sql|tests|sql)/[A-Za-z0-9_./-]+\.sql", re.I)


def tree_blobs(source: str, commit: str) -> list[tuple[str, str, str]]:
    raw = run([GIT, "--no-optional-locks", "--no-pager", "-C", source, "ls-tree", "-r", "-z", commit])
    output: list[tuple[str, str, str]] = []
    for token in raw.split(b"\0"):
        if not token:
            continue
        metadata, separator, raw_path = token.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise RuntimeError("Git tree record is malformed")
        if fields[1] != b"blob":
            continue
        path = os.fsdecode(raw_path)
        pure = pathlib.PurePosixPath(path)
        if pure.is_absolute() or pure.as_posix() != path or ".." in pure.parts:
            raise RuntimeError("Git path is unsafe")
        output.append((path, fields[0].decode("ascii"), fields[2].decode("ascii")))
    return output


def read_blobs(source: str, entries: list[tuple[str, str, str]]) -> list[tuple[str, str, str, bytes]]:
    process = subprocess.Popen(
        [GIT, "--no-optional-locks", "--no-pager", "-C", source, "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=False,
        env={"LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
    )
    assert process.stdin is not None and process.stdout is not None
    output: list[tuple[str, str, str, bytes]] = []
    try:
        for path, mode, oid in entries:
            process.stdin.write((oid + "\n").encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline().decode("ascii", "strict").strip().split()
            if len(header) != 3 or header[1] != "blob":
                raise RuntimeError("Git cat-file header is malformed")
            size = int(header[2])
            if size < 0 or size > MAX_BLOB:
                raise RuntimeError("Git blob is outside the size boundary")
            payload = process.stdout.read(size)
            if len(payload) != size or process.stdout.read(1) != b"\n":
                raise RuntimeError("Git cat-file payload is malformed")
            output.append((path, mode, oid, payload))
    finally:
        process.stdin.close()
        returncode = process.wait(timeout=30)
        process.stdout.close()
    if returncode != 0:
        raise RuntimeError("Git cat-file inventory failed")
    return output


def systemd_inventory() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    root = pathlib.Path("/etc/systemd/system")
    output: list[dict[str, object]] = []
    payloads: dict[str, bytes] = {}
    for path in sorted(root.glob("*"), key=lambda item: os.fsencode(item.name)):
        try:
            observed = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(observed.st_mode) or path.is_symlink() or observed.st_size > 1024 * 1024:
            continue
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        if b"/opt/chat-memory" not in payload:
            continue
        payloads[path.name] = payload
        signals = sorted(
            token
            for token, pattern in {
                "migration": rb"\bmigrat",
                "schema": rb"\bschema",
                "projection": rb"\bprojection",
                "materialization": rb"\bmaterializ",
                "consolidation": rb"\bconsolidat",
                "database": rb"\b(?:psql|postgres|database)\b",
            }.items()
            if re.search(pattern, payload, re.I)
        )
        suffix = path.suffix.removeprefix(".")
        unit_type = suffix if suffix in {"service", "timer", "path", "socket", "target", "mount", "automount", "slice", "scope"} else "other"
        output.append({"unit": path.name, "unit_type": unit_type, "sha256": digest(payload), "size": len(payload), "signals": signals})
    scheduled: list[dict[str, object]] = []
    service_names = {name for name in payloads if name.endswith(".service")}
    for path in sorted(root.glob("*.timer"), key=lambda item: os.fsencode(item.name)):
        try:
            observed = path.lstat()
            if not stat.S_ISREG(observed.st_mode) or path.is_symlink() or observed.st_size > 1024 * 1024:
                continue
            payload = path.read_bytes()
        except OSError:
            continue
        text = payload.decode("utf-8", "replace")
        target = path.stem + ".service"
        schedule: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", ";")) or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key == "Unit":
                target = value.strip()
            if key in {"OnCalendar", "OnActiveSec", "OnBootSec", "OnStartupSec", "OnUnitActiveSec", "OnUnitInactiveSec", "RandomizedDelaySec", "Persistent", "AccuracySec", "FixedRandomDelay"}:
                schedule.append(key + "=" + value.strip())
        if target not in service_names:
            continue
        schedule_bytes = ("\n".join(sorted(schedule)) + "\n").encode("utf-8")
        scheduled.append({"unit": path.name, "activates": target, "sha256": digest(payload), "size": len(payload), "schedule_sha256": digest(schedule_bytes), "schedule_field_count": len(schedule)})
    return output, scheduled


def cron_inventory() -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    root = pathlib.Path("/etc/cron.d")
    if not root.is_dir():
        return output
    for path in sorted(root.iterdir(), key=lambda item: os.fsencode(item.name)):
        try:
            observed = path.lstat()
            if not stat.S_ISREG(observed.st_mode) or path.is_symlink() or observed.st_size > 1024 * 1024:
                continue
            payload = path.read_bytes()
        except OSError:
            continue
        if b"/opt/chat-memory" not in payload:
            continue
        output.append({"name": path.name, "sha256": digest(payload), "size": len(payload)})
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--expected-commit", required=True)
    args = parser.parse_args()
    source = pathlib.Path(args.source)
    if not source.is_absolute() or source.is_symlink() or pathlib.Path(os.path.realpath(source)) != source:
        raise RuntimeError("source path is not absolute and real")
    if not COMMIT_RE.fullmatch(args.expected_commit):
        raise RuntimeError("expected commit is malformed")
    observed_commit = run([GIT, "--no-optional-locks", "--no-pager", "-C", source.as_posix(), "rev-parse", "HEAD"]).strip().decode("ascii")
    if observed_commit != args.expected_commit:
        raise RuntimeError("production commit drifted")
    observed_tree = run([GIT, "--no-optional-locks", "--no-pager", "-C", source.as_posix(), "rev-parse", "HEAD^{tree}"]).strip().decode("ascii")
    git_inventory, opening_refs, opening_worktrees = canonical_git_inventory(source.as_posix())
    status = run([GIT, "--no-optional-locks", "--no-pager", "-C", source.as_posix(), "status", "--porcelain=v2", "-z", "--untracked-files=all"])
    if status:
        raise RuntimeError("production worktree is dirty")
    entries = tree_blobs(source.as_posix(), observed_commit)
    blobs = read_blobs(source.as_posix(), entries)
    sql_sources: list[dict[str, object]] = []
    runtime_paths: list[dict[str, object]] = []
    for path, mode, oid, payload in blobs:
        if path.lower().endswith(".sql"):
            sql_sources.append(project_source_record(path, mode, oid, payload))
            continue
        if b"\0" in payload:
            continue
        signals = sorted(name for name, pattern in RUNTIME_SIGNALS.items() if pattern.search(payload))
        if not signals:
            continue
        references = sorted(set(match.decode("ascii") for match in SQL_REFERENCE.findall(payload)))
        runtime_paths.append(
            {
                "path": path,
                "mode": mode,
                "git_blob": oid,
                "sha256": digest(payload),
                "size": len(payload),
                "signals": signals,
                "referenced_sql_paths": references,
            }
        )
    hashes: dict[str, list[str]] = defaultdict(list)
    for item in sql_sources:
        hashes[str(item["sha256"])].append(str(item["path"]))
    duplicates = [
        {"sha256": hash_value, "paths": sorted(paths)}
        for hash_value, paths in sorted(hashes.items())
        if len(paths) > 1
    ]
    kinds = Counter(str(item["kind"]) for item in sql_sources)
    lanes = Counter(str(item["lane"]) for item in sql_sources)
    closing_commit = run([GIT, "--no-optional-locks", "--no-pager", "-C", source.as_posix(), "rev-parse", "HEAD"]).strip().decode("ascii")
    closing_tree = run([GIT, "--no-optional-locks", "--no-pager", "-C", source.as_posix(), "rev-parse", "HEAD^{tree}"]).strip().decode("ascii")
    closing_git_inventory, closing_refs, closing_worktrees = canonical_git_inventory(source.as_posix())
    closing_status = run([GIT, "--no-optional-locks", "--no-pager", "-C", source.as_posix(), "status", "--porcelain=v2", "-z", "--untracked-files=all"])
    if closing_commit != observed_commit or closing_tree != observed_tree or closing_status or closing_git_inventory != git_inventory or closing_refs != opening_refs or closing_worktrees != opening_worktrees:
        raise RuntimeError("production drifted during source inventory")
    systemd_units, scheduled_jobs = systemd_inventory()
    cron_jobs = cron_inventory()
    output = {
        "schema_version": SCHEMA_VERSION,
        "canonicalization": CANONICALIZATION,
        "production_commit": observed_commit,
        "production_tree": observed_tree,
        "git_inventory": git_inventory,
        "production_clean": True,
        "tracked_blob_count": len(entries),
        "sources": sorted(sql_sources, key=lambda item: str(item["path"])),
        "runtime_paths": sorted(runtime_paths, key=lambda item: str(item["path"])),
        "systemd_units": systemd_units,
        "scheduled_jobs": scheduled_jobs,
        "cron_jobs": cron_jobs,
        "duplicate_hashes": duplicates,
        "summary": {
            "source_count": len(sql_sources),
            "runtime_path_count": len(runtime_paths),
            "scheduled_job_count": len(scheduled_jobs),
            "systemd_unit_count": len(systemd_units),
            "cron_job_count": len(cron_jobs),
            "source_kind_counts": dict(sorted(kinds.items())),
            "lane_counts": dict(sorted(lanes.items())),
        },
    }
    sys.stdout.buffer.write(canonical_bytes(sanitize_output(output)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("repository source inventory failed: " + type(error).__name__ + ": " + str(error)[:500], file=sys.stderr)
        raise SystemExit(2)
