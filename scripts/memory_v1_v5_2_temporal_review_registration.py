#!/usr/bin/env python3
"""Build and apply bounded reviewed-temporal registrations for atom planning."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any
import uuid

import asyncpg


SPEC_CONTRACT = "memory_v1_v5_2_temporal_review_spec_v1"
MANIFEST_CONTRACT = "memory_v1_v5_2_temporal_review_manifest_v1"
RESULT_CONTRACT = "memory_v1_v5_2_temporal_review_apply_result_v1"
CONFIRMATION = "REGISTER_REVIEWED_TEMPORAL_PROJECTIONS_ONLY"
NAMESPACE = uuid.UUID("64b603d1-b835-5bf0-9c37-e16fd9e9273b")
MAX_ITEMS = 16


class TemporalReviewError(RuntimeError):
    pass


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode())


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_valid(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def exact_object(value: Any, keys: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise TemporalReviewError(f"{field} fields differ from the contract")
    return value


def parse_uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise TemporalReviewError(f"{field} must be a UUID") from exc


def repository_state() -> tuple[Path, str]:
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout
    if status.strip():
        raise TemporalReviewError("temporal review requires a clean worktree")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    return root, head


def repository_input(path_value: str, root: Path) -> bytes:
    path = Path(path_value).resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(root):
        raise TemporalReviewError("temporal review spec must be in the repository")
    if stat.S_IMODE(path.stat().st_mode) & 0o022:
        raise TemporalReviewError("temporal review spec is writable by a peer")
    return path.read_bytes()


def secure_bundle(path_value: str, review_root: Path) -> tuple[Path, dict[str, Any]]:
    path = Path(path_value).resolve(strict=True)
    metadata = path.stat()
    if (
        not path.is_file()
        or not path.is_relative_to(review_root)
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise TemporalReviewError("stage bundle must be mode 0600 in review root")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TemporalReviewError("stage bundle is not an object")
    return path, value


def load_spec(path_value: str, root: Path) -> tuple[dict[str, Any], str]:
    raw = repository_input(path_value, root)
    value = exact_object(
        json.loads(raw),
        {
            "contract_version",
            "target_server",
            "owner_user_id",
            "review_root",
            "items",
        },
        "temporal review spec",
    )
    owner = parse_uuid(value["owner_user_id"], "owner_user_id")
    review_root = Path(value["review_root"]).resolve(strict=True)
    if (
        value["contract_version"] != SPEC_CONTRACT
        or value["target_server"] != "seebx"
        or not review_root.is_dir()
        or stat.S_IMODE(review_root.stat().st_mode) & 0o077
        or not isinstance(value["items"], list)
        or not 1 <= len(value["items"]) <= MAX_ITEMS
    ):
        raise TemporalReviewError("temporal review spec is invalid")
    keys = {
        "case_id",
        "packet_id",
        "evidence_id",
        "observation_ref",
        "stage_bundle_path",
        "stage_bundle_sha256",
        "review_report_sha256",
        "review_reason_codes",
    }
    seen: set[tuple[str, str]] = set()
    items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(value["items"]):
        item = exact_object(raw_item, keys, f"temporal review item {index}")
        packet = parse_uuid(item["packet_id"], f"items[{index}].packet_id")
        evidence = parse_uuid(item["evidence_id"], f"items[{index}].evidence_id")
        observation_ref = item["observation_ref"]
        reasons = item["review_reason_codes"]
        if (
            not isinstance(item["case_id"], str)
            or not item["case_id"]
            or not isinstance(observation_ref, str)
            or not observation_ref
            or (packet, observation_ref) in seen
            or not sha256_valid(item["stage_bundle_sha256"])
            or not sha256_valid(item["review_report_sha256"])
            or not isinstance(reasons, list)
            or not reasons
            or len(set(reasons)) != len(reasons)
            or any(not isinstance(reason, str) or not reason for reason in reasons)
        ):
            raise TemporalReviewError(f"temporal review item {index} is invalid")
        seen.add((packet, observation_ref))
        items.append(
            {
                **item,
                "packet_id": packet,
                "evidence_id": evidence,
            }
        )
    return {
        "owner_user_id": owner,
        "review_root": review_root,
        "items": items,
    }, sha256_bytes(raw)


def reviewed_temporal_from_bundle(
    expected: dict[str, Any],
    review_root: Path,
    owner: str,
) -> dict[str, Any]:
    path, bundle = secure_bundle(expected["stage_bundle_path"], review_root)
    if sha256_file(path) != expected["stage_bundle_sha256"]:
        raise TemporalReviewError("stage bundle hash differs from the spec")
    if (
        bundle.get("contract_version") != "memory_v1_v5_2_stage_preflight_v1"
        or bundle.get("owner_user_id") != owner
        or bundle.get("case_id") != f"local-packet-{expected['packet_id']}"
        or bundle.get("source_report", {}).get("sha256")
        != expected["review_report_sha256"]
        or bundle.get("database_writes") != 0
        or bundle.get("qdrant_writes") != 0
        or bundle.get("external_model_calls") != 0
        or bundle.get("authorized_stage") is not False
    ):
        raise TemporalReviewError("reviewed stage bundle contract is invalid")
    extraction_text = bundle.get("extraction_packet_text")
    if (
        not isinstance(extraction_text, str)
        or sha256_text(extraction_text) != bundle.get("extraction_packet_sha256")
    ):
        raise TemporalReviewError("reviewed extraction packet hash is invalid")
    extraction = json.loads(extraction_text)
    matches = [
        item
        for item in extraction.get("observations", [])
        if item.get("observation_ref") == expected["observation_ref"]
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("temporal"), dict):
        raise TemporalReviewError("reviewed temporal observation is unavailable")
    return matches[0]["temporal"]


def validate_temporal_transform(raw: dict[str, Any], reviewed: dict[str, Any]) -> None:
    expected_offset = {
        "direction": "past",
        "magnitude": 1.0,
        "unit": "year",
        "approximate": True,
        "anchor_source": "evidence_observed_at",
    }
    if (
        raw.get("basis") != "relative"
        or raw.get("semantic") != "occurrence"
        or raw.get("shape") != "instant"
        or raw.get("source_form") != "relative"
        or raw.get("relative_offset") != expected_offset
        or raw.get("anchored_to_source_time") is not False
        or "reported_relative_year" not in raw.get("reason_codes", [])
        or reviewed.get("anchored_to_source_time") is not True
    ):
        raise TemporalReviewError("temporal source or reviewed anchor is invalid")
    normalized = json.loads(json.dumps(reviewed))
    normalized["anchored_to_source_time"] = False
    normalized["reason_codes"] = [
        code
        for code in normalized.get("reason_codes", [])
        if code
        not in {
            "relative_year_anchored_to_source_time",
            "review_last_year_relative_source_anchor",
        }
    ]
    if normalized != raw:
        raise TemporalReviewError("reviewed temporal changed more than the anchor")


def deterministic_id(
    owner: str,
    packet: str,
    observation_ref: str,
    reviewed_sha: str,
    role: str,
) -> str:
    return str(
        uuid.uuid5(
            NAMESPACE,
            "|".join((owner, packet, observation_ref, reviewed_sha, role)),
        )
    )


def decode_jsonb(value: Any) -> dict[str, Any]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise TemporalReviewError("atom planner did not return an object")
    return decoded


async def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    root, head = repository_state()
    spec, spec_sha = load_spec(args.spec, root)
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise TemporalReviewError("POSTGRES_DSN is required")
    connection = await asyncpg.connect(dsn, command_timeout=60)
    items: list[dict[str, Any]] = []
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            if await connection.fetchval("SELECT session_user") != "brains_app":
                raise TemporalReviewError("temporal manifest requires brains_app")
            await connection.execute(
                "SELECT set_config('app.user_id',$1,true)", spec["owner_user_id"]
            )
            for expected in spec["items"]:
                plan = decode_jsonb(
                    await connection.fetchval(
                        "SELECT memory.plan_owner_v5_2_atom_admission_v2($1::uuid)",
                        uuid.UUID(expected["packet_id"]),
                    )
                )
                if (
                    plan.get("source_packet_id") != expected["packet_id"]
                    or plan.get("source_evidence_id") != expected["evidence_id"]
                    or not sha256_valid(plan.get("source_packet_storage_sha256"))
                ):
                    raise TemporalReviewError("atom plan binding differs from spec")
                raw_matches = [
                    item
                    for item in plan.get("stage_projection", {}).get(
                        "observations", []
                    )
                    if item.get("observation_ref") == expected["observation_ref"]
                ]
                if len(raw_matches) != 1:
                    raise TemporalReviewError(
                        "raw atom temporal observation is unavailable"
                    )
                raw_temporal = raw_matches[0]["temporal"]
                reviewed_temporal = reviewed_temporal_from_bundle(
                    expected, spec["review_root"], spec["owner_user_id"]
                )
                validate_temporal_transform(raw_temporal, reviewed_temporal)
                raw_sha = sha256_text(stable_json(raw_temporal))
                reviewed_sha = sha256_text(stable_json(reviewed_temporal))
                items.append(
                    {
                        "case_id": expected["case_id"],
                        "operation_id": deterministic_id(
                            spec["owner_user_id"],
                            expected["packet_id"],
                            expected["observation_ref"],
                            reviewed_sha,
                            "operation",
                        ),
                        "registration_id": deterministic_id(
                            spec["owner_user_id"],
                            expected["packet_id"],
                            expected["observation_ref"],
                            reviewed_sha,
                            "registration",
                        ),
                        "packet_id": expected["packet_id"],
                        "evidence_id": expected["evidence_id"],
                        "observation_ref": expected["observation_ref"],
                        "packet_storage_sha256": plan[
                            "source_packet_storage_sha256"
                        ],
                        "stage_bundle_sha256": expected["stage_bundle_sha256"],
                        "review_report_sha256": expected["review_report_sha256"],
                        "raw_temporal_sha256": raw_sha,
                        "reviewed_temporal": reviewed_temporal,
                        "reviewed_temporal_sha256": reviewed_sha,
                        "review_reason_codes": expected["review_reason_codes"],
                    }
                )
    finally:
        await connection.close()
    manifest: dict[str, Any] = {
        "contract_version": MANIFEST_CONTRACT,
        "target_server": "seebx",
        "owner_user_id": spec["owner_user_id"],
        "required_base_commit": head,
        "spec_sha256": spec_sha,
        "items": items,
        "expected_new_rows": len(items),
        "manifest_sha256": "",
    }
    manifest["manifest_sha256"] = sha256_text(
        stable_json(
            {
                key: value
                for key, value in manifest.items()
                if key != "manifest_sha256"
            }
        )
    )
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "contract_version": "memory_v1_v5_2_temporal_review_manifest_report_v1",
        "manifest_sha256": manifest["manifest_sha256"],
        "item_count": len(items),
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }


def load_manifest(path_value: str) -> dict[str, Any]:
    path = Path(path_value).resolve(strict=True)
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise TemporalReviewError("temporal manifest must be mode 0600")
    value = exact_object(
        json.loads(path.read_text()),
        {
            "contract_version",
            "target_server",
            "owner_user_id",
            "required_base_commit",
            "spec_sha256",
            "items",
            "expected_new_rows",
            "manifest_sha256",
        },
        "temporal manifest",
    )
    expected_sha = sha256_text(
        stable_json(
            {
                key: item
                for key, item in value.items()
                if key != "manifest_sha256"
            }
        )
    )
    if (
        value["contract_version"] != MANIFEST_CONTRACT
        or value["target_server"] != "seebx"
        or value["manifest_sha256"] != expected_sha
        or not isinstance(value["items"], list)
        or not 1 <= len(value["items"]) <= MAX_ITEMS
        or value["expected_new_rows"] != len(value["items"])
    ):
        raise TemporalReviewError("temporal manifest is invalid")
    return value


def verify_repository_base(required_base: str) -> str:
    root, head = repository_state()
    result = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", required_base, head],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise TemporalReviewError("temporal manifest base is not an ancestor")
    return head


async def run_manifest(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_manifest(args.manifest)
    head = verify_repository_base(manifest["required_base_commit"])
    if (
        args.mode == "apply"
        and (
            os.getenv("MEMORY_V1_V5_2_TEMPORAL_REVIEW_APPLY") != "authorized"
            or args.confirm != CONFIRMATION
        )
    ):
        raise TemporalReviewError("durable temporal review capability is absent")
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise TemporalReviewError("POSTGRES_DSN is required")
    connection = await asyncpg.connect(dsn, command_timeout=60)
    transaction = connection.transaction(isolation="serializable")
    results: list[dict[str, Any]] = []
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise TemporalReviewError("temporal apply requires brains_app")
        await transaction.start()
        await connection.execute(
            "SELECT set_config('app.user_id',$1,true)",
            manifest["owner_user_id"],
        )
        expected_outcome = "replayed" if args.mode == "replay" else "applied"
        for item in manifest["items"]:
            row = await connection.fetchrow(
                """
                SELECT * FROM memory.review_owner_v5_2_temporal_projection_v1(
                  $1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12::jsonb
                )
                """,
                uuid.UUID(item["operation_id"]),
                uuid.UUID(item["registration_id"]),
                uuid.UUID(item["packet_id"]),
                uuid.UUID(item["evidence_id"]),
                item["observation_ref"],
                item["packet_storage_sha256"],
                item["stage_bundle_sha256"],
                item["review_report_sha256"],
                item["raw_temporal_sha256"],
                stable_json(item["reviewed_temporal"]),
                item["reviewed_temporal_sha256"],
                stable_json(item["review_reason_codes"]),
            )
            if row["outcome"] != expected_outcome:
                raise TemporalReviewError("unexpected temporal registration outcome")
            results.append(
                {
                    "case_id": item["case_id"],
                    "registration_id": str(row["registration_id"]),
                    "outcome": row["outcome"],
                    "reviewed_temporal_sha256": row[
                        "reviewed_temporal_sha256"
                    ],
                }
            )
        if args.mode == "preflight":
            await transaction.rollback()
        else:
            await transaction.commit()
    except BaseException:
        if connection.is_in_transaction():
            await transaction.rollback()
        raise
    finally:
        await connection.close()
    return {
        "contract_version": RESULT_CONTRACT,
        "mode": args.mode,
        "head": head,
        "manifest_sha256": manifest["manifest_sha256"],
        "results": results,
        "persistent_writes": len(results) if args.mode == "apply" else 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--spec", required=True)
    manifest.add_argument("--output", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--mode", choices=("preflight", "apply", "replay"), required=True)
    run.add_argument("--manifest", required=True)
    run.add_argument("--confirm")
    args = parser.parse_args()
    result = (
        await build_manifest(args)
        if args.command == "manifest"
        else await run_manifest(args)
    )
    print(stable_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
