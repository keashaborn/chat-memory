#!/usr/bin/env python3
"""Resolve a governed claim projection to create, reinforce, or manual review."""

from __future__ import annotations

import json
from typing import Any, Mapping
import uuid

from scripts.memory_v1_projection_v5_2_contract import validate_packet
from scripts.memory_v1_v5_2_projection_dispatch import (
    build_packet,
    build_reinforcement_packet,
    sha256,
    stable_json,
)


class ClaimTargetResolutionError(RuntimeError):
    pass


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ClaimTargetResolutionError(f"{field} must be a JSON object")
    return value


def _uuid_text(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClaimTargetResolutionError(f"{field} must be a UUID") from exc


def _projection(packet: Mapping[str, Any]) -> dict[str, Any]:
    projections = packet.get("projections")
    if not isinstance(projections, list) or len(projections) != 1:
        raise ClaimTargetResolutionError("projection packet must contain one item")
    projection = projections[0]
    if not isinstance(projection, dict) or projection.get("lane") != "claim":
        raise ClaimTargetResolutionError("target resolver accepts only claim projections")
    return projection


def _claim_review_reason_codes(
    row: Mapping[str, Any], projection: Mapping[str, Any]
) -> list[str]:
    identity = projection["identity"]
    payload = projection["payload"]
    if (
        str(row["status"]) != "supported"
        or _uuid_text(row["subject_entity_id"], "claim subject")
        != identity["subject_entity_id"]
        or str(row["predicate"]) != identity["predicate"]
        or (
            _uuid_text(row["object_entity_id"], "claim object")
            if row["object_entity_id"] is not None
            else None
        )
        != identity["object_entity_id"]
        or str(row["canonical_key"])
        != f"v5:{identity['semantic_key_sha256']}"
    ):
        raise ClaimTargetResolutionError(
            "existing governed claim differs from the semantic projection"
        )
    object_literal = row["object_literal"]
    if isinstance(object_literal, str):
        object_literal = json.loads(object_literal)
    expected_literal_sha = (
        sha256(object_literal) if object_literal is not None else None
    )
    if expected_literal_sha != identity["object_literal_sha256"]:
        raise ClaimTargetResolutionError(
            "existing governed claim literal differs from the projection"
        )
    retrieval_policy = _json_object(row["retrieval_policy"], "retrieval policy")
    if not isinstance(row["current_revision_number"], int) or (
        row["current_revision_number"] < 1
    ):
        raise ClaimTargetResolutionError(
            "existing governed claim has no current revision"
        )
    reasons: list[str] = []
    if str(row["canonical_text"]) != payload["canonical_text"]:
        reasons.append("existing_semantic_aggregate_render_drift")
    if retrieval_policy.get("surface_policy") != payload["surface_policy"]:
        reasons.append("existing_semantic_aggregate_policy_drift")
    return reasons


def _claim_matches_projection(
    row: Mapping[str, Any], projection: Mapping[str, Any]
) -> None:
    reasons = _claim_review_reason_codes(row, projection)
    if reasons:
        raise ClaimTargetResolutionError(",".join(reasons))


async def resolve_claim_projection_target(
    conn: Any,
    *,
    owner_user_id: str,
    source: Mapping[str, Any],
    plan_id: str,
    registry: Mapping[str, Mapping[str, Any]],
    allow_existing_plan: bool = False,
) -> dict[str, Any]:
    """Resolve one exact source observation without mutating any store."""

    owner = _uuid_text(owner_user_id, "owner_user_id")
    normalized_plan = uuid.UUID(plan_id)
    actor = await conn.fetchval("SELECT memory.current_actor_user_id()")
    if actor is None or str(actor) != owner:
        raise ClaimTargetResolutionError("authenticated owner context mismatch")

    create_packet = build_packet(owner, source)
    validate_packet(create_packet, owner, dict(registry))
    create_projection = _projection(create_packet)
    create_packet_text = stable_json(create_packet)
    create_preflight_row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_packet_v5_2($1,$2)",
        normalized_plan,
        create_packet_text,
    )
    if create_preflight_row is None:
        raise ClaimTargetResolutionError("create preflight returned no row")
    create_preflight = dict(create_preflight_row)
    create_existing_plans = int(create_preflight["existing_plans"])
    if create_existing_plans not in ({0, 1} if allow_existing_plan else {0}):
        raise ClaimTargetResolutionError("an equivalent projection plan already exists")
    aggregate_count = int(create_preflight["existing_aggregates"])
    semantic_key = str(create_preflight["semantic_key_sha256"])
    if semantic_key != create_projection["identity"]["semantic_key_sha256"]:
        raise ClaimTargetResolutionError("database semantic key differs from packet")

    if aggregate_count == 0:
        return {
            "action": "create",
            "reason_codes": ["no_existing_semantic_aggregate"],
            "packet": create_packet,
            "packet_sha256": create_packet["packet_sha256"],
            "owner_manifest_sha256": create_preflight[
                "owner_manifest_sha256"
            ],
            "semantic_key_sha256": semantic_key,
            "target_claim_id": None,
            "expected_revision_number": None,
            "existing_claim_status": None,
        }
    if aggregate_count != 1:
        raise ClaimTargetResolutionError(
            "semantic identity resolved to multiple governed claims"
        )

    claim_row = await conn.fetchrow(
        """
        SELECT claim.claim_id,claim.status::text,claim.subject_entity_id,
               claim.predicate,claim.object_entity_id,claim.object_literal,
               claim.canonical_text,claim.canonical_key,
               claim.retrieval_policy,
               COALESCE(max(revision.revision_number),0)::integer
                 AS current_revision_number
        FROM memory.claim AS claim
        LEFT JOIN memory.claim_revision AS revision
          ON revision.owner_user_id=claim.owner_user_id
         AND revision.claim_id=claim.claim_id
        WHERE claim.owner_user_id=$1
          AND claim.canonical_key=$2
        GROUP BY claim.claim_id
        """,
        uuid.UUID(owner),
        f"v5:{semantic_key}",
    )
    if claim_row is None:
        raise ClaimTargetResolutionError(
            "semantic aggregate count did not resolve to a governed claim"
        )
    claim = dict(claim_row)
    if claim["status"] != "supported":
        return {
            "action": "manual_review",
            "reason_codes": [
                "existing_semantic_aggregate_not_supported",
                f"existing_status_{claim['status']}",
            ],
            "packet": None,
            "packet_sha256": None,
            "owner_manifest_sha256": None,
            "semantic_key_sha256": semantic_key,
            "target_claim_id": str(claim["claim_id"]),
            "expected_revision_number": int(
                claim["current_revision_number"]
            ),
            "existing_claim_status": claim["status"],
        }
    review_reasons = _claim_review_reason_codes(claim, create_projection)
    if review_reasons:
        return {
            "action": "manual_review",
            "reason_codes": review_reasons,
            "packet": None,
            "packet_sha256": None,
            "owner_manifest_sha256": None,
            "semantic_key_sha256": semantic_key,
            "target_claim_id": str(claim["claim_id"]),
            "expected_revision_number": int(
                claim["current_revision_number"]
            ),
            "existing_claim_status": claim["status"],
        }

    reinforcement_packet = build_reinforcement_packet(
        owner,
        source,
        target_claim_id=str(claim["claim_id"]),
        expected_revision_number=int(claim["current_revision_number"]),
    )
    validate_packet(reinforcement_packet, owner, dict(registry))
    reinforcement_preflight_row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_reinforcement_v5_2($1,$2)",
        normalized_plan,
        stable_json(reinforcement_packet),
    )
    if reinforcement_preflight_row is None:
        raise ClaimTargetResolutionError(
            "reinforcement preflight returned no row"
        )
    reinforcement_preflight = dict(reinforcement_preflight_row)
    if (
        int(reinforcement_preflight["existing_aggregates"]) != 1
        or int(reinforcement_preflight["existing_plans"])
        not in ({0, 1} if allow_existing_plan else {0})
        or str(reinforcement_preflight["target_claim_id"])
        != str(claim["claim_id"])
        or int(reinforcement_preflight["target_revision_number"])
        != int(claim["current_revision_number"])
    ):
        raise ClaimTargetResolutionError(
            "reinforcement preflight differs from the resolved aggregate"
        )
    return {
        "action": "reinforce",
        "reason_codes": ["exact_semantic_aggregate_already_supported"],
        "packet": reinforcement_packet,
        "packet_sha256": reinforcement_packet["packet_sha256"],
        "owner_manifest_sha256": reinforcement_preflight[
            "owner_manifest_sha256"
        ],
        "semantic_key_sha256": semantic_key,
        "target_claim_id": str(claim["claim_id"]),
        "expected_revision_number": int(claim["current_revision_number"]),
        "existing_claim_status": claim["status"],
    }
