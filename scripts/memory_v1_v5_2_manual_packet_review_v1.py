#!/usr/bin/env python3
"""Build a zero-write reviewed decision for one authoritative V5.2 packet."""

from __future__ import annotations

import argparse
import asyncio
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


SPEC_CONTRACT = "memory_v1_v5_2_manual_packet_review_spec_v1"
REPORT_CONTRACT = "memory_v1_v5_2_manual_packet_review_report_v1"


class ManualPacketReviewError(RuntimeError):
    pass


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def parse_utc(value: Any, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ManualPacketReviewError(f"{field} is not an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ManualPacketReviewError(f"{field} lacks a timezone")
    return parsed.astimezone(dt.timezone.utc)


def exact_object(value: Any, keys: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ManualPacketReviewError(f"{field} fields differ from the contract")
    return value


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
        raise ManualPacketReviewError("manual packet review requires a clean worktree")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    return root, head


def load_spec(path_value: str, root: Path) -> tuple[dict[str, Any], str]:
    path = Path(path_value).resolve(strict=True)
    if (
        not path.is_file()
        or not path.is_relative_to(root)
        or stat.S_IMODE(path.stat().st_mode) & 0o022
    ):
        raise ManualPacketReviewError("review spec must be protected repository input")
    raw = path.read_bytes()
    spec = exact_object(
        json.loads(raw),
        {
            "contract_version",
            "target_server",
            "owner_user_id",
            "evidence_id",
            "evidence_content_sha256",
            "packet_id",
            "packet_storage_sha256",
            "route_event_id",
            "route_reason_code",
            "source_observed_at",
            "source_recorded_at",
            "thread_id",
            "prior_user_context_sha256",
            "prior_role_resolution_id",
            "prior_role_mention_id",
            "prior_duration_evidence_id",
            "prior_duration_observation_id",
            "expected_entity_refs",
            "expected_observation_refs",
        },
        "review spec",
    )
    if spec["contract_version"] != SPEC_CONTRACT or spec["target_server"] != "seebx":
        raise ManualPacketReviewError("review spec contract or server is invalid")
    for field in (
        "owner_user_id",
        "evidence_id",
        "packet_id",
        "route_event_id",
        "thread_id",
        "prior_role_resolution_id",
        "prior_role_mention_id",
        "prior_duration_evidence_id",
        "prior_duration_observation_id",
    ):
        try:
            spec[field] = str(uuid.UUID(str(spec[field])))
        except (TypeError, ValueError) as error:
            raise ManualPacketReviewError(f"{field} is not a UUID") from error
    for field in (
        "evidence_content_sha256",
        "packet_storage_sha256",
        "prior_user_context_sha256",
    ):
        if not valid_sha256(spec[field]):
            raise ManualPacketReviewError(f"{field} is not a SHA-256")
    parse_utc(spec["source_observed_at"], "source_observed_at")
    parse_utc(spec["source_recorded_at"], "source_recorded_at")
    if spec["expected_entity_refs"] != ["e00", "e01"]:
        raise ManualPacketReviewError("entity reference set drifted")
    if spec["expected_observation_refs"] != ["o01", "o02", "o03"]:
        raise ManualPacketReviewError("observation reference set drifted")
    return spec, sha256_bytes(raw)


def decode_object(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ManualPacketReviewError(f"{field} is not an object")
    return value


def analyze_packet(
    packet: dict[str, Any],
    *,
    evidence_observed_at: dt.datetime,
    evidence_recorded_at: dt.datetime,
    prior_user_text: str,
    prior_duration_literal: dict[str, Any],
) -> dict[str, Any]:
    mentions = {item.get("entity_ref"): item for item in packet.get("entity_mentions", [])}
    observations = {
        item.get("observation_ref"): item for item in packet.get("observations", [])
    }
    if set(mentions) != {"e00", "e01"} or set(observations) != {"o01", "o02", "o03"}:
        raise ManualPacketReviewError("manual packet semantic shape drifted")
    person = mentions["e00"]
    place = mentions["e01"]
    residence = observations["o01"]
    dementia = observations["o02"]
    duration = observations["o03"]
    if (
        person.get("entity_type") != "person"
        or person.get("name_text") != "Jerry"
        or person.get("relationship_role") != "family:father"
        or "dad" not in prior_user_text.casefold()
    ):
        raise ManualPacketReviewError("Jerry father coreference is not established")
    if (
        place.get("entity_type") != "place"
        or place.get("name_text") != "assisted living"
        or place.get("mention_kind") != "named"
    ):
        raise ManualPacketReviewError("assisted-living mention shape drifted")
    if (
        residence.get("predicate") != "residence.lives_at"
        or residence.get("subject_entity_ref") != "e00"
        or residence.get("object", {}).get("entity_ref") != "e01"
    ):
        raise ManualPacketReviewError("residence observation shape drifted")
    for item in (dementia, duration):
        if (
            item.get("predicate") != "health.user_reported_observation"
            or item.get("subject_entity_ref") != "e00"
            or item.get("sensitivity") != "high"
            or item.get("surface_policy") != "explicit_recall_only"
            or item.get("modality") != "reported_observation"
        ):
            raise ManualPacketReviewError("third-party health boundary drifted")
    if dementia.get("object", {}).get("value") != "severe dementia":
        raise ManualPacketReviewError("dementia observation content drifted")
    duration_object = duration.get("object", {})
    if (
        duration_object.get("value") != "short-term memory lasts about three seconds"
        or duration_object.get("approximate") is not False
        or prior_duration_literal.get("value")
        != "Memory only lasts about 30 seconds or so."
    ):
        raise ManualPacketReviewError("duration comparison boundary drifted")
    lower = parse_utc(
        residence.get("temporal", {}).get("instant_range", {}).get("lower"),
        "residence temporal lower bound",
    )
    if lower != evidence_recorded_at or lower == evidence_observed_at:
        raise ManualPacketReviewError("expected reprocessing-time anchor defect is absent")
    return {
        "disposition": "corrected_split_required",
        "entity_decisions": [
            {
                "entity_ref": "e00",
                "decision": "approve_named_role_resolution",
                "reason_codes": [
                    "adjacent_user_context_resolves_father",
                    "reconcile_prior_role_only_father",
                ],
            },
            {
                "entity_ref": "e01",
                "decision": "hold_generic_place_identity",
                "reason_codes": [
                    "descriptive_place_is_not_unique_named_entity",
                    "residence_object_contract_required",
                ],
            },
        ],
        "observation_decisions": [
            {
                "observation_ref": "o01",
                "decision": "hold_for_object_and_temporal_correction",
                "reason_codes": [
                    "generic_place_identity_unresolved",
                    "use_evidence_observed_at_not_reprocessing_time",
                ],
            },
            {
                "observation_ref": "o02",
                "decision": "approve_restricted_user_report",
                "reason_codes": [
                    "third_party_health_user_report",
                    "high_sensitivity_explicit_recall_only",
                ],
            },
            {
                "observation_ref": "o03",
                "decision": "hold_for_approximation_and_temporal_comparison",
                "reason_codes": [
                    "approximate_literal_required",
                    "retain_prior_time_indexed_estimate",
                    "do_not_overwrite_prior_report",
                ],
            },
        ],
        "corrected_temporal_anchor": evidence_observed_at.isoformat().replace("+00:00", "Z"),
        "corrected_duration_approximate": True,
    }


async def build_report(args: argparse.Namespace) -> dict[str, Any]:
    root, head = repository_state()
    spec, spec_sha = load_spec(args.spec, root)
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ManualPacketReviewError("POSTGRES_DSN is required")
    owner = uuid.UUID(spec["owner_user_id"])
    connection = await asyncpg.connect(dsn, command_timeout=30)
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            if await connection.fetchval("SELECT session_user") != "brains_app":
                raise ManualPacketReviewError("review requires brains_app")
            await connection.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            evidence = await connection.fetchrow(
                """
                SELECT content,content_sha256,observed_at,recorded_at,metadata
                FROM memory.evidence
                WHERE owner_user_id=$1 AND evidence_id=$2
                """,
                owner,
                uuid.UUID(spec["evidence_id"]),
            )
            plan = decode_object(
                await connection.fetchval(
                    "SELECT memory.plan_owner_v5_2_manual_packet_review_v1($1,$2,$3)",
                    uuid.UUID(spec["packet_id"]),
                    uuid.UUID(spec["prior_role_resolution_id"]),
                    uuid.UUID(spec["prior_duration_observation_id"]),
                ),
                "manual packet review plan",
            )
            if evidence is None:
                raise ManualPacketReviewError("owner-scoped evidence is unavailable")
            if (
                plan.get("contract_version")
                != "memory_v1_v5_2_manual_packet_review_plan_v1"
                or plan.get("owner_user_id") != spec["owner_user_id"]
                or plan.get("evidence_id") != spec["evidence_id"]
                or plan.get("packet_id") != spec["packet_id"]
                or plan.get("packet_storage_sha256") != spec["packet_storage_sha256"]
                or plan.get("route_event_id") != spec["route_event_id"]
                or plan.get("route") != "manual_review_artifact_ready"
                or plan.get("route_reason_code") != spec["route_reason_code"]
                or plan.get("manual_review_required") is not True
                or plan.get("manual_review_count") != 2
                or plan.get("blocking_code_count") != 2
            ):
                raise ManualPacketReviewError("authoritative packet binding drifted")
            if (
                evidence["content_sha256"] != spec["evidence_content_sha256"]
                or sha256_text(evidence["content"]) != spec["evidence_content_sha256"]
            ):
                raise ManualPacketReviewError("evidence content binding drifted")
            metadata = decode_object(evidence["metadata"], "evidence metadata")
            if metadata.get("thread_id") != spec["thread_id"]:
                raise ManualPacketReviewError("evidence thread binding drifted")
            prior_user = await connection.fetchrow(
                """
                SELECT text,created_at
                FROM public.chat_log
                WHERE owner_user_id=$1 AND thread_id=$2
                  AND source='frontend/chat:user' AND created_at < $3
                ORDER BY created_at DESC LIMIT 1
                """,
                owner,
                uuid.UUID(spec["thread_id"]),
                evidence["observed_at"],
            )
            if (
                prior_user is None
                or sha256_text(prior_user["text"]) != spec["prior_user_context_sha256"]
            ):
                raise ManualPacketReviewError("adjacent user antecedent drifted")
            prior_role = decode_object(plan.get("prior_role"), "prior role")
            if (
                prior_role.get("resolution_id") != spec["prior_role_resolution_id"]
                or prior_role.get("mention_id") != spec["prior_role_mention_id"]
                or prior_role.get("action") != "defer"
                or prior_role.get("decision_state") != "deferred"
                or prior_role.get("relationship_role") != "family:father"
                or prior_role.get("mention_kind") != "role_only"
                or prior_role.get("name_text") is not None
            ):
                raise ManualPacketReviewError("prior father role plan drifted")
            prior_duration = decode_object(plan.get("prior_duration"), "prior duration")
            if (
                prior_duration.get("observation_id")
                != spec["prior_duration_observation_id"]
                or prior_duration.get("evidence_id")
                != spec["prior_duration_evidence_id"]
            ):
                raise ManualPacketReviewError("prior duration observation drifted")
            observed_at = evidence["observed_at"].astimezone(dt.timezone.utc)
            recorded_at = evidence["recorded_at"].astimezone(dt.timezone.utc)
            if (
                observed_at != parse_utc(spec["source_observed_at"], "source_observed_at")
                or recorded_at != parse_utc(spec["source_recorded_at"], "source_recorded_at")
            ):
                raise ManualPacketReviewError("evidence times drifted")
            decisions = analyze_packet(
                decode_object(plan.get("normalized_packet"), "normalized packet"),
                evidence_observed_at=observed_at,
                evidence_recorded_at=recorded_at,
                prior_user_text=prior_user["text"],
                prior_duration_literal=decode_object(
                    prior_duration.get("object_literal"), "prior duration literal"
                ),
            )
    finally:
        await connection.close()
    report = {
        "contract_version": REPORT_CONTRACT,
        "target_server": "seebx",
        "repository_commit": head,
        "spec_sha256": spec_sha,
        "owner_user_id": spec["owner_user_id"],
        "evidence_id": spec["evidence_id"],
        "packet_id": spec["packet_id"],
        "packet_storage_sha256": spec["packet_storage_sha256"],
        "route_event_id": spec["route_event_id"],
        "review": decisions,
        "prior_role_resolution_id": spec["prior_role_resolution_id"],
        "prior_duration_observation_id": spec["prior_duration_observation_id"],
        "database_writes": 0,
        "qdrant_writes": 0,
        "model_calls": 0,
        "prompt_influence": 0,
    }
    payload = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if stat.S_IMODE(output.parent.stat().st_mode) & 0o077:
        raise ManualPacketReviewError("output directory is not private")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "status": "reviewed",
        "report_path": str(output),
        "report_sha256": sha256_bytes(payload),
        "disposition": decisions["disposition"],
        "database_writes": 0,
        "qdrant_writes": 0,
        "model_calls": 0,
        "prompt_influence": 0,
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    try:
        print(stable_json(asyncio.run(build_report(arguments()))))
        return 0
    except ManualPacketReviewError as error:
        reason = str(error).strip().lower().replace(" ", "_")
        print(stable_json({"status": "rejected", "reason_code": reason}))
        return 1
    except asyncpg.PostgresError as error:
        code = error.sqlstate or "unknown"
        category = type(error).__name__.replace("Error", "").lower()
        print(stable_json({
            "status": "rejected",
            "reason_code": f"database_contract_error_{code}_{category}",
        }))
        return 1
    except (OSError, ValueError):
        print(stable_json({"status": "rejected", "reason_code": "local_contract_error"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
