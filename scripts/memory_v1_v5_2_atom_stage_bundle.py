#!/usr/bin/env python3
"""Build and probe one exact reviewed V5.2 atom-stage bundle without production writes."""

from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_v5_1_stage_preflight import (
    REQUEST_NAMESPACE,
    RESOLVER,
    RESOLVER_VERSION,
    V5_2_EXTRACTION_CONTRACT,
    V5_2_REGISTRY_VERSION,
    V5_2_RESOLUTION_CONTRACT,
    _candidates,
    _schema_hash,
    _validate_extraction_packet,
    _validate_resolution_packet,
    resolve_mention,
)
from scripts.memory_v1_v5_2_stage_batch import BUNDLE_CONTRACT, MANIFEST_CONTRACT


BUILD_MANIFEST_CONTRACT = "memory_v1_v5_2_atom_stage_build_manifest_v1"
PLAN_CONTRACT = "memory_v1_v5_2_atom_stage_plan_v1"
SOURCE_REPORT_CONTRACT = "memory_v1_v5_2_atom_stage_source_report_v1"
BUILD_MANIFEST_KEYS = {
    "contract_version",
    "target_server",
    "owner_user_id",
    "case_id",
    "apply_id",
    "proposal_id",
    "review_id",
    "packet_id",
    "evidence_id",
    "apply_manifest_sha256",
    "proposal_sha256",
    "stage_projection_sha256",
    "expected_counts",
}
PLAN_KEYS = {
    "contract_version",
    "policy_version",
    "owner_user_id",
    "apply_id",
    "proposal_id",
    "review_id",
    "packet_id",
    "evidence_id",
    "apply_manifest_sha256",
    "review_authorization_manifest_sha256",
    "proposal_sha256",
    "source_packet_storage_sha256",
    "source_validator_packet_sha256",
    "stage_projection_sha256",
    "stage_projection_text",
    "stage_projection",
    "mention_sha256_by_ref",
    "counts",
}
STAGE_COUNT_KEYS = {
    "entity_mentions",
    "observations",
    "comparison_hints",
    "deferrals",
}


