#!/usr/bin/env python3
"""Build one owner-scoped, zero-write review bundle from a local V5.1/V5.2 packet."""

from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as dt
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_relational_extraction_v5_provider import (
    NormalizedPacket,
    canonical_sha256,
)
from scripts.memory_v1_v5_1_stage_preflight import (
    REQUEST_NAMESPACE,
    RESOLVER,
    RESOLVER_VERSION,
    V5_1_EXTRACTION_CONTRACT,
    V5_1_REGISTRY_VERSION,
    V5_1_RESOLUTION_CONTRACT,
    V5_2_EXTRACTION_CONTRACT,
    V5_2_REGISTRY_VERSION,
    V5_2_RESOLUTION_CONTRACT,
    _candidates,
    _schema_hash,
    _validate_extraction_packet,
    _validate_resolution_packet,
    _verify_source,
    resolve_mention,
    sha256_text,
    stable_json,
)


REVIEW_CONTRACT = "memory_v1_v5_1_local_packet_review_v1"
BUNDLE_CONTRACT = "memory_v1_v5_1_stage_preflight_v1"
REVIEW_NAMESPACE = uuid.UUID("d9bbc38c-812a-53f9-bfcb-c2185db1f7ca")
V5_2_REVIEW_NAMESPACE = uuid.UUID("f2285012-c276-52c8-a919-452274ac8ca1")
DEFAULT_REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
CANONICAL_PET_SPECIES = frozenset(
    {"bird", "cat", "dog", "horse", "llama", "rabbit"}
)
EXPLICIT_CALENDAR_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
LAST_YEAR_RE = re.compile(r"\blast year\b", re.IGNORECASE)
HASH_FIELDS = (
    "evidence_content_sha256",
    "provider_model_sha256",
    "model_file_sha256",
    "runtime_revision_sha256",
    "policy_compiler_sha256",
    "provider_output_sha256",
    "validator_packet_sha256",
    "packet_storage_sha256",
    "evidence_authority_sha256",
)


class LocalPacketReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewProfile:
    name: str
    extraction_contract: str
    registry_version: str
    resolution_contract: str
    review_contract: str
    bundle_contract: str
    review_namespace: uuid.UUID
    extraction_schema: str
    resolution_schema: str
    extractor: str


REVIEW_PROFILES = {
    "v5_1": ReviewProfile(
        name="v5_1",
        extraction_contract=V5_1_EXTRACTION_CONTRACT,
        registry_version=V5_1_REGISTRY_VERSION,
        resolution_contract=V5_1_RESOLUTION_CONTRACT,
        review_contract=REVIEW_CONTRACT,
        bundle_contract=BUNDLE_CONTRACT,
        review_namespace=REVIEW_NAMESPACE,
        extraction_schema="specs/memory_v1_relational_extraction_v5_1.schema.json",
        resolution_schema="specs/memory_v1_entity_resolution_review_v5_1.schema.json",
        extractor="memory_v1_v5_1_local_packet_review",
    ),
    "v5_2": ReviewProfile(
        name="v5_2",
        extraction_contract=V5_2_EXTRACTION_CONTRACT,
        registry_version=V5_2_REGISTRY_VERSION,
        resolution_contract=V5_2_RESOLUTION_CONTRACT,
        review_contract="memory_v1_v5_2_local_packet_review_v1",
        bundle_contract="memory_v1_v5_2_stage_preflight_v1",
        review_namespace=V5_2_REVIEW_NAMESPACE,
        extraction_schema="specs/memory_v1_relational_extraction_v5_2.schema.json",
        resolution_schema="specs/memory_v1_entity_resolution_review_v5_2.schema.json",
        extractor="memory_v1_v5_2_local_packet_review",
    ),
}


def review_profile(name: str) -> ReviewProfile:
    try:
        return REVIEW_PROFILES[name]
    except KeyError as exc:
        raise LocalPacketReviewError("review contract profile is not allowlisted") from exc


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read one immutable local V5.1/V5.2 packet through the owner-scoped API and "
            "emit a deterministic, zero-write entity-resolution review bundle."
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--packet-id", required=True)
    parser.add_argument("--review-report", required=True)
    parser.add_argument("--stage-bundle", required=True)
    parser.add_argument(
        "--contract-profile", choices=sorted(REVIEW_PROFILES), default="v5_1"
    )
    parser.add_argument("--extraction-schema")
    parser.add_argument("--resolution-schema")
    parser.add_argument("--review-root", default=str(DEFAULT_REVIEW_ROOT))
    return parser.parse_args()


