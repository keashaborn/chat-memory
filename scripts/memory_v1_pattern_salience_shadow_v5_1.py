#!/usr/bin/env python3
"""Deterministic, owner-scoped V5.1 pattern/salience shadow generator.

The default mode is a repeatable-read, zero-write evaluation.  It consumes the
restricted Postgres input surface and emits no evidence, claim, or prompt prose.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable
import uuid

from scripts.memory_v1_pattern_salience_policy_v5_1 import (
    PatternInput,
    assess_pattern,
)


WORKER_VERSION = "memory_v1_pattern_salience_shadow_v5_1"
INPUT_CONTRACT = "memory_v1_pattern_salience_shadow_input_v5_1"
REPORT_CONTRACT = "memory_v1_pattern_salience_shadow_report_v5_1"
ASSESSMENT_CONTRACT = "memory_v1_epistemic_pattern_salience_v5_1"
POLICY_VERSION = "memory_v1_epistemic_pattern_salience_policy_v5_1"
REGISTRY_VERSION = "memory_predicate_registry_v5_1"
ASSESSMENT_METHOD = "memory_v1_epistemic_assessment_v5_1"
SALIENCE_METHOD = "memory_v1_salience_features_v5_1"
REQUEST_NAMESPACE = uuid.UUID("9bd718f7-b86e-5592-bfbd-230a14b33a88")

EVENT_PATTERN_PREFIXES = ("health.", "life_event.", "event.", "state.")
STRUCTURED_PREFIXES = ("nutrition.", "training.", "measurement.")
SENSITIVITY_RANK = {"low": 0, "medium": 1, "high": 2, "restricted": 3}


class ShadowGenerationError(RuntimeError):
    pass


def stable_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256(value: Any) -> str:
    return sha256_text(stable_json(value))


def round4(value: float) -> float | int:
    result = round(min(1.0, max(0.0, value)) + 0.0, 4)
    return 0 if result == 0 else 1 if result == 1 else result


def mean(values: Iterable[float]) -> float:
    materialized = list(values)
    return fmean(materialized) if materialized else 0.0


def evidence_dimension(value: Any) -> float:
    """Use a neutral prior for legacy evidence whose component is unknown."""
    return 0.5 if value is None else float(value)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


def validate_snapshot(snapshot: dict[str, Any], owner: uuid.UUID) -> None:
    required = {
        "contract_version",
        "owner_user_id_sha256",
        "observation_total",
        "observation_returned",
        "observations_truncated",
        "target_total",
        "target_returned",
        "targets_truncated",
        "observations",
        "targets",
    }
    if set(snapshot) != required:
        raise ShadowGenerationError("shadow input shape is not closed")
    if snapshot["contract_version"] != INPUT_CONTRACT:
        raise ShadowGenerationError("shadow input contract mismatch")
    if snapshot["owner_user_id_sha256"] != sha256_text(str(owner)):
        raise ShadowGenerationError("shadow input owner binding mismatch")
    if snapshot["observations_truncated"] or snapshot["targets_truncated"]:
        raise ShadowGenerationError("shadow input was truncated")
    if snapshot["observation_total"] != len(snapshot["observations"]):
        raise ShadowGenerationError("shadow observation count mismatch")
    if snapshot["target_total"] != len(snapshot["targets"]):
        raise ShadowGenerationError("shadow target count mismatch")


def pattern_route(observation: dict[str, Any]) -> tuple[str | None, str | None]:
    predicate = observation["predicate"]
    if predicate.startswith(STRUCTURED_PREFIXES):
        return None, "authoritative_structured_domain_route"
    if observation["subject_entity_id"] is None:
        return None, "unresolved_subject_entity"
    if observation["object_kind"] == "entity" and observation["object_entity_id"] is None:
        return None, "unresolved_object_entity"
    if observation["object_kind"] == "literal":
        return None, "literal_object_identity_not_bound"
    if not predicate.startswith(EVENT_PATTERN_PREFIXES):
        return None, "static_assertion_routes_to_claim_reassessment"
    if predicate.startswith("state."):
        return "persistence", None
    return "recurrence", None


def observation_manifest(items: list[dict[str, Any]]) -> str:
    rows = [
        "|".join(
            str(item.get(key) or "")
            for key in (
                "observation_id",
                "evidence_id",
                "observation_role",
                "episode_key_sha256",
                "independence_key_sha256",
                "temporal_bucket_sha256",
                "observation_sha256",
                "evidence_content_sha256",
            )
        )
        for item in sorted(items, key=lambda row: row["observation_id"])
    ]
    return sha256_text("\n".join(rows))


def build_pattern_evaluation(
    observations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    routed_counts: dict[str, int] = defaultdict(int)
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        pattern_kind, rejection = pattern_route(observation)
        if rejection:
            routed_counts[rejection] += 1
            continue
        key = (
            pattern_kind or "",
            observation["subject_entity_id"],
            observation["object_entity_id"],
            observation["predicate"],
        )
        groups[key].append(observation)

    evaluations: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    for key, rows in sorted(groups.items()):
        pattern_kind, subject, secondary, predicate = key
        occurrences = [row for row in rows if row["polarity"] != "negated"]
        counterexamples = [row for row in rows if row["polarity"] == "negated"]
        episodes = {row["episode_key_sha256"] for row in occurrences}
        buckets = {row["temporal_bucket_sha256"] for row in occurrences}
        dates = sorted(
            value for value in (parse_date(row["effective_date"]) for row in rows)
            if value is not None
        )
        span_days = (dates[-1] - dates[0]).days if dates else 0
        decision = assess_pattern(
            PatternInput(
                pattern_kind=pattern_kind,
                eligible_occurrence_count=len(occurrences),
                independent_episode_count=len(episodes),
                distinct_temporal_bucket_count=len(buckets),
                span_days=span_days,
                counterexample_count=len(counterexamples),
                explicit_owner_pattern_report=False,
                transient_identity_risk=predicate.startswith("health."),
                structured_domain_authoritative=False,
                third_party_scope=rows[0]["subject_entity_type"] != "self",
            )
        )
        evaluations.append(
            {
                "pattern_key_sha256": sha256_text(
                    "|".join(
                        (
                            "memory_v1_pattern_key_v5_1",
                            pattern_kind,
                            subject,
                            secondary or "",
                            predicate,
                        )
                    )
                ),
                "pattern_kind": pattern_kind,
                "predicate_family": predicate,
                "occurrence_count": len(occurrences),
                "counterexample_count": len(counterexamples),
                "independent_episode_count": len(episodes),
                "distinct_temporal_bucket_count": len(buckets),
                "disposition": decision.disposition,
                "pattern_state": decision.pattern_state,
                "automatic_review_eligible": decision.automatic_review_eligible,
                "reason_codes": list(decision.reason_codes),
            }
        )
        if decision.disposition not in {"pattern_review", "manual_review"}:
            continue
        items = []
        for row in sorted(rows, key=lambda item: item["observation_id"]):
            items.append(
                {
                    "observation_id": row["observation_id"],
                    "evidence_id": row["evidence_id"],
                    "observation_role": (
                        "counterexample" if row["polarity"] == "negated" else "occurrence"
                    ),
                    "episode_key_sha256": row["episode_key_sha256"],
                    "independence_key_sha256": row["independence_key_sha256"],
                    "temporal_bucket_sha256": row["temporal_bucket_sha256"],
                    "observation_sha256": row["observation_sha256"],
                    "evidence_content_sha256": row["evidence_content_sha256"],
                }
            )
        sensitivity = max(
            (row["sensitivity"] for row in rows), key=SENSITIVITY_RANK.__getitem__
        )
        proposal = {
            "contract_version": "memory_v1_pattern_review_v5_1",
            "policy_version": POLICY_VERSION,
            "source_registry_version": REGISTRY_VERSION,
            "pattern_key_sha256": evaluations[-1]["pattern_key_sha256"],
            "pattern_kind": pattern_kind,
            "subject_entity_id": subject,
            "secondary_entity_id": secondary,
            "predicate_family": predicate,
            "sensitivity": sensitivity,
            "pattern_state": decision.pattern_state,
            "occurrence_count": len(occurrences),
            "counterexample_count": len(counterexamples),
            "independent_episode_count": len(episodes),
            "distinct_temporal_bucket_count": len(buckets),
            "valid_from": dates[0].isoformat() if dates else None,
            "valid_to": None,
            "automatic_review_eligible": decision.automatic_review_eligible,
            "identity_inference_forbidden": True,
            "causal_inference_forbidden": True,
            "reason_codes": list(decision.reason_codes),
            "method_version": "memory_v1_pattern_shadow_generator_v5_1",
            "input_manifest_sha256": observation_manifest(items),
            "observation_items": items,
            "proposal_sha256": "0" * 64,
        }
        proposal["proposal_sha256"] = sha256(
            {key: value for key, value in proposal.items() if key != "proposal_sha256"}
        )
        proposals.append(proposal)
    return evaluations, proposals, dict(sorted(routed_counts.items()))


def assessment_state(target: dict[str, Any], counts: dict[str, int]) -> str:
    if target["status"] == "retracted":
        return "retracted"
    if target["status"] == "superseded":
        return "superseded"
    if target["status"] == "disputed" or (counts["supports"] and counts["opposes"]):
        return "contested"
    if not counts["supports"]:
        return "insufficient"
    if target["target_kind"] == "project_knowledge":
        return "supported" if (target["authority_state"] or "").startswith("ratified:") else "emerging"
    return "supported" if target["status"] == "supported" else "emerging"


def importance_for(target: dict[str, Any]) -> float:
    if target["status"] in {"retracted", "superseded"}:
        return 0.0
    if target["target_kind"] == "project_knowledge":
        return 0.7
    target_class = target["target_class"]
    if target_class.startswith(("identity.", "name.")):
        return 0.6
    if target_class.startswith("relationship."):
        return 0.6
    if target_class.startswith(("personal_event.", "life_event.")):
        return 0.6
    if target_class.startswith("pet."):
        return 0.4
    return 0.4


def half_life_days(target: dict[str, Any]) -> int:
    if target["target_kind"] == "project_knowledge":
        return 30
    target_class = target["target_class"]
    if target_class.startswith(("identity.", "name.", "pet.")):
        return 3650
    if target_class.startswith(("personal_event.", "life_event.")):
        return 3650
    if target_class.startswith("relationship."):
        return 730
    if target_class.startswith("health."):
        return 14
    return 365


def build_target_packet(
    target: dict[str, Any],
    observations_by_id: dict[str, dict[str, Any]],
    as_of: date,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    links = target["observation_links"]
    missing = sorted(
        link["observation_id"]
        for link in links
        if link["observation_id"] not in observations_by_id
    )
    if missing:
        raise ShadowGenerationError("target references an unavailable observation")
    linked = [(link, observations_by_id[link["observation_id"]]) for link in links]
    roles: dict[str, list[str]] = {
        "supports": [], "opposes": [], "qualifies": [], "corrects": []
    }
    for link, _ in linked:
        stance = link["stance"]
        if stance == "supports":
            roles["supports"].append(link["observation_id"])
        elif stance == "opposes":
            roles["opposes"].append(link["observation_id"])
        else:
            roles["qualifies"].append(link["observation_id"])
    for values in roles.values():
        values.sort()
    counts = {key: len(value) for key, value in roles.items()}
    state = assessment_state(target, counts)
    if not linked:
        return None, {
            "target_kind": target["target_kind"],
            "target_id_sha256": sha256_text(target["target_id"]),
            "disposition": "not_persistable",
            "reason_codes": ["missing_governed_observation_links"],
        }

    considered = [row for _, row in linked]
    support_rows = [
        (link, row) for link, row in linked if link["stance"] == "supports"
    ]
    oppose_rows = [
        (link, row) for link, row in linked if link["stance"] == "opposes"
    ]
    independence_keys = {row["independence_key_sha256"] for _, row in support_rows}
    opposition_keys = {row["independence_key_sha256"] for _, row in oppose_rows}
    source_count = len({row["evidence_source_system"] for row in considered})
    directness = mean(evidence_dimension(row["evidence_directness"]) for row in considered)
    reliability = mean(
        evidence_dimension(row["evidence_source_reliability"])
        for row in considered
    )
    relevance = mean(float(link["relevance"]) for link, _ in linked)
    extraction = mean(float(row["extraction_confidence"]) for row in considered)
    independence = len(independence_keys | opposition_keys) / len(linked)
    valid_from = parse_date(target["valid_from"])
    valid_to = parse_date(target["valid_to"])
    temporal_fit = 0.0 if (
        (valid_from is not None and as_of < valid_from)
        or (valid_to is not None and as_of > valid_to)
    ) else 1.0
    specificity = 1.0 if target["object_kind"] in {"literal", "entity"} else 0.0
    base_strength = min(directness, reliability, relevance, extraction)
    support_strength = base_strength * (len(independence_keys) / max(1, len(support_rows)))
    opposition_strength = base_strength * (len(opposition_keys) / max(1, len(oppose_rows)))
    input_rows = [
        {
            "observation_id": link["observation_id"],
            "stance": link["stance"],
            "relevance": link["relevance"],
            "observation_sha256": row["observation_sha256"],
            "evidence_content_sha256": row["evidence_content_sha256"],
            "independence_key_sha256": row["independence_key_sha256"],
        }
        for link, row in linked
    ]
    inputs_sha = sha256(sorted(input_rows, key=lambda row: (row["observation_id"], row["stance"])))
    latest = max(
        value for value in (parse_date(row["effective_date"]) for row in considered)
        if value is not None
    )
    age_days = max(0, (as_of - latest).days)
    recency = math.pow(0.5, age_days / half_life_days(target))
    frequency = min(1.0, max(0, len(independence_keys) - 1) / 2.0)
    contradiction = 1.0 if state in {"retracted", "contested"} else (
        counts["opposes"] / max(1, counts["supports"] + counts["opposes"])
    )
    reason_codes = [f"assessment_{state}", "deterministic_multidimensional_salience"]
    packet = {
        "contract_version": ASSESSMENT_CONTRACT,
        "policy_version": POLICY_VERSION,
        "source_registry_version": REGISTRY_VERSION,
        "target": {
            "target_kind": target["target_kind"],
            "target_id": target["target_id"],
            "target_revision_number": target["target_revision_number"],
            "semantic_key_sha256": target["semantic_key_sha256"],
        },
        "evidence_assessment": {
            "assessment_state": state,
            "supporting_observation_count": counts["supports"],
            "opposing_observation_count": counts["opposes"],
            "qualifying_observation_count": counts["qualifies"],
            "corrective_observation_count": counts["corrects"],
            "independent_support_cluster_count": len(independence_keys),
            "independent_opposition_cluster_count": len(opposition_keys),
            "source_diversity_count": source_count,
            "supporting_observation_ids": roles["supports"],
            "opposing_observation_ids": roles["opposes"],
            "qualifying_observation_ids": roles["qualifies"],
            "corrective_observation_ids": roles["corrects"],
            "dimensions": {
                "directness": round4(directness),
                "source_reliability": round4(reliability),
                "independence": round4(independence),
                "relevance": round4(relevance),
                "temporal_fit": round4(temporal_fit),
                "specificity": round4(specificity),
                "extraction_quality": round4(extraction),
                "support_strength": round4(support_strength),
                "opposition_strength": round4(opposition_strength),
            },
            "method": "deterministic_policy",
            "method_version": ASSESSMENT_METHOD,
            "inputs_sha256": inputs_sha,
        },
        "pattern_assessment": None,
        "salience_features": {
            "importance": round4(importance_for(target)),
            "frequency": round4(frequency),
            "recency": round4(recency),
            "emotional_significance": 0,
            "goal_relevance": 0,
            "future_utility": 0,
            "retrieval_utility": 0,
            "contradiction_pressure": round4(contradiction),
            "as_of_date": as_of.isoformat(),
            "method_version": SALIENCE_METHOD,
            "signal_manifest_sha256": sha256([]),
        },
        "retrieval_history": {
            "eligible_count": 0,
            "selected_count": 0,
            "injected_count": 0,
            "explicitly_helpful_count": 0,
            "explicitly_confirmed_count": 0,
            "corrected_count": 0,
            "not_relevant_count": 0,
            "caused_confusion_count": 0,
            "last_retrieval_trace_id": None,
        },
        "reason_codes": reason_codes,
        "input_manifest_sha256": inputs_sha,
        "packet_sha256": "0" * 64,
    }
    packet["packet_sha256"] = sha256(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    )
    request_id = uuid.uuid5(
        REQUEST_NAMESPACE,
        f"{target['target_id']}|{target['target_revision_number']}|{packet['packet_sha256']}",
    )
    return packet, {
        "target_kind": target["target_kind"],
        "target_id_sha256": sha256_text(target["target_id"]),
        "disposition": "snapshot_candidate",
        "assessment_state": state,
        "request_id": str(request_id),
        "packet_sha256": packet["packet_sha256"],
        "reason_codes": reason_codes,
    }


def generate_report(
    snapshot: dict[str, Any], owner: uuid.UUID, as_of: date
) -> dict[str, Any]:
    validate_snapshot(snapshot, owner)
    observations = snapshot["observations"]
    observations_by_id = {row["observation_id"]: row for row in observations}
    if len(observations_by_id) != len(observations):
        raise ShadowGenerationError("duplicate observation identifier")
    pattern_evaluations, pattern_proposals, pattern_routes = build_pattern_evaluation(
        observations
    )
    target_packets = []
    target_evaluations = []
    for target in snapshot["targets"]:
        packet, evaluation = build_target_packet(target, observations_by_id, as_of)
        target_evaluations.append(evaluation)
        if packet is not None:
            target_packets.append(
                {
                    "request_id": evaluation["request_id"],
                    "packet": packet,
                }
            )
    report = {
        "contract_version": REPORT_CONTRACT,
        "worker_version": WORKER_VERSION,
        "policy_version": POLICY_VERSION,
        "owner_user_id_sha256": sha256_text(str(owner)),
        "as_of_date": as_of.isoformat(),
        "input_snapshot_sha256": sha256(snapshot),
        "input_counts": {
            "observations": len(observations),
            "targets": len(snapshot["targets"]),
        },
        "pattern_routes": pattern_routes,
        "pattern_evaluations": pattern_evaluations,
        "pattern_proposals": pattern_proposals,
        "target_evaluations": target_evaluations,
        "target_snapshot_candidates": target_packets,
        "write_budget": {
            "pattern_reviews": len(pattern_proposals),
            "target_snapshot_packets": len(target_packets),
            "pattern_heads": 0,
            "pattern_revisions": 0,
            "qdrant": 0,
            "prompt_influence": 0,
        },
        "proof": {
            "database_writes": 0,
            "external_model_calls": 0,
            "local_model_calls": 0,
            "qdrant_writes": 0,
            "prompt_influence": 0,
            "owner_scope_bound": True,
            "input_complete": True,
            "literal_pattern_identity_fail_closed": True,
        },
        "report_sha256": "0" * 64,
    }
    report["report_sha256"] = sha256(
        {key: value for key, value in report.items() if key != "report_sha256"}
    )
    return report


def secure_write(path: Path, value: dict[str, Any]) -> str:
    payload = stable_json(value) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return sha256_text(payload)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--as-of-date", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-observations", type=int, default=256)
    parser.add_argument("--max-targets", type=int, default=256)
    return parser.parse_args()


async def load_snapshot(
    dsn: str, owner: uuid.UUID, max_observations: int, max_targets: int
) -> dict[str, Any]:
    import asyncpg

    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ShadowGenerationError("shadow generator requires brains_app")
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            raw = await conn.fetchval(
                "SELECT memory.load_pattern_salience_shadow_inputs_v5_1($1,$2)",
                max_observations,
                max_targets,
            )
            txid = await conn.fetchval("SELECT txid_current_if_assigned()")
        if txid is not None:
            raise ShadowGenerationError("read-only shadow input assigned a txid")
        return json.loads(raw) if isinstance(raw, str) else raw
    finally:
        await conn.close()


async def run() -> int:
    args = arguments()
    owner = uuid.UUID(args.owner_user_id)
    as_of = date.fromisoformat(args.as_of_date)
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ShadowGenerationError("POSTGRES_DSN is required")
    snapshot = await load_snapshot(
        dsn, owner, args.max_observations, args.max_targets
    )
    report = generate_report(snapshot, owner, as_of)
    if args.output:
        file_sha = secure_write(args.output, report)
        print(stable_json({
            "outcome": "zero_write_report_created",
            "output": str(args.output),
            "file_sha256": file_sha,
            "report_sha256": report["report_sha256"],
            "pattern_proposals": len(report["pattern_proposals"]),
            "target_snapshot_candidates": len(report["target_snapshot_candidates"]),
            "database_writes": 0,
        }))
    else:
        print(stable_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
