from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence


REVIEW_VERSION = "memory_v1_specialized_review_20260713_v1"
SOURCE_MODE = "candidate_extraction_review_plan_dry_run_no_writes"
SOURCE_PLAN_VERSION = "memory_v1_candidate_extraction_review_20260713_v1"
STANDARD_ROUTE = "standard_reviewed_extraction"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DECISIONS = frozenset({"rewrite", "split", "defer", "reject"})
TARGET_ROLES = {
    "claim": frozenset(
        {
            "user_assertion",
            "user_belief_or_opinion",
            "mixed_user_assertion_and_belief",
        }
    ),
    "preference": frozenset({"response_or_life_preference"}),
    "project_knowledge": frozenset({"project_assertion"}),
}
DECISION_ACTIONS = {
    "rewrite": {
        "claim": "claim_candidate_after_comparison",
        "preference": "preference_candidate_after_schema",
        "project_knowledge": "project_candidate_after_schema",
    },
    "split": {
        "claim": "reextract_split_units",
    },
    "defer": {
        "claim": "reextract_expanded_context",
        "project_knowledge": "reextract_expanded_context",
    },
    "reject": {
        "claim": "retain_evidence_only",
    },
}


class SpecializedReviewError(RuntimeError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _required_text(value: Any, field: str, *, max_length: int = 4000) -> str:
    text = str(value or "").strip()
    if not text:
        raise SpecializedReviewError(f"{field} is required")
    if len(text) > max_length:
        raise SpecializedReviewError(f"{field} exceeds {max_length} characters")
    return text


def _object(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecializedReviewError(f"{field} must be an object")
    return dict(value)


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise SpecializedReviewError(f"{field} must be an array")
    return value


def _count_map(values: Iterable[str]) -> Dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _load_json(path: str | Path, field: str) -> tuple[Path, bytes, Dict[str, Any]]:
    resolved = Path(path).resolve()
    try:
        raw_bytes = resolved.read_bytes()
        raw = json.loads(raw_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecializedReviewError(f"cannot read {field}: {resolved}") from exc
    return resolved, raw_bytes, _object(raw, field)


def _specialized_units(source: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    units: Dict[str, Dict[str, Any]] = {}
    lanes = _object(source.get("lanes"), "source.lanes")
    for lane_name, lane_value in lanes.items():
        lane = _object(lane_value, f"source.lanes.{lane_name}")
        target = _required_text(lane.get("target"), f"source.lanes.{lane_name}.target")
        for raw_unit in _array(
            lane.get("review_units"), f"source.lanes.{lane_name}.review_units"
        ):
            unit = _object(raw_unit, "source review unit")
            if unit.get("primary_route") == STANDARD_ROUTE:
                continue
            evidence_id = _required_text(unit.get("evidence_id"), "evidence_id")
            uuid.UUID(evidence_id)
            if evidence_id in units:
                raise SpecializedReviewError(f"duplicate source evidence_id: {evidence_id}")
            units[evidence_id] = {**unit, "target": target, "lane": lane_name}
    return units


def _validate_controls(controls: Mapping[str, Any]) -> None:
    if controls.get("apply_authorized") is not False:
        raise SpecializedReviewError("review must explicitly prohibit apply")
    for field in (
        "candidate_writes",
        "claim_writes",
        "preference_writes",
        "project_knowledge_writes",
        "qdrant_writes",
    ):
        if controls.get(field) != 0:
            raise SpecializedReviewError(f"controls.{field} must be zero")
    for field in ("prompt_injection", "automatic_promotion"):
        if controls.get(field) is not False:
            raise SpecializedReviewError(f"controls.{field} must be false")


def _validate_normalization_sources(
    values: Sequence[Any],
    source_units: Mapping[str, Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    result: list[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(values):
        item = _object(raw, f"external_normalization_sources[{index}]")
        evidence_id = _required_text(item.get("evidence_id"), "normalization evidence_id")
        if evidence_id not in source_units:
            raise SpecializedReviewError("normalization source references an unknown unit")
        field = _required_text(item.get("field"), "normalization field", max_length=120)
        key = (evidence_id, field)
        if key in seen:
            raise SpecializedReviewError("duplicate external normalization source")
        seen.add(key)
        source_url = _required_text(item.get("source_url"), "normalization source_url")
        if not source_url.startswith("https://"):
            raise SpecializedReviewError("normalization source_url must use HTTPS")
        result.append(
            {
                "evidence_id": evidence_id,
                "field": field,
                "normalized_value": _required_text(
                    item.get("normalized_value"), "normalized_value"
                ),
                "source_kind": _required_text(item.get("source_kind"), "source_kind"),
                "source_url": source_url,
            }
        )
    return result


def _validate_decision(
    raw: Mapping[str, Any],
    *,
    packet_route: str,
    source_unit: Mapping[str, Any],
) -> Dict[str, Any]:
    decision = _required_text(raw.get("decision"), "decision")
    if decision not in DECISIONS:
        raise SpecializedReviewError(f"unsupported decision: {decision}")
    target = _required_text(raw.get("target"), "target")
    if target != source_unit.get("target"):
        raise SpecializedReviewError("review target differs from source target")
    if source_unit.get("primary_route") != packet_route:
        raise SpecializedReviewError("decision is in the wrong route packet")
    role = _required_text(raw.get("reviewed_epistemic_role"), "reviewed_epistemic_role")
    if role not in TARGET_ROLES.get(target, frozenset()):
        raise SpecializedReviewError(f"invalid reviewed role for {target}: {role}")
    expected_action = DECISION_ACTIONS.get(decision, {}).get(target)
    action = _required_text(raw.get("recommended_action"), "recommended_action")
    if action != expected_action:
        raise SpecializedReviewError(
            f"invalid action for {decision}/{target}: expected={expected_action}, got={action}"
        )
    drafts = _array(raw.get("canonical_drafts"), "canonical_drafts")
    if any(not isinstance(value, str) or not value.strip() for value in drafts):
        raise SpecializedReviewError("canonical drafts must be non-empty strings")
    if decision == "rewrite" and len(drafts) != 1:
        raise SpecializedReviewError("rewrite must contain exactly one canonical draft")
    if decision == "split" and len(drafts) < 2:
        raise SpecializedReviewError("split must contain at least two canonical drafts")
    if decision in {"defer", "reject"} and drafts:
        raise SpecializedReviewError(f"{decision} must not contain canonical drafts")
    reasons = _array(raw.get("reason_codes"), "reason_codes")
    if not reasons or any(not isinstance(value, str) or not value.strip() for value in reasons):
        raise SpecializedReviewError("reason_codes must contain non-empty strings")
    if len(reasons) != len(set(reasons)):
        raise SpecializedReviewError("reason_codes must be unique")
    return {
        "evidence_id": str(source_unit["evidence_id"]),
        "review_unit_id": str(source_unit["review_unit_id"]),
        "input_lock_sha256": str(source_unit["input_lock_sha256"]),
        "content_sha256": str(source_unit["content_sha256"]),
        "target": target,
        "lane": str(source_unit["lane"]),
        "route": packet_route,
        "source_epistemic_role": str(source_unit["epistemic_role"]),
        "reviewed_epistemic_role": role,
        "decision": decision,
        "recommended_action": action,
        "surface_policy": _required_text(raw.get("surface_policy"), "surface_policy"),
        "canonical_drafts": [value.strip() for value in drafts],
        "reason_codes": list(reasons),
        "preview": str(source_unit["preview"]),
    }


def build_specialized_review_report(
    *,
    source_report_path: str | Path,
    review_path: str | Path,
) -> Dict[str, Any]:
    source_path, source_bytes, source = _load_json(source_report_path, "source report")
    review_file, review_bytes, review = _load_json(review_path, "review file")
    if source.get("mode") != SOURCE_MODE:
        raise SpecializedReviewError("source report has the wrong mode")
    if source.get("plan_version") != SOURCE_PLAN_VERSION:
        raise SpecializedReviewError("source report has the wrong plan version")
    if review.get("review_version") != REVIEW_VERSION:
        raise SpecializedReviewError("review file has the wrong review version")
    source_sha = _sha256_bytes(source_bytes)
    if review.get("source_report_sha256") != source_sha:
        raise SpecializedReviewError("source report SHA-256 differs from review lock")
    if review.get("source_plan_version") != source.get("plan_version"):
        raise SpecializedReviewError("source plan version differs from review lock")
    if str(review.get("owner_user_id")) != str(source.get("owner_user_id")):
        raise SpecializedReviewError("review/source owner mismatch")
    if str(review.get("batch_id")) != str(source.get("source_batch", {}).get("batch_id")):
        raise SpecializedReviewError("review/source batch mismatch")
    controls = _object(review.get("controls"), "controls")
    _validate_controls(controls)

    source_units = _specialized_units(source)
    expected_count = review.get("expected_specialized_units")
    if isinstance(expected_count, bool) or not isinstance(expected_count, int):
        raise SpecializedReviewError("expected_specialized_units must be an integer")
    if expected_count != len(source_units):
        raise SpecializedReviewError("specialized source unit count differs from review lock")

    normalization_sources = _validate_normalization_sources(
        _array(
            review.get("external_normalization_sources", []),
            "external_normalization_sources",
        ),
        source_units,
    )
    reviewed_rows: list[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    packet_outputs: list[Dict[str, Any]] = []
    for packet_index, raw_packet in enumerate(_array(review.get("packets"), "packets")):
        packet = _object(raw_packet, f"packets[{packet_index}]")
        route = _required_text(packet.get("route"), "packet route")
        decisions: list[Dict[str, Any]] = []
        for raw_decision in _array(packet.get("decisions"), "packet decisions"):
            decision_map = _object(raw_decision, "packet decision")
            evidence_id = _required_text(decision_map.get("evidence_id"), "evidence_id")
            if evidence_id in seen_ids:
                raise SpecializedReviewError(f"duplicate reviewed evidence_id: {evidence_id}")
            source_unit = source_units.get(evidence_id)
            if source_unit is None:
                raise SpecializedReviewError(f"review references unknown evidence_id: {evidence_id}")
            seen_ids.add(evidence_id)
            validated = _validate_decision(
                decision_map,
                packet_route=route,
                source_unit=source_unit,
            )
            decisions.append(validated)
            reviewed_rows.append(validated)
        packet_outputs.append(
            {
                "route": route,
                "reviewed_unit_count": len(decisions),
                "decisions": decisions,
            }
        )
    missing = set(source_units) - seen_ids
    extra = seen_ids - set(source_units)
    if missing or extra:
        raise SpecializedReviewError(
            f"review coverage mismatch: missing={sorted(missing)}, extra={sorted(extra)}"
        )

    role_corrections = sum(
        row["source_epistemic_role"] != row["reviewed_epistemic_role"]
        for row in reviewed_rows
    )
    reviewed_input_sha = hashlib.sha256(
        _stable_json(
            [
                {
                    "decision": row["decision"],
                    "evidence_id": row["evidence_id"],
                    "input_lock_sha256": row["input_lock_sha256"],
                    "recommended_action": row["recommended_action"],
                    "reviewed_epistemic_role": row["reviewed_epistemic_role"],
                }
                for row in reviewed_rows
            ]
        ).encode("utf-8")
    ).hexdigest()
    return {
        "mode": "specialized_candidate_review_packets_no_writes",
        "review_version": REVIEW_VERSION,
        "review_file": str(review_file),
        "review_file_sha256": _sha256_bytes(review_bytes),
        "source_report": str(source_path),
        "source_report_sha256": source_sha,
        "reviewed_input_sha256": reviewed_input_sha,
        "owner_user_id": str(review["owner_user_id"]),
        "batch_id": str(review["batch_id"]),
        "review_authority": str(review["review_authority"]),
        "summary": {
            "reviewed_unit_count": len(reviewed_rows),
            "packets": _count_map(row["route"] for row in reviewed_rows),
            "targets": _count_map(row["target"] for row in reviewed_rows),
            "decisions": _count_map(row["decision"] for row in reviewed_rows),
            "recommended_actions": _count_map(
                row["recommended_action"] for row in reviewed_rows
            ),
            "canonical_draft_count": sum(
                len(row["canonical_drafts"]) for row in reviewed_rows
            ),
            "epistemic_role_correction_count": role_corrections,
            "external_normalization_source_count": len(normalization_sources),
            "candidate_records_created": 0,
            "durable_records_created": 0,
        },
        "controls": {
            **controls,
            "source_report_hash_verified": True,
            "every_specialized_unit_reviewed_once": True,
            "full_evidence_content_in_report": False,
            "source_evidence_mutated": False,
            "target_lane_changes_applied": False,
            "user_ratification_required": True,
            "apply_option_exposed": False,
        },
        "external_normalization_sources": normalization_sources,
        "ready_for_user_ratification": True,
        "packets": packet_outputs,
    }