def _json_value(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise LocalPacketReviewError(f"{label} is not a JSON object")
    return value


def _sha256_valid(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _secure_root(path_value: str) -> Path:
    root = Path(path_value).resolve(strict=True)
    mode = stat.S_IMODE(root.stat().st_mode)
    if not root.is_dir() or mode & 0o022:
        raise LocalPacketReviewError(
            "review root must not be writable by group or peers"
        )
    return root


def _output_path(path_value: str, root: Path) -> Path:
    path = Path(path_value).resolve()
    if not path.is_relative_to(root):
        raise LocalPacketReviewError(f"output is outside the review root: {path}")
    if path.exists():
        raise LocalPacketReviewError(f"immutable output already exists: {path}")
    return path


def _secure_write(path: Path, value: dict[str, Any]) -> str:
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
    return hashlib.sha256(payload).hexdigest()


def _repository_state() -> tuple[Path, str]:
    root = Path(__file__).resolve().parents[1]
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise LocalPacketReviewError("local packet review requires a clean worktree")
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root, commit


def packet_quality_findings(packet: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sanitized blockers; never include literal values or source prose."""
    findings: list[dict[str, Any]] = []
    for observation in packet["observations"]:
        predicate = observation["predicate"]
        object_value = observation.get("object")
        canonical_species = (
            object_value.get("value")
            if isinstance(object_value, dict)
            and object_value.get("kind") == "literal"
            and object_value.get("datatype") == "text"
            else None
        )
        if (
            predicate == "pet.species"
            and canonical_species not in CANONICAL_PET_SPECIES
        ):
            findings.append(
                {
                    "code": "open_pet_species_domain_review_required",
                    "observation_ref": observation["observation_ref"],
                    "predicate": predicate,
                    "blocking": True,
                }
            )
        if observation["projection_class"] == "project_knowledge" and (
            observation["project_scope"].get("state") != "resolved"
        ):
            findings.append(
                {
                    "code": "project_scope_resolution_required",
                    "observation_ref": observation["observation_ref"],
                    "predicate": predicate,
                    "blocking": True,
                }
            )
    for code in sorted(set(packet["packet_findings"])):
        findings.append(
            {
                "code": f"extractor_finding_{code}",
                "observation_ref": None,
                "predicate": None,
                "blocking": True,
            }
        )
    for deferral in packet["deferrals"]:
        findings.append(
            {
                "code": f"extractor_deferral_{deferral['reason_code']}",
                "observation_ref": None,
                "predicate": None,
                "blocking": True,
            }
        )
    if packet["comparison_hints"]:
        findings.append(
            {
                "code": "comparison_hint_review_required",
                "observation_ref": None,
                "predicate": None,
                "blocking": True,
            }
        )
    return sorted(
        findings,
        key=lambda item: (
            item["code"], item["observation_ref"] or "", item["predicate"] or ""
        ),
    )


def normalize_review_packet(
    packet: dict[str, Any], evidence_content: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Repair only source-provable legacy compiler metadata; never invent facts."""
    value = copy.deepcopy(packet)
    transformations: list[dict[str, Any]] = []
    for observation in value["observations"]:
        temporal = observation["temporal"]
        if not (
            temporal["basis"] == "calendar"
            and temporal["source_form"] == "partial_absolute"
            and temporal["calendar_range"] is not None
            and temporal["anchored_to_source_time"] is False
        ):
            continue
        source_text_parts: list[str] = []
        source_hashes: list[str] = []
        for span in observation["source_spans"]:
            start, end = span["start"], span["end"]
            if not (
                isinstance(start, int)
                and isinstance(end, int)
                and 0 <= start < end <= len(evidence_content)
            ):
                raise LocalPacketReviewError("review temporal source span is invalid")
            source_text = evidence_content[start:end]
            source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            if source_hash != span["span_sha256"]:
                raise LocalPacketReviewError("review temporal source span hash mismatch")
            source_text_parts.append(source_text)
            source_hashes.append(source_hash)
        explicit_years = sorted(
            {int(item) for item in EXPLICIT_CALENDAR_YEAR_RE.findall(" ".join(source_text_parts))}
        )
        calendar_range = temporal["calendar_range"]
        lower = calendar_range.get("lower")
        if not isinstance(lower, str):
            continue
        try:
            lower_year = dt.date.fromisoformat(lower).year
        except ValueError as exc:
            raise LocalPacketReviewError("review calendar lower bound is invalid") from exc
        if len(explicit_years) != 1:
            continue
        if explicit_years[0] != lower_year:
            raise LocalPacketReviewError(
                "explicit source year differs from normalized calendar range"
            )
        temporal["source_form"] = "absolute"
        repair_code = "review_explicit_calendar_year_source_form"
        if repair_code not in temporal["reason_codes"]:
            if len(temporal["reason_codes"]) >= 10:
                raise LocalPacketReviewError("temporal reason-code budget is exhausted")
            temporal["reason_codes"].append(repair_code)
        transformations.append(
            {
                "code": repair_code,
                "observation_ref": observation["observation_ref"],
                "from_source_form": "partial_absolute",
                "to_source_form": "absolute",
                "source_span_sha256": sorted(source_hashes),
            }
        )
    for observation in value["observations"]:
        temporal = observation["temporal"]
        if not (
            temporal["basis"] == "calendar"
            and temporal["source_form"] == "partial_absolute"
            and temporal["calendar_range"] is not None
            and temporal["anchored_to_source_time"] is False
        ):
            continue
        source_parts = [
            evidence_content[span["start"] : span["end"]]
            for span in observation["source_spans"]
        ]
        if not LAST_YEAR_RE.search(" ".join(source_parts)):
            continue
        recorded_at = value.get("source_envelope", {}).get("source_recorded_at")
        lower = temporal["calendar_range"].get("lower")
        if not isinstance(recorded_at, str) or not isinstance(lower, str):
            raise LocalPacketReviewError("relative year source anchor is absent")
        try:
            recorded_year = dt.datetime.fromisoformat(
                recorded_at.replace("Z", "+00:00")
            ).year
            lower_year = dt.date.fromisoformat(lower).year
        except ValueError as exc:
            raise LocalPacketReviewError("relative year source anchor is invalid") from exc
        if lower_year != recorded_year - 1:
            raise LocalPacketReviewError(
                "relative year differs from source-recorded anchor"
            )
        temporal["anchored_to_source_time"] = True
        temporal["reason_codes"] = [
            code for code in temporal["reason_codes"] if code != "explicit_year"
        ]
        for code in (
            "relative_year_anchored_to_source_time",
            "review_last_year_source_anchor",
        ):
            if code not in temporal["reason_codes"]:
                temporal["reason_codes"].append(code)
        transformations.append(
            {
                "code": "review_last_year_source_anchor",
                "observation_ref": observation["observation_ref"],
                "from_anchored_to_source_time": False,
                "to_anchored_to_source_time": True,
            }
        )
    return value, transformations


def review_artifact_eligible(row: dict[str, Any]) -> bool:
    return bool(
        row["manual_review_required"]
        or row["entity_mention_count"]
        or row["observation_count"]
        or row["comparison_hint_count"]
    )


def model_call_provenance_valid(
    profile: ReviewProfile,
    local_model_calls: Any,
    external_model_calls: Any,
) -> bool:
    if external_model_calls != 0:
        return False
    if profile.name == "v5_2":
        return local_model_calls in {0, 1}
    return local_model_calls == 1


def _validate_row(
    row: dict[str, Any],
    packet: dict[str, Any],
    profile: ReviewProfile,
) -> None:
    if any(not _sha256_valid(row[field]) for field in HASH_FIELDS):
        raise LocalPacketReviewError("local packet provenance contains an invalid hash")
    if row["provider_id"] != "local_llama_cpp":
        raise LocalPacketReviewError("packet is not from the private local provider")
    if not model_call_provenance_valid(
        profile,
        row["local_model_calls"],
        row["external_model_calls"],
    ):
        raise LocalPacketReviewError("local packet model-call provenance is invalid")
    if row["storage_integrity_verified"] is not True:
        raise LocalPacketReviewError("local packet storage integrity was not verified")
    if canonical_sha256(packet) != row["validator_packet_sha256"]:
        raise LocalPacketReviewError("immutable normalized packet hash mismatch")
    if not review_artifact_eligible(row):
        raise LocalPacketReviewError(
            "local packet has no reviewable relational content"
        )
    if (
        row["job_status"] != "review_required"
        or row["job_route"] != "relational_extraction"
        or row["job_lease_present"]
        or row["job_error_present"]
    ):
        raise LocalPacketReviewError("local packet job is not review-ready")
    if row["evidence_status"] != "active":
        raise LocalPacketReviewError("local packet evidence is not active")
    if row["evidence_content_sha256"] != row["evidence_authority_sha256"]:
        raise LocalPacketReviewError("packet and evidence authority hashes differ")
    if row["exact_stage_batch_count"] or row["evidence_stage_batch_count"]:
        raise LocalPacketReviewError("evidence already has relational staging state")
    expected_counts = (
        len(packet["entity_mentions"]),
        len(packet["observations"]),
        len(packet["comparison_hints"]),
        len(packet["deferrals"]),
    )
    actual_counts = (
        row["entity_mention_count"],
        row["observation_count"],
        row["comparison_hint_count"],
        row["deferral_count"],
    )
    if actual_counts != expected_counts:
        raise LocalPacketReviewError("local packet row counts do not match payload")
    source = packet["source_envelope"]
    if source["job_id"] != str(row["job_id"]):
        raise LocalPacketReviewError("source envelope job ID is not packet-bound")


async def _build(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise LocalPacketReviewError("POSTGRES_DSN is required")
    owner = uuid.UUID(args.owner_user_id)
    packet_id = uuid.UUID(args.packet_id)
    profile = review_profile(args.contract_profile)
    repo_root, commit = _repository_state()
    extraction_schema = args.extraction_schema or profile.extraction_schema
    resolution_schema = args.resolution_schema or profile.resolution_schema

    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise LocalPacketReviewError("review requires a brains_app session")
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            packet_rows = await conn.fetch(
                "SELECT * FROM memory.read_owner_v5_local_packet_review_v1($1)",
                packet_id,
            )
            if len(packet_rows) != 1:
                raise LocalPacketReviewError("owner-scoped immutable packet not found")
            row = dict(packet_rows[0])
            packet = _json_value(row["normalized_packet"], "normalized packet")
            _validate_row(row, packet, profile)
            immutable = NormalizedPacket.model_validate(packet).model_dump(mode="json")
            if canonical_sha256(immutable) != row["validator_packet_sha256"]:
                raise LocalPacketReviewError("validated packet differs from immutable packet")
            evidence = {
                "status": row["evidence_status"],
                "source_system": row["evidence_source_system"],
                "external_id": row["evidence_external_id"],
                "content": row["evidence_content"],
                "content_sha256": row["evidence_authority_sha256"],
            }
            _verify_source(immutable, evidence)
            reviewed, transformations = normalize_review_packet(
                immutable, evidence["content"]
            )
            _validate_extraction_packet(
                reviewed,
                extraction_contract=profile.extraction_contract,
                registry_version=profile.registry_version,
            )
            validated = NormalizedPacket.model_validate(reviewed).model_dump(mode="json")
            resolutions = [
                resolve_mention(
                    mention,
                    validated["observations"],
                    await _candidates(
                        conn, owner, mention, validated["observations"]
                    ),
                    validated["deferrals"],
                )
                for mention in validated["entity_mentions"]
            ]
            txid_assigned = await conn.fetchval("SELECT txid_current_if_assigned()")
    finally:
        await conn.close()

    resolution_body = {
        "contract_version": profile.resolution_contract,
        "source_envelope": validated["source_envelope"],
        "predicate_registry_version": profile.registry_version,
        "entity_normalization_version": "memory_entity_normalization_v5",
        "resolver": RESOLVER,
        "resolver_version": RESOLVER_VERSION,
        "resolutions": resolutions,
    }
    resolution = dict(resolution_body)
    resolution["packet_sha256"] = sha256_text(stable_json(resolution_body))
    _validate_resolution_packet(
        resolution,
        validated,
        resolution_contract=profile.resolution_contract,
        registry_version=profile.registry_version,
    )
    extraction_text = stable_json(validated)
    resolution_text = stable_json(resolution)
    extraction_sha = sha256_text(extraction_text)
    resolution_sha = sha256_text(resolution_text)
    evidence_id = str(row["evidence_id"])
    request_id = str(
        uuid.uuid5(
            REQUEST_NAMESPACE,
            "|".join(
                (str(owner), evidence_id, extraction_sha, resolution_sha, RESOLVER_VERSION)
            ),
        )
    )
    review_id = str(
        uuid.uuid5(
            profile.review_namespace,
            "|".join(
                (
                    str(owner), str(packet_id), row["packet_storage_sha256"],
                    extraction_sha, resolution_sha,
                )
            ),
        )
    )
    resolution_summary = {
        state: sum(item["decision_state"] == state for item in resolutions)
        for state in (
            "auto_link_eligible", "manual_review_required", "deferred", "rejected"
        )
    }
    findings = packet_quality_findings(validated)
    blocking_codes = sorted({item["code"] for item in findings if item["blocking"]})
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    review = {
        "contract_version": profile.review_contract,
        "mode": "owner_scoped_local_packet_review_zero_write",
        "generated_at": now,
        "owner_user_id": str(owner),
        "review_id": review_id,
        "packet_id": str(packet_id),
        "operation_id": str(row["operation_id"]),
        "job_id": str(row["job_id"]),
        "evidence_id": evidence_id,
        "evidence_content_sha256": row["evidence_content_sha256"],
        "validator_packet_sha256": row["validator_packet_sha256"],
        "packet_storage_sha256": row["packet_storage_sha256"],
        "derived_packet_sha256": extraction_sha,
        "resolution_packet_sha256": resolution_sha,
        "provider_provenance": {
            field: row[field]
            for field in (
                "provider_id", "provider_version", "provider_model_sha256",
                "model_file_sha256", "runtime_revision_sha256",
                "policy_compiler_sha256", "provider_output_sha256",
            )
        },
        "repository_commit": commit,
        "deterministic_transformations": transformations,
        "resolution_summary": resolution_summary,
        "quality_findings": findings,
        "blocking_codes": blocking_codes,
        "review_disposition": "manual_review_required",
        "zero_write_proof": {
            "database_writes": 0,
            "qdrant_writes": 0,
            "external_model_calls": 0,
            "readonly_repeatable_read_transaction": True,
            "transaction_id_assigned": txid_assigned is not None,
        },
    }
    bundle = {
        "contract_version": profile.bundle_contract,
        "generated_at": now,
        "mode": "preflight_only_zero_write",
        "server": "seebx",
        "owner_user_id": str(owner),
        "case_id": f"local-packet-{packet_id}",
        "evidence_id": evidence_id,
        "request_id": request_id,
        "extractor": profile.extractor,
        "extractor_version": commit,
        "source_report": {},
        "schemas": {
            "extraction_sha256": _schema_hash(
                (repo_root / extraction_schema).resolve()
            ),
            "resolution_sha256": _schema_hash(
                (repo_root / resolution_schema).resolve()
            ),
        },
        "extraction_packet_text": extraction_text,
        "resolution_packet_text": resolution_text,
        "extraction_packet_sha256": extraction_sha,
        "resolution_packet_sha256": resolution_sha,
        "resolution_summary": resolution_summary,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "authorized_stage": False,
    }
    return review, bundle


async def main() -> int:
    args = arguments()
    profile = review_profile(args.contract_profile)
    root = _secure_root(args.review_root)
    report_path = _output_path(args.review_report, root)
    bundle_path = _output_path(args.stage_bundle, root)
    review, bundle = await _build(args)
    report_sha = _secure_write(report_path, review)
    try:
        bundle["source_report"] = {
            "path": str(report_path),
            "sha256": report_sha,
        }
        bundle_sha = _secure_write(bundle_path, bundle)
    except BaseException:
        report_path.unlink(missing_ok=True)
        raise
    print(
        stable_json(
            {
                "contract_version": profile.review_contract,
                "predicate_contract_profile": profile.name,
                "review_report": str(report_path),
                "review_report_sha256": report_sha,
                "stage_bundle": str(bundle_path),
                "stage_bundle_sha256": bundle_sha,
                "owner_user_id": review["owner_user_id"],
                "packet_id": review["packet_id"],
                "resolution_summary": review["resolution_summary"],
                "blocking_codes": review["blocking_codes"],
                "review_disposition": review["review_disposition"],
                "database_writes": 0,
                "qdrant_writes": 0,
                "external_model_calls": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
