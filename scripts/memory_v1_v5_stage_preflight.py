#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "memory_v1_v5_stage_preflight_v1"
RESOLVER = "memory_v1_owner_exact_resolver"
RESOLVER_VERSION = "v5.1"
REQUEST_NAMESPACE = uuid.UUID("0f3cc8bb-167e-5f5d-91d7-d2f1889eed35")
SENSITIVE_LEVELS = {"medium", "high", "restricted"}
CORRECTION_CLASSES = {"correction"}
EXTRACTION_KEYS = {
    "contract_version", "source_envelope", "predicate_registry_version",
    "entity_mentions", "observations", "comparison_hints", "deferrals",
    "packet_findings",
}
SOURCE_KEYS = {
    "job_id", "source_system", "source_external_id", "source_sha256",
    "source_recorded_at",
}
MENTION_KEYS = {
    "entity_ref", "entity_type", "mention_kind", "name_text",
    "relationship_role", "source_spans", "extraction_confidence", "reason_codes",
}
OBSERVATION_KEYS = {
    "observation_ref", "subject_entity_ref", "predicate",
    "predicate_registry_status", "object", "polarity", "modality",
    "projection_class", "surface_policy", "temporal", "project_scope",
    "sensitivity", "extraction_confidence", "source_spans", "reason_codes",
}
TEMPORAL_KEYS = {
    "semantic", "shape", "basis", "source_form", "certainty", "precision",
    "instant", "calendar_range", "instant_range", "relative_offset",
    "recurrence", "anchored_to_source_time", "normalization_policy_version",
    "reason_codes",
}
RESOLUTION_PACKET_KEYS = {
    "contract_version", "source_envelope", "predicate_registry_version",
    "entity_normalization_version", "resolver", "resolver_version",
    "resolutions", "packet_sha256",
}
RESOLUTION_KEYS = {
    "entity_ref", "mention_sha256", "action", "decision_state",
    "selected_entity_id", "proposed_entity", "candidate_set_sha256",
    "candidate_set", "review_reason_codes", "decision_sha256",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build one owner/evidence-scoped V5 staging bundle without writes"
    )
    parser.add_argument("--report", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--extraction-schema",
        default="specs/memory_v1_relational_extraction_v5.schema.json",
    )
    parser.add_argument(
        "--resolution-schema",
        default="specs/memory_v1_entity_resolution_review_v5.schema.json",
    )
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def secure_write(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(payload).hexdigest()


def _schema_hash(schema_path: Path) -> str:
    schema_bytes = schema_path.read_bytes()
    schema = json.loads(schema_bytes)
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise RuntimeError("V5 schema draft mismatch")
    return hashlib.sha256(schema_bytes).hexdigest()


def _sha256_valid(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _spans_valid(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, dict)
        and set(item) == {"start", "end", "span_sha256"}
        and isinstance(item["start"], int)
        and isinstance(item["end"], int)
        and item["start"] >= 0
        and item["end"] > item["start"]
        and _sha256_valid(item["span_sha256"])
        for item in value
    )


def _validate_extraction_packet(value: dict[str, Any]) -> None:
    if set(value) != EXTRACTION_KEYS:
        raise RuntimeError("extraction packet fields mismatch")
    if value["contract_version"] != "memory_v1_relational_extraction_v5":
        raise RuntimeError("extraction contract version mismatch")
    if value["predicate_registry_version"] != "memory_predicate_registry_v5":
        raise RuntimeError("predicate registry version mismatch")
    source = value["source_envelope"]
    if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
        raise RuntimeError("source envelope fields mismatch")
    if source["source_system"] != "public.chat_log" or not _sha256_valid(
        source["source_sha256"]
    ):
        raise RuntimeError("source envelope is invalid")
    mentions = value["entity_mentions"]
    observations = value["observations"]
    if not isinstance(mentions, list) or len(mentions) > 24:
        raise RuntimeError("entity mention count is invalid")
    if not isinstance(observations, list) or len(observations) > 32:
        raise RuntimeError("observation count is invalid")
    refs: set[str] = set()
    for mention in mentions:
        if not isinstance(mention, dict) or set(mention) != MENTION_KEYS:
            raise RuntimeError("entity mention fields mismatch")
        ref = mention["entity_ref"]
        if ref in refs or not isinstance(ref, str):
            raise RuntimeError("entity reference is invalid or duplicated")
        refs.add(ref)
        if not _spans_valid(mention["source_spans"]):
            raise RuntimeError("entity mention spans are invalid")
    observation_refs: set[str] = set()
    for observation in observations:
        if not isinstance(observation, dict) or set(observation) != OBSERVATION_KEYS:
            raise RuntimeError("observation fields mismatch")
        if observation["observation_ref"] in observation_refs:
            raise RuntimeError("observation reference is duplicated")
        observation_refs.add(observation["observation_ref"])
        if observation["subject_entity_ref"] not in refs:
            raise RuntimeError("observation subject is unresolved")
        object_value = observation["object"]
        if object_value.get("kind") == "entity" and object_value.get("entity_ref") not in refs:
            raise RuntimeError("observation object is unresolved")
        if observation["predicate_registry_status"] != "governed":
            raise RuntimeError("observation predicate is not governed")
        if not isinstance(observation["temporal"], dict) or set(
            observation["temporal"]
        ) != TEMPORAL_KEYS:
            raise RuntimeError("observation temporal fields mismatch")
        if not _spans_valid(observation["source_spans"]):
            raise RuntimeError("observation spans are invalid")
    if not isinstance(value["deferrals"], list) or not isinstance(
        value["comparison_hints"], list
    ):
        raise RuntimeError("extraction packet lists are invalid")


def _validate_resolution_packet(
    value: dict[str, Any], extraction_packet: dict[str, Any]
) -> None:
    if set(value) != RESOLUTION_PACKET_KEYS:
        raise RuntimeError("resolution packet fields mismatch")
    if value["contract_version"] != "memory_v1_entity_resolution_review_v5":
        raise RuntimeError("resolution contract version mismatch")
    if value["source_envelope"] != extraction_packet["source_envelope"]:
        raise RuntimeError("resolution source envelope mismatch")
    if value["predicate_registry_version"] != "memory_predicate_registry_v5":
        raise RuntimeError("resolution registry version mismatch")
    if value["entity_normalization_version"] != "memory_entity_normalization_v5":
        raise RuntimeError("entity normalization version mismatch")
    resolutions = value["resolutions"]
    if not isinstance(resolutions, list) or len(resolutions) != len(
        extraction_packet["entity_mentions"]
    ):
        raise RuntimeError("resolution count mismatch")
    expected_refs = {item["entity_ref"] for item in extraction_packet["entity_mentions"]}
    actual_refs: set[str] = set()
    for resolution in resolutions:
        if not isinstance(resolution, dict) or set(resolution) != RESOLUTION_KEYS:
            raise RuntimeError("resolution fields mismatch")
        actual_refs.add(resolution["entity_ref"])
        if not all(
            _sha256_valid(resolution[key])
            for key in (
                "mention_sha256", "candidate_set_sha256", "decision_sha256"
            )
        ):
            raise RuntimeError("resolution hash is invalid")
        if not isinstance(resolution["candidate_set"], list) or len(
            resolution["candidate_set"]
        ) > 20:
            raise RuntimeError("resolution candidate set is invalid")
    if actual_refs != expected_refs or len(actual_refs) != len(resolutions):
        raise RuntimeError("resolution entity references mismatch")
    if not _sha256_valid(value["packet_sha256"]):
        raise RuntimeError("resolution packet hash is invalid")


def _source_spans(packet: dict[str, Any]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for mention in packet["entity_mentions"]:
        spans.extend(mention["source_spans"])
    for observation in packet["observations"]:
        spans.extend(observation["source_spans"])
    for deferral in packet["deferrals"]:
        spans.extend(deferral["source_spans"])
    return spans


def _verify_source(packet: dict[str, Any], evidence: dict[str, Any]) -> None:
    source = packet["source_envelope"]
    if evidence["status"] != "active":
        raise RuntimeError("evidence is not active")
    if evidence["source_system"] != "public.chat_log":
        raise RuntimeError("evidence source system is not public.chat_log")
    if evidence["external_id"] != source["source_external_id"]:
        raise RuntimeError("evidence external ID does not match packet")
    content = evidence["content"]
    if not isinstance(content, str):
        raise RuntimeError("evidence content is unavailable")
    content_sha = sha256_text(content)
    if content_sha != source["source_sha256"] or evidence["content_sha256"] != content_sha:
        raise RuntimeError("evidence content hash does not match packet")
    for span in _source_spans(packet):
        start, end = span["start"], span["end"]
        if start < 0 or end <= start or end > len(content):
            raise RuntimeError("packet source span is out of bounds")
        if sha256_text(content[start:end]) != span["span_sha256"]:
            raise RuntimeError("packet source span hash mismatch")


def _mention_observations(
    mention: dict[str, Any], observations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    entity_ref = mention["entity_ref"]
    return [
        item
        for item in observations
        if item["subject_entity_ref"] == entity_ref
        or (
            item["object"]["kind"] == "entity"
            and item["object"]["entity_ref"] == entity_ref
        )
    ]


def _candidate_payload(
    candidate: dict[str, Any], *, same_name_count: int, mention: dict[str, Any]
) -> dict[str, Any]:
    role_unverified = bool(mention.get("relationship_role"))
    return {
        "entity_id": str(candidate["entity_id"]),
        "entity_type": candidate["entity_type"],
        "features": {
            "active_status": True,
            "entity_type_match": True,
            "exact_canonical_name": bool(candidate["exact_canonical_name"]),
            "exact_alias": bool(candidate["exact_alias"]),
            "relationship_role_supported": False,
            "source_local_coreference": "source_local_coreference"
            in mention["reason_codes"],
            "graph_neighbor_supported": False,
            "conflicting_attribute_count": 0,
            "same_name_candidate_count": same_name_count,
        },
        "exclusion_reasons": ["relationship_role_unverified"]
        if role_unverified
        else [],
    }


def resolve_mention(
    mention: dict[str, Any],
    observations: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    mention_hash = sha256_text(stable_json(mention))
    relevant = _mention_observations(mention, observations)
    sensitive = any(item["sensitivity"] in SENSITIVE_LEVELS for item in relevant)
    corrective = any(
        item["projection_class"] in CORRECTION_CLASSES
        or item["modality"] == "corrective"
        for item in relevant
    )
    role_unverified = bool(mention.get("relationship_role"))
    candidate_set = [
        _candidate_payload(item, same_name_count=len(candidates), mention=mention)
        for item in sorted(candidates, key=lambda item: str(item["entity_id"]))
    ]
    candidate_hash = sha256_text(stable_json(candidate_set))
    action = "defer"
    state = "deferred"
    selected: str | None = None
    proposed: dict[str, Any] | None = None
    reasons: list[str] = []

    kind = mention["mention_kind"]
    entity_type = mention["entity_type"]
    name = mention.get("name_text")
    if kind == "self_reference":
        if entity_type == "self" and len(candidates) == 1:
            action = "link_existing"
            state = "auto_link_eligible"
            selected = str(candidates[0]["entity_id"])
            reasons = ["trusted_owner_self_binding"]
        else:
            reasons = ["trusted_owner_self_binding_unavailable"]
    elif kind in {"role_only", "anonymous"}:
        reasons = [f"{kind}_creation_blocked"]
    elif entity_type == "project":
        reasons = ["trusted_project_binding_required"]
    elif kind != "named" or not isinstance(name, str) or not name.strip():
        reasons = ["stable_named_identity_required"]
    elif len(candidates) > 1:
        reasons = ["same_name_owner_ambiguity"]
    elif len(candidates) == 1:
        action = "link_existing"
        selected = str(candidates[0]["entity_id"])
        if sensitive or corrective or role_unverified:
            state = "manual_review_required"
            reasons = sorted(
                reason
                for reason, required in (
                    ("sensitive_context_review", sensitive),
                    ("correction_target_review", corrective),
                    ("relationship_role_unverified", role_unverified),
                )
                if required
            )
        else:
            state = "auto_link_eligible"
            reasons = ["unique_exact_owner_match"]
    else:
        action = "create_new"
        state = "manual_review_required"
        proposed = {
            "entity_type": entity_type,
            "identity_state": "named",
            "canonical_name": name.strip(),
            "display_label": name.strip(),
            "creation_reason": "new_named_entity_no_exact_owner_match",
        }
        reasons = ["new_named_entity_requires_review"]

    decision = {
        "entity_ref": mention["entity_ref"],
        "mention_sha256": mention_hash,
        "action": action,
        "decision_state": state,
        "selected_entity_id": selected,
        "proposed_entity": proposed,
        "candidate_set_sha256": candidate_hash,
        "candidate_set": candidate_set,
        "review_reason_codes": reasons,
    }
    decision["decision_sha256"] = sha256_text(stable_json(decision))
    return decision


async def _normalize(conn: asyncpg.Connection, value: str) -> str:
    return str(
        await conn.fetchval(
            """
            SELECT btrim(lower(regexp_replace(
              public.unaccent($1), '[^[:alnum:]]+', ' ', 'g'
            )))
            """,
            value,
        )
    )


async def _candidates(
    conn: asyncpg.Connection, owner: uuid.UUID, mention: dict[str, Any]
) -> list[dict[str, Any]]:
    if mention["mention_kind"] == "self_reference":
        rows = await conn.fetch(
            """
            SELECT entity_id, entity_type, true AS exact_canonical_name,
                   false AS exact_alias
              FROM memory.entity
             WHERE owner_user_id=$1 AND entity_type='self' AND status='active'
             ORDER BY entity_id
            """,
            owner,
        )
        return [dict(row) for row in rows]
    name = mention.get("name_text")
    if mention["mention_kind"] != "named" or not isinstance(name, str) or not name.strip():
        return []
    normalized = await _normalize(conn, name)
    rows = await conn.fetch(
        """
        SELECT entity.entity_id, entity.entity_type,
               bool_or(entity.normalized_name=$3) AS exact_canonical_name,
               bool_or(alias.normalized_alias=$3) AS exact_alias
          FROM memory.entity AS entity
          LEFT JOIN memory.entity_alias AS alias
            ON alias.owner_user_id=entity.owner_user_id
           AND alias.entity_id=entity.entity_id
         WHERE entity.owner_user_id=$1
           AND entity.entity_type=$2
           AND entity.status='active'
           AND (entity.normalized_name=$3 OR alias.normalized_alias=$3)
         GROUP BY entity.entity_id, entity.entity_type
         ORDER BY entity.entity_id
        """,
        owner,
        mention["entity_type"],
        normalized,
    )
    return [dict(row) for row in rows]


def _load_case(report: dict[str, Any], case_id: str, owner: uuid.UUID) -> dict[str, Any]:
    if (
        report.get("store") is not False
        or report.get("mode") != "zero_write_relational_v5_specialized_live_evaluation"
    ):
        raise RuntimeError("source report is not a store=false zero-write evaluation")
    if report.get("owner_user_id") != str(owner):
        raise RuntimeError("source report owner does not match requested owner")
    proof = report.get("zero_write_proof") or {}
    if proof.get("passed") is not True:
        raise RuntimeError("source report zero-write proof did not pass")
    matches = [item for item in report.get("sources", []) if item.get("case_id") == case_id]
    if len(matches) != 1:
        raise RuntimeError("case ID does not select exactly one source")
    source = matches[0]
    if (source.get("evaluation") or {}).get("passed") is not True:
        raise RuntimeError("selected source did not pass evaluation")
    return source


async def main() -> int:
    import asyncpg

    args = arguments()
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    owner = uuid.UUID(args.owner_user_id)
    report_path = Path(args.report).resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    source = _load_case(report, args.case_id, owner)
    packet = source["packet"]
    extraction_schema_sha = _schema_hash(Path(args.extraction_schema).resolve())
    _validate_extraction_packet(packet)

    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SELECT set_config('app.user_id', $1, true)", str(owner))
            evidence_rows = await conn.fetch(
                """
                SELECT evidence_id, source_system, external_id, content,
                       content_sha256, status::text
                  FROM memory.evidence
                 WHERE owner_user_id=$1 AND source_system='public.chat_log'
                   AND external_id=$2 AND content_sha256=$3
                 ORDER BY evidence_id
                """,
                owner,
                packet["source_envelope"]["source_external_id"],
                packet["source_envelope"]["source_sha256"],
            )
            if len(evidence_rows) != 1:
                raise RuntimeError("packet does not resolve to exactly one owner-scoped evidence row")
            evidence = dict(evidence_rows[0])
            _verify_source(packet, evidence)
            resolutions = []
            for mention in packet["entity_mentions"]:
                resolutions.append(
                    resolve_mention(
                        mention,
                        packet["observations"],
                        await _candidates(conn, owner, mention),
                    )
                )
    finally:
        await conn.close()

    resolution_body = {
        "contract_version": "memory_v1_entity_resolution_review_v5",
        "source_envelope": packet["source_envelope"],
        "predicate_registry_version": "memory_predicate_registry_v5",
        "entity_normalization_version": "memory_entity_normalization_v5",
        "resolver": RESOLVER,
        "resolver_version": RESOLVER_VERSION,
        "resolutions": resolutions,
    }
    resolution_packet = dict(resolution_body)
    resolution_packet["packet_sha256"] = sha256_text(stable_json(resolution_body))
    resolution_schema_sha = _schema_hash(Path(args.resolution_schema).resolve())
    _validate_resolution_packet(resolution_packet, packet)
    extraction_text = stable_json(packet)
    resolution_text = stable_json(resolution_packet)
    extraction_sha = sha256_text(extraction_text)
    resolution_sha = sha256_text(resolution_text)
    evidence_id = str(evidence["evidence_id"])
    request_id = str(
        uuid.uuid5(
            REQUEST_NAMESPACE,
            "|".join((str(owner), evidence_id, extraction_sha, resolution_sha, RESOLVER_VERSION)),
        )
    )
    bundle = {
        "contract_version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "preflight_only_zero_write",
        "server": "seebx",
        "owner_user_id": str(owner),
        "case_id": args.case_id,
        "evidence_id": evidence_id,
        "request_id": request_id,
        "extractor": report["pipeline_version"],
        "extractor_version": report["evaluator_commit"],
        "source_report": {
            "path": str(report_path),
            "sha256": sha256_file(report_path),
        },
        "schemas": {
            "extraction_sha256": extraction_schema_sha,
            "resolution_sha256": resolution_schema_sha,
        },
        "extraction_packet_text": extraction_text,
        "resolution_packet_text": resolution_text,
        "extraction_packet_sha256": extraction_sha,
        "resolution_packet_sha256": resolution_sha,
        "resolution_summary": {
            state: sum(1 for item in resolutions if item["decision_state"] == state)
            for state in (
                "auto_link_eligible",
                "manual_review_required",
                "deferred",
                "rejected",
            )
        },
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "authorized_stage": False,
    }
    output_path = Path(args.output).resolve()
    output_sha = secure_write(output_path, bundle)
    print(
        stable_json(
            {
                "version": VERSION,
                "output": str(output_path),
                "sha256": output_sha,
                "case_id": args.case_id,
                "evidence_id": evidence_id,
                "resolution_summary": bundle["resolution_summary"],
                "database_writes": 0,
                "qdrant_writes": 0,
                "external_model_calls": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
