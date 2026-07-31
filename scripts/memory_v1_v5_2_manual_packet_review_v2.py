#!/usr/bin/env python3
"""Build a zero-write reviewed decision for the compiler-v11 Jerry packet."""

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


SPEC_CONTRACT = "memory_v1_v5_2_manual_packet_review_spec_v2"
REPORT_CONTRACT = "memory_v1_v5_2_manual_packet_review_report_v2"


class ManualPacketReviewV2Error(RuntimeError):
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
        raise ManualPacketReviewV2Error(f"{field} is not ISO-8601") from error
    if parsed.tzinfo is None:
        raise ManualPacketReviewV2Error(f"{field} lacks timezone")
    return parsed.astimezone(dt.timezone.utc)


def exact_object(value: Any, keys: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ManualPacketReviewV2Error(f"{field} fields differ from contract")
    return value


def decode_object(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ManualPacketReviewV2Error(f"{field} is not an object")
    return value


def repository_state() -> tuple[Path, str]:
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True, capture_output=True, text=True, env=env,
    ).stdout
    if status.strip():
        raise ManualPacketReviewV2Error("review requires clean worktree")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()
    return root, head


def load_spec(path_value: str, root: Path) -> tuple[dict[str, Any], str]:
    path = Path(path_value).resolve(strict=True)
    if (
        not path.is_file()
        or not path.is_relative_to(root)
        or stat.S_IMODE(path.stat().st_mode) & 0o022
    ):
        raise ManualPacketReviewV2Error("spec must be protected repository input")
    raw = path.read_bytes()
    spec = exact_object(
        json.loads(raw),
        {
            "contract_version", "target_server", "owner_user_id", "evidence_id",
            "evidence_content_sha256", "packet_id", "packet_storage_sha256",
            "policy_compiler_sha256", "route_event_id", "route_reason_code",
            "source_observed_at", "source_recorded_at", "thread_id",
            "prior_user_context_sha256", "prior_role_resolution_id",
            "prior_role_mention_id", "prior_duration_evidence_id",
            "prior_duration_observation_id", "expected_entity_refs",
            "expected_observation_refs", "expected_deferral_reason_codes",
        },
        "review spec",
    )
    if spec["contract_version"] != SPEC_CONTRACT or spec["target_server"] != "seebx":
        raise ManualPacketReviewV2Error("spec contract or server is invalid")
    for field in (
        "owner_user_id", "evidence_id", "packet_id", "route_event_id", "thread_id",
        "prior_role_resolution_id", "prior_role_mention_id",
        "prior_duration_evidence_id", "prior_duration_observation_id",
    ):
        try:
            spec[field] = str(uuid.UUID(str(spec[field])))
        except (TypeError, ValueError) as error:
            raise ManualPacketReviewV2Error(f"{field} is not UUID") from error
    for field in (
        "evidence_content_sha256", "packet_storage_sha256",
        "policy_compiler_sha256", "prior_user_context_sha256",
    ):
        if not valid_sha256(spec[field]):
            raise ManualPacketReviewV2Error(f"{field} is not SHA-256")
    parse_utc(spec["source_observed_at"], "source_observed_at")
    parse_utc(spec["source_recorded_at"], "source_recorded_at")
    if spec["expected_entity_refs"] != ["e00"]:
        raise ManualPacketReviewV2Error("entity references drifted")
    if spec["expected_observation_refs"] != ["o02", "o03"]:
        raise ManualPacketReviewV2Error("observation references drifted")
    if spec["expected_deferral_reason_codes"] != [
        "insufficient_evidence", "sensitive_manual_review", "unregistered_predicate"
    ]:
        raise ManualPacketReviewV2Error("deferral reasons drifted")
    return spec, sha256_bytes(raw)


def analyze_packet(
    packet: dict[str, Any], *, evidence_observed_at: dt.datetime,
    prior_user_text: str, prior_duration_literal: dict[str, Any],
) -> dict[str, Any]:
    mentions = {item.get("entity_ref"): item for item in packet.get("entity_mentions", [])}
    observations = {
        item.get("observation_ref"): item for item in packet.get("observations", [])
    }
    deferrals = packet.get("deferrals", [])
    deferral_reasons = sorted(item.get("reason_code") for item in deferrals)
    if set(mentions) != {"e00"} or set(observations) != {"o02", "o03"}:
        raise ManualPacketReviewV2Error("packet semantic shape drifted")
    if deferral_reasons != [
        "insufficient_evidence", "sensitive_manual_review", "unregistered_predicate"
    ]:
        raise ManualPacketReviewV2Error("packet deferral shape drifted")
    person = mentions["e00"]
    if (
        person.get("entity_type") != "person"
        or person.get("name_text") != "Jerry"
        or person.get("relationship_role") != "family:father"
        or "dad" not in prior_user_text.casefold()
    ):
        raise ManualPacketReviewV2Error("Jerry father coreference is not established")
    dementia = observations["o02"]
    duration = observations["o03"]
    for item in (dementia, duration):
        if (
            item.get("predicate") != "health.user_reported_observation"
            or item.get("subject_entity_ref") != "e00"
            or item.get("sensitivity") != "high"
            or item.get("surface_policy") != "explicit_recall_only"
            or item.get("modality") != "reported_observation"
            or item.get("polarity") != "affirmed"
            or item.get("temporal", {}).get("instant_range", {}).get("lower")
            != evidence_observed_at.isoformat().replace("+00:00", "Z")
        ):
            raise ManualPacketReviewV2Error("restricted health boundary drifted")
    if dementia.get("object", {}).get("value") != "severe dementia":
        raise ManualPacketReviewV2Error("dementia content drifted")
    duration_object = duration.get("object", {})
    if (
        duration_object.get("value") != "short-term memory lasts about three seconds"
        or duration_object.get("approximate") is not False
        or "approximate_reported_duration" not in duration.get("reason_codes", [])
        or duration.get("temporal", {}).get("certainty") != "bounded"
        or prior_duration_literal.get("value") != "Memory only lasts about 30 seconds or so."
    ):
        raise ManualPacketReviewV2Error("duration comparison boundary drifted")
    unregistered = next(
        item for item in deferrals if item["reason_code"] == "unregistered_predicate"
    )
    if (
        unregistered.get("memory_shape") != "supportive_context"
        or unregistered.get("sensitivity") != "medium"
        or unregistered.get("review_required") is not True
    ):
        raise ManualPacketReviewV2Error("care-setting deferral boundary drifted")
    return {
        "disposition": "approve_restricted_partial_stage",
        "entity_decisions": [{
            "entity_ref": "e00",
            "decision": "approve_named_role_resolution",
            "reason_codes": [
                "adjacent_user_context_resolves_father",
                "reconcile_prior_role_only_father",
            ],
        }],
        "observation_decisions": [
            {
                "observation_ref": "o02",
                "decision": "approve_restricted_user_report",
                "reason_codes": [
                    "third_party_health_user_report",
                    "high_sensitivity_explicit_recall_only",
                    "source_time_anchored",
                ],
            },
            {
                "observation_ref": "o03",
                "decision": "approve_restricted_time_indexed_user_report",
                "reason_codes": [
                    "approximate_duration_preserved_in_text_and_reason_code",
                    "retain_prior_time_indexed_estimate",
                    "do_not_overwrite_prior_report",
                    "high_sensitivity_explicit_recall_only",
                ],
            },
        ],
        "deferral_decisions": [
            {
                "reason_code": "unregistered_predicate",
                "decision": "continue_deferred",
                "reason_codes": [
                    "assisted_living_is_care_setting_not_unique_place",
                    "literal_care_setting_predicate_required",
                ],
            },
            {
                "reason_code": "insufficient_evidence",
                "decision": "terminal_no_direct_claim",
                "reason_codes": ["whole_turn_direct_claim_not_authorized"],
            },
            {
                "reason_code": "sensitive_manual_review",
                "decision": "satisfied_for_approved_health_observations",
                "reason_codes": ["explicit_review_preserves_surface_policy"],
            },
        ],
        "approved_observation_refs": ["o02", "o03"],
        "held_observation_refs": [],
        "care_setting_deferred": True,
    }


async def build_report(args: argparse.Namespace) -> dict[str, Any]:
    root, head = repository_state()
    spec, spec_sha = load_spec(args.spec, root)
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ManualPacketReviewV2Error("POSTGRES_DSN is required")
    owner = uuid.UUID(spec["owner_user_id"])
    connection = await asyncpg.connect(dsn, command_timeout=30)
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            if await connection.fetchval("SELECT session_user") != "brains_app":
                raise ManualPacketReviewV2Error("review requires brains_app")
            await connection.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            evidence = await connection.fetchrow(
                """SELECT content,content_sha256,observed_at,recorded_at,metadata
                   FROM memory.evidence WHERE owner_user_id=$1 AND evidence_id=$2""",
                owner, uuid.UUID(spec["evidence_id"]),
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
                raise ManualPacketReviewV2Error("owner-scoped evidence unavailable")
            if (
                plan.get("owner_user_id") != spec["owner_user_id"]
                or plan.get("evidence_id") != spec["evidence_id"]
                or plan.get("packet_id") != spec["packet_id"]
                or plan.get("packet_storage_sha256") != spec["packet_storage_sha256"]
                or plan.get("route_event_id") != spec["route_event_id"]
                or plan.get("route") != "manual_review_artifact_ready"
                or plan.get("route_reason_code") != spec["route_reason_code"]
                or plan.get("manual_review_required") is not True
                or plan.get("manual_review_count") != 1
                or plan.get("blocking_code_count") != 3
            ):
                raise ManualPacketReviewV2Error("authoritative packet binding drifted")
            if (
                evidence["content_sha256"] != spec["evidence_content_sha256"]
                or sha256_text(evidence["content"]) != spec["evidence_content_sha256"]
            ):
                raise ManualPacketReviewV2Error("evidence binding drifted")
            metadata = decode_object(evidence["metadata"], "evidence metadata")
            if metadata.get("thread_id") != spec["thread_id"]:
                raise ManualPacketReviewV2Error("thread binding drifted")
            prior_user = await connection.fetchrow(
                """SELECT text,created_at FROM public.chat_log
                   WHERE owner_user_id=$1 AND thread_id=$2
                     AND source='frontend/chat:user' AND created_at < $3
                   ORDER BY created_at DESC,id DESC LIMIT 1""",
                owner, uuid.UUID(spec["thread_id"]), evidence["observed_at"],
            )
            if prior_user is None or sha256_text(prior_user["text"]) != spec["prior_user_context_sha256"]:
                raise ManualPacketReviewV2Error("adjacent user antecedent drifted")
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
                raise ManualPacketReviewV2Error("prior father-role plan drifted")
            prior_duration = decode_object(plan.get("prior_duration"), "prior duration")
            if (
                prior_duration.get("observation_id") != spec["prior_duration_observation_id"]
                or prior_duration.get("evidence_id") != spec["prior_duration_evidence_id"]
            ):
                raise ManualPacketReviewV2Error("prior duration binding drifted")
            observed_at = evidence["observed_at"].astimezone(dt.timezone.utc)
            recorded_at = evidence["recorded_at"].astimezone(dt.timezone.utc)
            if (
                observed_at != parse_utc(spec["source_observed_at"], "source_observed_at")
                or recorded_at != parse_utc(spec["source_recorded_at"], "source_recorded_at")
            ):
                raise ManualPacketReviewV2Error("evidence times drifted")
            decisions = analyze_packet(
                decode_object(plan.get("normalized_packet"), "normalized packet"),
                evidence_observed_at=observed_at,
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
    payload = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if stat.S_IMODE(output.parent.stat().st_mode) & 0o077:
        raise ManualPacketReviewV2Error("output directory is not private")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload); stream.flush(); os.fsync(stream.fileno())
    return {
        "status": "reviewed", "report_path": str(output),
        "report_sha256": sha256_bytes(payload),
        "disposition": decisions["disposition"],
        "approved_observations": len(decisions["approved_observation_refs"]),
        "care_setting_deferred": decisions["care_setting_deferred"],
        "database_writes": 0, "qdrant_writes": 0,
        "model_calls": 0, "prompt_influence": 0,
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    try:
        print(stable_json(asyncio.run(build_report(arguments())))); return 0
    except ManualPacketReviewV2Error as error:
        reason = str(error).strip().lower().replace(" ", "_")
        print(stable_json({"status": "rejected", "reason_code": reason})); return 1
    except asyncpg.PostgresError as error:
        code = error.sqlstate or "unknown"
        print(stable_json({"status": "rejected", "reason_code": f"database_contract_error_{code}"})); return 1
    except (OSError, ValueError, subprocess.SubprocessError):
        print(stable_json({"status": "rejected", "reason_code": "local_contract_error"})); return 1


if __name__ == "__main__":
    raise SystemExit(main())