class AtomStageError(RuntimeError):
    pass


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_valid(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def exact_object(value: Any, keys: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise AtomStageError(f"{field} fields differ from the locked contract")
    return value


def secure_read(path_value: str) -> tuple[Path, dict[str, Any]]:
    path = Path(path_value).resolve(strict=True)
    if not path.is_file():
        raise AtomStageError(f"input is not a regular file: {path}")
    return path, json.loads(path.read_text(encoding="utf-8"))


def exclusive_write(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o022:
        raise AtomStageError("output directory is writable by group or others")
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return sha256_bytes(payload)


def decode_jsonb(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise AtomStageError(f"{field} did not return a JSON object")
    return value


def repository_state() -> tuple[Path, str]:
    root = Path(__file__).resolve().parents[1]
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root, head


def load_build_manifest(path_value: str) -> tuple[Path, dict[str, Any]]:
    path, value = secure_read(path_value)
    exact_object(value, BUILD_MANIFEST_KEYS, "build manifest")
    if (
        value["contract_version"] != BUILD_MANIFEST_CONTRACT
        or value["target_server"] != "seebx"
    ):
        raise AtomStageError("build manifest contract or server is invalid")
    for key in (
        "owner_user_id",
        "apply_id",
        "proposal_id",
        "review_id",
        "packet_id",
        "evidence_id",
    ):
        uuid.UUID(str(value[key]))
    for key in (
        "apply_manifest_sha256",
        "proposal_sha256",
        "stage_projection_sha256",
    ):
        if not sha256_valid(value[key]):
            raise AtomStageError(f"build manifest {key} is invalid")
    counts = exact_object(
        value["expected_counts"], STAGE_COUNT_KEYS, "expected counts"
    )
    if any(
        not isinstance(counts[key], int)
        or isinstance(counts[key], bool)
        or counts[key] < 0
        for key in STAGE_COUNT_KEYS
    ):
        raise AtomStageError("build manifest counts are invalid")
    if not isinstance(value["case_id"], str) or not value["case_id"].strip():
        raise AtomStageError("build manifest case_id is invalid")
    return path, value


def validate_plan(plan: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    exact_object(plan, PLAN_KEYS, "stage plan")
    if (
        plan["contract_version"] != PLAN_CONTRACT
        or plan["policy_version"] != "memory_v1_v5_2_atom_stage_policy_v1"
    ):
        raise AtomStageError("stage plan contract is invalid")
    for key in (
        "owner_user_id",
        "apply_id",
        "proposal_id",
        "review_id",
        "packet_id",
        "evidence_id",
        "apply_manifest_sha256",
        "proposal_sha256",
        "stage_projection_sha256",
    ):
        if plan[key] != manifest[key]:
            raise AtomStageError(f"stage plan {key} differs from the build manifest")
    if plan["counts"] != manifest["expected_counts"]:
        raise AtomStageError("stage plan counts differ from the build manifest")
    projection = plan["stage_projection"]
    if not isinstance(projection, dict):
        raise AtomStageError("stage projection is not an object")
    projection_text = plan["stage_projection_text"]
    if (
        not isinstance(projection_text, str)
        or sha256_text(projection_text) != plan["stage_projection_sha256"]
        or json.loads(projection_text) != projection
    ):
        raise AtomStageError("stage projection hash mismatch")
    if projection.get("deferrals") != [] or projection.get("packet_findings") != []:
        raise AtomStageError("stage projection contains non-admitted material")
    mention_hashes = plan["mention_sha256_by_ref"]
    expected_refs = {item["entity_ref"] for item in projection["entity_mentions"]}
    if (
        not isinstance(mention_hashes, dict)
        or set(mention_hashes) != expected_refs
        or not all(sha256_valid(value) for value in mention_hashes.values())
    ):
        raise AtomStageError("stage plan mention hashes are invalid")
    _validate_extraction_packet(
        projection,
        extraction_contract=V5_2_EXTRACTION_CONTRACT,
        registry_version=V5_2_REGISTRY_VERSION,
    )
    return projection


async def build(args: argparse.Namespace) -> dict[str, Any]:
    repo, head = repository_state()
    build_manifest_path, manifest = load_build_manifest(args.manifest)
    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_root, 0o700)
    owner = uuid.UUID(manifest["owner_user_id"])
    apply_id = uuid.UUID(manifest["apply_id"])
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise AtomStageError("POSTGRES_DSN is required")

    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            if await conn.fetchval("SELECT session_user") != "brains_app":
                raise AtomStageError("POSTGRES_DSN must authenticate as brains_app")
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            raw_plan = await conn.fetchval(
                "SELECT memory.plan_owner_v5_2_atom_stage_v1($1::uuid)",
                apply_id,
            )
            plan = decode_jsonb(raw_plan, "stage planner")
            projection = validate_plan(plan, manifest)
            resolutions: list[dict[str, Any]] = []
            for mention in projection["entity_mentions"]:
                candidates = await _candidates(
                    conn, owner, mention, projection["observations"]
                )
                resolution = resolve_mention(
                    mention,
                    projection["observations"],
                    candidates,
                    projection["deferrals"],
                )
                if (
                    resolution["action"] != "link_existing"
                    or resolution["decision_state"] != "auto_link_eligible"
                    or resolution["review_reason_codes"]
                    != ["trusted_owner_self_binding"]
                ):
                    raise AtomStageError(
                        "reviewed projection does not resolve through the trusted self path"
                    )
                resolution["mention_sha256"] = plan["mention_sha256_by_ref"][
                    mention["entity_ref"]
                ]
                decision_body = {
                    key: value
                    for key, value in resolution.items()
                    if key != "decision_sha256"
                }
                resolution["decision_sha256"] = sha256_text(
                    stable_json(decision_body)
                )
                resolutions.append(resolution)
    finally:
        await conn.close()

    resolution_body = {
        "contract_version": V5_2_RESOLUTION_CONTRACT,
        "source_envelope": projection["source_envelope"],
        "predicate_registry_version": V5_2_REGISTRY_VERSION,
        "entity_normalization_version": "memory_entity_normalization_v5",
        "resolver": RESOLVER,
        "resolver_version": RESOLVER_VERSION,
        "resolutions": resolutions,
    }
    resolution = dict(resolution_body)
    resolution["packet_sha256"] = sha256_text(stable_json(resolution_body))
    _validate_resolution_packet(
        resolution,
        projection,
        resolution_contract=V5_2_RESOLUTION_CONTRACT,
        registry_version=V5_2_REGISTRY_VERSION,
    )

    extraction_text = plan["stage_projection_text"]
    resolution_text = stable_json(resolution)
    extraction_sha = sha256_text(extraction_text)
    resolution_sha = sha256_text(resolution_text)
    if extraction_sha != manifest["stage_projection_sha256"]:
        raise AtomStageError("canonical extraction differs from authorized projection")
    request_id = uuid.uuid5(
        REQUEST_NAMESPACE,
        "|".join(
            (
                str(owner),
                manifest["evidence_id"],
                manifest["apply_id"],
                extraction_sha,
                resolution_sha,
                RESOLVER_VERSION,
            )
        ),
    )

    source_report = {
        "contract_version": SOURCE_REPORT_CONTRACT,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "owner_scoped_zero_write_atom_stage_build",
        "target_server": "seebx",
        "owner_user_id": str(owner),
        "apply_id": manifest["apply_id"],
        "required_head_commit": head,
        "build_manifest_path": str(build_manifest_path),
        "build_manifest_sha256": sha256_file(build_manifest_path),
        "stage_plan": plan,
        "resolution_summary": {
            "auto_link_eligible": len(resolutions),
            "manual_review_required": 0,
            "deferred": 0,
            "rejected": 0,
        },
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }
    source_report_path = output_root / "source-report.json"
    source_report_sha = exclusive_write(source_report_path, source_report)

    extraction_schema = repo / "specs/memory_v1_relational_extraction_v5_2.schema.json"
    resolution_schema = (
        repo / "specs/memory_v1_entity_resolution_review_v5_2.schema.json"
    )
    bundle = {
        "contract_version": BUNDLE_CONTRACT,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "preflight_only_zero_write",
        "server": "seebx",
        "owner_user_id": str(owner),
        "case_id": manifest["case_id"],
        "evidence_id": manifest["evidence_id"],
        "request_id": str(request_id),
        "extractor": "memory_v1_v5_2_atom_stage_projection",
        "extractor_version": head,
        "source_report": {
            "path": str(source_report_path),
            "sha256": source_report_sha,
        },
        "schemas": {
            "extraction_sha256": _schema_hash(extraction_schema),
            "resolution_sha256": _schema_hash(resolution_schema),
        },
        "extraction_packet_text": extraction_text,
        "resolution_packet_text": resolution_text,
        "extraction_packet_sha256": extraction_sha,
        "resolution_packet_sha256": resolution_sha,
        "resolution_summary": {
            "auto_link_eligible": len(resolutions),
            "manual_review_required": 0,
            "deferred": 0,
            "rejected": 0,
        },
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "authorized_stage": False,
    }
    bundle_path = output_root / "bundle.json"
    bundle_sha = exclusive_write(bundle_path, bundle)
    counts = {
        "mentions": len(projection["entity_mentions"]),
        "resolutions": len(resolutions),
        "candidates": sum(len(item["candidate_set"]) for item in resolutions),
        "observations": len(projection["observations"]),
        "temporals": len(projection["observations"]),
    }
    stage_manifest = {
        "contract_version": MANIFEST_CONTRACT,
        "target_server": "seebx",
        "owner_user_id": str(owner),
        "bundles": [
            {
                "path": str(bundle_path),
                "sha256": bundle_sha,
                "expected_outcome": "applied",
                "expected_counts": counts,
            }
        ],
    }
    stage_manifest_path = output_root / "stage-manifest.json"
    stage_manifest_sha = exclusive_write(stage_manifest_path, stage_manifest)
    return {
        "contract_version": "memory_v1_v5_2_atom_stage_build_report_v1",
        "owner_user_id": str(owner),
        "apply_id": str(apply_id),
        "request_id": str(request_id),
        "source_report": str(source_report_path),
        "bundle": str(bundle_path),
        "bundle_sha256": bundle_sha,
        "stage_manifest": str(stage_manifest_path),
        "stage_manifest_sha256": stage_manifest_sha,
        "expected_counts": counts,
        "expected_new_rows": sum(counts.values()) + 2,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }


def load_secure_bundle(path_value: str) -> tuple[Path, dict[str, Any]]:
    path = Path(path_value).resolve(strict=True)
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise AtomStageError("probe bundle must be a mode-0600 file")
    value = json.loads(path.read_text(encoding="utf-8"))
    return path, value


async def expect_stage_rejection(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    bundle: dict[str, Any],
    extraction: dict[str, Any],
) -> str:
    transaction = conn.transaction()
    await transaction.start()
    try:
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        await conn.fetchrow(
            """
            SELECT *
            FROM memory.stage_relational_packet_v5_2(
              $1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8
            )
            """,
            uuid.uuid5(uuid.UUID(bundle["request_id"]), "guard-probe"),
            uuid.UUID(bundle["evidence_id"]),
            bundle["extractor"],
            bundle["extractor_version"],
            stable_json(extraction),
            bundle["resolution_packet_text"],
            sha256_text(stable_json(extraction)),
            bundle["resolution_packet_sha256"],
        )
    except asyncpg.PostgresError as exc:
        code = exc.sqlstate or "unknown"
        await transaction.rollback()
        return code
    await transaction.rollback()
    raise AtomStageError("tampered atom projection unexpectedly entered staging")


async def probe(args: argparse.Namespace) -> dict[str, Any]:
    _, bundle = load_secure_bundle(args.bundle)
    owner = uuid.UUID(bundle["owner_user_id"])
    other_owner = uuid.UUID(args.other_owner_user_id)
    extraction = json.loads(bundle["extraction_packet_text"])
    tampered = copy.deepcopy(extraction)
    tampered["observations"][0]["object"]["value"]["position"] += " [tampered]"
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise AtomStageError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        tamper_code = await expect_stage_rejection(
            conn, owner=owner, bundle=bundle, extraction=tampered
        )
        transaction = conn.transaction(isolation="repeatable_read", readonly=True)
        await transaction.start()
        try:
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)", str(other_owner)
            )
            await conn.fetchval(
                "SELECT memory.plan_owner_v5_2_atom_stage_v1($1::uuid)",
                uuid.UUID(args.apply_id),
            )
        except asyncpg.PostgresError as exc:
            cross_owner_code = exc.sqlstate or "unknown"
            await transaction.rollback()
        else:
            await transaction.rollback()
            raise AtomStageError("cross-owner atom stage plan unexpectedly resolved")
    finally:
        await conn.close()
    if tamper_code != "23514" or cross_owner_code != "P0002":
        raise AtomStageError(
            f"unexpected guard codes: tamper={tamper_code}, cross_owner={cross_owner_code}"
        )
    return {
        "contract_version": "memory_v1_v5_2_atom_stage_guard_probe_v1",
        "tampered_projection_rejection": tamper_code,
        "cross_owner_rejection": cross_owner_code,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--manifest", required=True)
    build_parser.add_argument("--output-root", required=True)
    probe_parser = subparsers.add_parser("probe")
    probe_parser.add_argument("--bundle", required=True)
    probe_parser.add_argument("--apply-id", required=True)
    probe_parser.add_argument("--other-owner-user-id", required=True)
    return parser.parse_args()


async def main() -> int:
    args = arguments()
    result = await (build(args) if args.command == "build" else probe(args))
    print(stable_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
