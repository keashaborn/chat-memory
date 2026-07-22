#!/usr/bin/env python3
"""Fail-closed verifier for the schema-only Memory V1 V5.2 production install."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid


PLAN_CONTRACT = "memory_v1_v5_2_schema_install_plan_v1"
AUTH_CONTRACT = "memory_v1_v5_2_schema_install_authorization_v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ContractError(RuntimeError):
    pass


def run(args: list[str], *, input_text: str | None = None) -> str:
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise ContractError(
            f"command failed ({result.returncode}): {' '.join(args)}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"JSON root must be an object: {path}")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_time(value: object, name: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ContractError(f"{name} must be an ISO-8601 string")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name} is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{name} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def validate_manifest(manifest: dict, repo_root: Path) -> None:
    if manifest.get("contract_version") != PLAN_CONTRACT:
        raise ContractError("unexpected manifest contract_version")
    if manifest.get("production_authorized") is not False:
        raise ContractError("committed manifest must remain unauthorized")
    source = manifest.get("source")
    target = manifest.get("target")
    auth = manifest.get("authorization_policy")
    if not isinstance(source, dict) or not isinstance(target, dict) or not isinstance(auth, dict):
        raise ContractError("source, target, and authorization_policy must be objects")
    base = source.get("required_production_base_commit")
    if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ContractError("required production base commit is invalid")
    expected_target = {
        "server": "seebx",
        "postgres_container": "brains-postgres-1",
        "database": "memory",
        "maintenance_role": "sage",
        "application_role": "brains_app",
        "private_schema": "memory",
    }
    if target != expected_target:
        raise ContractError("target is not exact")
    if auth != {
        "required": True,
        "contract_version": AUTH_CONTRACT,
        "file_mode": "0600",
        "maximum_validity_minutes": 30,
        "bind_plan_sha256": True,
        "bind_exact_head_commit": True,
        "environment_gate": "MEMORY_V1_V5_2_SCHEMA_INSTALL=authorized",
    }:
        raise ContractError("authorization policy is not exact")
    if manifest.get("hard_stop") != "before_live_v5_2_extraction_staging_projection_or_retrieval":
        raise ContractError("hard stop is not exact")

    files = manifest.get("source_files")
    if not isinstance(files, list) or not files:
        raise ContractError("source_files must be a nonempty list")
    seen: set[str] = set()
    ordinals: dict[str, list[int]] = {}
    root = repo_root.resolve()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {
            "path", "sha256", "kind", "ordinal"
        }:
            raise ContractError("source file entries must have exact keys")
        relpath = entry["path"]
        expected_sha = entry["sha256"]
        kind = entry["kind"]
        ordinal = entry["ordinal"]
        if not isinstance(relpath, str) or not relpath or relpath in seen:
            raise ContractError(f"invalid or duplicate source path: {relpath!r}")
        seen.add(relpath)
        if not isinstance(kind, str) or not isinstance(ordinal, int) or ordinal < 1:
            raise ContractError(f"invalid source kind or ordinal: {relpath}")
        ordinals.setdefault(kind, []).append(ordinal)
        if not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha):
            raise ContractError(f"invalid source SHA-256: {relpath}")
        candidate = (repo_root / relpath).resolve()
        if root not in candidate.parents or not candidate.is_file():
            raise ContractError(f"source file is missing or escapes repository: {relpath}")
        if file_sha256(candidate) != expected_sha:
            raise ContractError(f"source file hash mismatch: {relpath}")
    for kind, values in ordinals.items():
        if sorted(values) != list(range(1, len(values) + 1)):
            raise ContractError(f"{kind} ordinals are not contiguous")

    head = run(["git", "-C", str(repo_root), "rev-parse", "HEAD"])
    run(["git", "-C", str(repo_root), "merge-base", "--is-ancestor", base, head])
    if run(["git", "-C", str(repo_root), "status", "--porcelain"]):
        raise ContractError("Git worktree must be clean")


def validate_authorization(
    authorization_path: Path,
    manifest_path: Path,
    repo_root: Path,
) -> dict:
    mode = stat.S_IMODE(authorization_path.stat().st_mode)
    if mode != 0o600:
        raise ContractError("authorization file mode must be 0600")
    if repo_root.resolve() in authorization_path.resolve().parents:
        raise ContractError("authorization file must be outside the Git worktree")
    authorization = load_json(authorization_path)
    expected_keys = {
        "contract_version", "authorization_id", "approved_by_user",
        "scope", "plan_sha256", "head_commit", "issued_at", "expires_at",
    }
    if set(authorization) != expected_keys:
        raise ContractError("authorization fields are not exact")
    if authorization["contract_version"] != AUTH_CONTRACT:
        raise ContractError("authorization contract mismatch")
    try:
        uuid.UUID(str(authorization["authorization_id"]))
    except ValueError as exc:
        raise ContractError("authorization_id is not a UUID") from exc
    if authorization["approved_by_user"] is not True:
        raise ContractError("approved_by_user must be true")
    if authorization["scope"] != "schema_only_zero_live_v5_2_data":
        raise ContractError("authorization scope mismatch")
    if authorization["plan_sha256"] != file_sha256(manifest_path):
        raise ContractError("authorization is not bound to this manifest")
    head = run(["git", "-C", str(repo_root), "rev-parse", "HEAD"])
    if authorization["head_commit"] != head:
        raise ContractError("authorization is not bound to the exact Git head")
    issued = parse_time(authorization["issued_at"], "issued_at")
    expires = parse_time(authorization["expires_at"], "expires_at")
    now = dt.datetime.now(dt.timezone.utc)
    if issued > now + dt.timedelta(minutes=1) or expires <= now:
        raise ContractError("authorization is not currently valid")
    if expires - issued > dt.timedelta(minutes=30):
        raise ContractError("authorization validity exceeds 30 minutes")
    return authorization


def database_scalar(sql: str) -> str:
    return run([
        "docker", "exec", "brains-postgres-1", "psql", "-X", "-A", "-t",
        "-v", "ON_ERROR_STOP=1", "-U", "sage", "-d", "memory", "-c", sql,
    ])


def production_preflight() -> dict:
    exact = database_scalar("""
      SELECT (
        current_setting('server_version_num')::integer/10000=16
        AND to_regrole('brains_app') IS NOT NULL
        AND to_regrole('memory_v5_writer') IS NOT NULL
        AND to_regrole('memory_v5_reader') IS NOT NULL
        AND to_regrole('memory_v5_local_inference_maintainer') IS NOT NULL
        AND to_regclass('memory.evidence') IS NOT NULL
        AND to_regclass('memory.observation') IS NOT NULL
        AND to_regclass('memory.entity_resolution_plan') IS NOT NULL
        AND to_regclass('memory.projection_plan') IS NOT NULL
        AND to_regprocedure('memory.require_v5_writer_context()') IS NOT NULL
        AND to_regprocedure('memory.require_v5_reader_context()') IS NOT NULL
        AND to_regprocedure('memory.apply_projection_v5(uuid,uuid,text,uuid,text)') IS NOT NULL
      )::integer
    """)
    absent = database_scalar("""
      SELECT (
        NOT EXISTS (
          SELECT 1 FROM memory.predicate_registry_version
          WHERE registry_version='memory_predicate_registry_v5_2'
        )
        AND to_regprocedure('memory.persist_owner_v5_2_local_packet_v1(uuid,uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,jsonb,boolean,integer)') IS NULL
        AND to_regprocedure('memory.read_governed_claims_v2(uuid[])') IS NULL
        AND to_regprocedure('memory.stage_relational_packet_v5_2(uuid,uuid,text,text,text,text,text,text)') IS NULL
        AND to_regclass('memory.entity_resolution_reconciliation_v5_2') IS NULL
        AND to_regprocedure('memory.stage_projection_plan_v5_2(uuid,text,text)') IS NULL
      )::integer
    """)
    if exact != "1" or absent != "1":
        raise ContractError("production prerequisites or required-absent V5.2 state failed")
    return {
        "contract_version": "memory_v1_v5_2_schema_install_preflight_v1",
        "database_prerequisites": "passed",
        "v5_2_schema_state": "absent",
        "database_writes": 0,
        "qdrant_writes": 0,
        "prompt_influence": 0,
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--list-kind")
    parser.add_argument("--production-preflight", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = arguments()
    repo_root = args.repo_root.resolve()
    manifest_path = args.manifest.resolve()
    manifest = load_json(manifest_path)
    validate_manifest(manifest, repo_root)
    if args.authorization:
        validate_authorization(args.authorization.resolve(), manifest_path, repo_root)
    if args.list_kind:
        for entry in sorted(
            (item for item in manifest["source_files"] if item["kind"] == args.list_kind),
            key=lambda item: item["ordinal"],
        ):
            print(entry["path"])
        return 0
    report = {"manifest": "passed", "production_preflight": "not_requested"}
    if args.production_preflight:
        if not args.authorization:
            raise ContractError("production preflight requires authorization")
        report.update(production_preflight())
    if args.output:
        args.output.write_text(
            json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(args.output, 0o600)
    else:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        raise SystemExit(1)
