#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
REASON_RE = re.compile(r"^[a-z][a-z0-9_]{1,99}$")
CASE_KEYS = {"case_id", "source", "expected"}
EXPECTED_KEYS = {
    "disposition",
    "required_lanes",
    "required_predicates",
    "required_blockers",
    "required_relations",
    "temporal_authority",
    "project_scope",
    "owner_binding",
    "replay_rule",
}
PACKET_KEYS = {
    "contract_version",
    "predicate_registry_version",
    "projection_policy_version",
    "projector",
    "projector_version",
    "projections",
    "packet_sha256",
}
PROJECTION_KEYS = {
    "projection_ref",
    "lane",
    "observation_inputs",
    "identity",
    "target",
    "temporal_policy",
    "review",
    "relations",
    "payload",
}
IDENTITY_KEYS = {
    "subject_entity_id",
    "predicate",
    "object_kind",
    "object_entity_id",
    "object_literal_sha256",
    "polarity",
    "modality",
    "semantic_key_sha256",
}
CLAIM_PAYLOAD_KEYS = {
    "kind",
    "claim_class",
    "canonical_text",
    "surface_policy",
}
PREFERENCE_PAYLOAD_KEYS = {
    "kind",
    "preference_class",
    "domain",
    "preference_key",
    "value",
    "preference_polarity",
    "scope",
    "stability",
    "surface_policy",
}
PROJECT_PAYLOAD_KEYS = {
    "kind",
    "project_id",
    "component_key",
    "binding_source",
    "knowledge_kind",
    "knowledge_key",
    "canonical_text",
    "document_state",
    "authority_level",
    "surface_policy",
}
FORBIDDEN_SCHEMA_PROPERTIES = {
    "owner_user_id",
    "confidence",
    "extraction_confidence",
    "truth_confidence",
    "truth_score",
    "importance",
    "salience",
    "frequency_score",
    "recency_score",
    "valid_from",
    "valid_to",
    "write_allowed",
    "approved",
}
LANE_FOR_CLASS = {
    "direct_claim": "claim",
    "supportive_context": "claim",
    "correction": "claim",
    "never_surface": "claim",
    "life_preference": "preference",
    "response_preference": "preference",
    "project_knowledge": "project_knowledge",
}
PROJECT_KIND_FOR_PREDICATE = {
    "project.constraint": "constraint",
    "project.current_state": "current_state",
    "project.proposed_feature": "proposed_feature",
    "project.requirement": "requirement",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--schema", default="specs/memory_v1_projection_plan_v5.schema.json"
    )
    parser.add_argument(
        "--registry", default="specs/memory_v1_predicate_registry_v5.json"
    )
    parser.add_argument(
        "--base-cases", default="evals/memory_v1_relational_extraction_v5_cases.jsonl"
    )
    parser.add_argument(
        "--cases", default="evals/memory_v1_projection_v5_cases.jsonl"
    )
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def property_names(value: Any) -> set[str]:
    names: set[str] = set()
    for node in walk(value):
        if isinstance(node, dict) and isinstance(node.get("properties"), dict):
            names |= set(node["properties"])
    return names


def unique_strings(value: Any, label: str) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AssertionError(f"{label} must be a string list")
    if len(value) != len(set(value)):
        raise AssertionError(f"{label} must contain unique values")
    return set(value)


def reason_codes(value: Any, label: str) -> set[str]:
    reasons = unique_strings(value, label)
    if len(reasons) > 20 or any(not REASON_RE.fullmatch(item) for item in reasons):
        raise AssertionError(f"{label} contains an invalid reason code")
    return reasons


def validate_projection_schema(schema: dict[str, Any]) -> None:
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise AssertionError("unexpected projection JSON Schema draft")
    if schema.get("additionalProperties") is not False:
        raise AssertionError("projection packet must reject extra properties")
    if schema.get("properties", {}).get("contract_version", {}).get("const") != (
        "memory_v1_projection_plan_v5"
    ):
        raise AssertionError("projection contract version mismatch")
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict) or not definitions:
        raise AssertionError("projection definitions are missing")
    for node in walk(schema):
        if isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                raise AssertionError(f"non-local projection reference: {ref}")
            if ref.removeprefix("#/$defs/") not in definitions:
                raise AssertionError(f"unresolved projection reference: {ref}")
        if isinstance(node, dict) and node.get("type") == "object":
            properties = node.get("properties")
            if properties is not None:
                if node.get("additionalProperties") is not False:
                    raise AssertionError("projection objects must reject extra properties")
                if not set(node.get("required", [])) <= set(properties):
                    raise AssertionError("projection object requires unknown property")
    forbidden = sorted(property_names(schema) & FORBIDDEN_SCHEMA_PROPERTIES)
    if forbidden:
        raise AssertionError(f"forbidden projection properties: {forbidden}")
    if set(definitions["projection"]["properties"]) != PROJECTION_KEYS:
        raise AssertionError("projection fields mismatch")
    if set(definitions["identity"]["properties"]) != IDENTITY_KEYS:
        raise AssertionError("projection identity fields mismatch")


def validate_registry(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if registry.get("registry_version") != "memory_predicate_registry_v5":
        raise AssertionError("predicate registry version mismatch")
    predicates = registry.get("predicates")
    if not isinstance(predicates, list) or len(predicates) != 25:
        raise AssertionError("projection contract requires exactly 25 V5 predicates")
    by_name: dict[str, dict[str, Any]] = {}
    for entry in predicates:
        predicate = entry["predicate"]
        if predicate in by_name:
            raise AssertionError(f"duplicate predicate: {predicate}")
        classes = unique_strings(entry["projection_classes"], "projection classes")
        if not classes or not classes <= set(LANE_FOR_CLASS):
            raise AssertionError(f"unroutable projection class: {predicate}")
        lanes = {LANE_FOR_CLASS[item] for item in classes}
        if len(lanes) != 1:
            raise AssertionError(f"predicate crosses durable lanes: {predicate}")
        if "response_preference" in classes and entry["surface_policies"] != [
            "zero_token_control_only"
        ]:
            raise AssertionError("response preference may only be a zero-token control")
        if "project_knowledge" in classes and entry["surface_policies"] != [
            "exact_project_scope_only"
        ]:
            raise AssertionError("project knowledge must remain project-scoped")
        by_name[predicate] = entry
    return by_name


def expected_lanes(base: dict[str, Any]) -> set[str]:
    return {
        LANE_FOR_CLASS[item]
        for item in base["expected"]["required_projection_classes"]
    }


def validate_cases(
    cases: list[dict[str, Any]], base_cases: list[dict[str, Any]]
) -> None:
    if len(cases) != 25 or len(base_cases) != 25:
        raise AssertionError("projection and extraction fixtures must both contain 25 cases")
    for ordinal, (case, base) in enumerate(zip(cases, base_cases, strict=True), 1):
        if set(case) != CASE_KEYS:
            raise AssertionError(f"case fields mismatch: {ordinal}")
        if case["case_id"] != f"v5-{ordinal:02d}" or case["case_id"] != base["case_id"]:
            raise AssertionError(f"case identity/order mismatch: {ordinal}")
        if case["source"] != base["source"]:
            raise AssertionError(f"source binding mismatch: {case['case_id']}")
        expected = case["expected"]
        if not isinstance(expected, dict) or set(expected) != EXPECTED_KEYS:
            raise AssertionError(f"expected fields mismatch: {case['case_id']}")
        disposition = expected["disposition"]
        if disposition not in {"no_plan", "deferred", "manual_review_required"}:
            raise AssertionError(f"invalid disposition: {case['case_id']}")
        lanes = unique_strings(expected["required_lanes"], "required lanes")
        if lanes != expected_lanes(base):
            raise AssertionError(f"projection lane drift: {case['case_id']}")
        predicates = unique_strings(expected["required_predicates"], "required predicates")
        if predicates != set(base["expected"]["required_predicate_families"]):
            raise AssertionError(f"predicate drift: {case['case_id']}")
        blockers = unique_strings(expected["required_blockers"], "required blockers")
        if blockers != set(base["expected"]["required_deferrals"]):
            raise AssertionError(f"deferral drift: {case['case_id']}")
        relations = unique_strings(expected["required_relations"], "required relations")
        if relations != set(base["expected"]["required_comparison_relations"]):
            raise AssertionError(f"relation drift: {case['case_id']}")
        has_temporal = bool(base["expected"]["required_temporal_features"])
        if expected["temporal_authority"] != (
            "memory.observation_temporal" if has_temporal else "none"
        ):
            raise AssertionError(f"temporal authority drift: {case['case_id']}")
        has_project = "project_knowledge" in lanes
        if expected["project_scope"] != (
            "trusted_existing_project" if has_project else "none"
        ):
            raise AssertionError(f"project scope drift: {case['case_id']}")
        if expected["owner_binding"] != "session_actor_and_owner_manifest":
            raise AssertionError(f"owner binding missing: {case['case_id']}")
        if expected["replay_rule"] != "same_owner_manifest_zero_write":
            raise AssertionError(f"replay rule missing: {case['case_id']}")
        outcome = base["expected"]["outcome"]
        if outcome == "no_observation" and disposition != "no_plan":
            raise AssertionError(f"question created a projection plan: {case['case_id']}")
        if outcome in {"defer_all", "conditional_project"} and disposition != "deferred":
            raise AssertionError(f"required deferral was weakened: {case['case_id']}")
        if outcome in {"extract", "mixed"} and disposition != "manual_review_required":
            raise AssertionError(f"review gate was weakened: {case['case_id']}")
        if has_project and "project_scope_unresolved" in blockers and disposition != "deferred":
            raise AssertionError(f"unresolved project was projected: {case['case_id']}")
        if relations and disposition != "manual_review_required":
            raise AssertionError(f"relation was not reviewed: {case['case_id']}")


def lane_scope(payload: dict[str, Any]) -> dict[str, Any]:
    kind = payload.get("kind")
    if kind == "claim":
        return {}
    if kind == "preference":
        return {
            "preference_class": payload["preference_class"],
            "domain": payload["domain"],
            "preference_key": payload["preference_key"],
            "scope": payload["scope"],
        }
    if kind == "project_knowledge":
        return {
            "project_id": payload["project_id"],
            "component_key": payload["component_key"],
            "binding_source": payload["binding_source"],
            "knowledge_kind": payload["knowledge_kind"],
            "knowledge_key": payload["knowledge_key"],
        }
    raise AssertionError("unknown projection payload kind")


def semantic_key_sha256(
    actor_user_id: str, lane: str, identity: dict[str, Any], payload: dict[str, Any]
) -> str:
    uuid.UUID(actor_user_id)
    canonical = {
        "identity_version": "memory_projection_identity_v5",
        "owner_user_id": actor_user_id,
        "lane": lane,
        "subject_entity_id": identity["subject_entity_id"],
        "predicate": identity["predicate"],
        "object_kind": identity["object_kind"],
        "object_entity_id": identity["object_entity_id"],
        "object_literal_sha256": identity["object_literal_sha256"],
        "polarity": identity["polarity"],
        "modality": identity["modality"],
        "lane_scope": lane_scope(payload),
    }
    return sha256(canonical)


def owner_manifest_sha256(actor_user_id: str, packet_sha256: str) -> str:
    uuid.UUID(actor_user_id)
    if not SHA_RE.fullmatch(packet_sha256):
        raise AssertionError("invalid packet SHA-256")
    return sha256(
        {
            "manifest_version": "memory_projection_owner_manifest_v5",
            "owner_user_id": actor_user_id,
            "packet_sha256": packet_sha256,
        }
    )


def validate_packet(
    packet: dict[str, Any], actor_user_id: str, registry_by_name: dict[str, dict[str, Any]]
) -> None:
    uuid.UUID(actor_user_id)
    if set(packet) != PACKET_KEYS:
        raise AssertionError("packet fields mismatch")
    if packet["contract_version"] != "memory_v1_projection_plan_v5":
        raise AssertionError("packet contract mismatch")
    if packet["predicate_registry_version"] != "memory_predicate_registry_v5":
        raise AssertionError("packet registry mismatch")
    if packet["projection_policy_version"] != "memory_projection_policy_v5":
        raise AssertionError("packet policy mismatch")
    for field in ("projector", "projector_version"):
        if not isinstance(packet[field], str) or not 1 <= len(packet[field].strip()) <= 120:
            raise AssertionError(f"invalid {field}")
    if not isinstance(packet["projections"], list) or len(packet["projections"]) > 100:
        raise AssertionError("invalid projections list")
    expected_packet_hash = sha256({key: value for key, value in packet.items() if key != "packet_sha256"})
    if packet["packet_sha256"] != expected_packet_hash:
        raise AssertionError("packet SHA-256 mismatch")
    seen_refs: set[str] = set()
    for projection in packet["projections"]:
        if not isinstance(projection, dict) or set(projection) != PROJECTION_KEYS:
            raise AssertionError("projection fields mismatch")
        ref = projection["projection_ref"]
        if not re.fullmatch(r"p[0-9]{2}", ref) or ref in seen_refs:
            raise AssertionError("projection reference mismatch")
        seen_refs.add(ref)
        lane = projection["lane"]
        if lane not in {"claim", "preference", "project_knowledge"}:
            raise AssertionError("invalid projection lane")
        inputs = projection["observation_inputs"]
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= 100:
            raise AssertionError("projection requires observation provenance")
        observation_ids: set[str] = set()
        for item in inputs:
            if set(item) != {"observation_id", "observation_sha256", "stance"}:
                raise AssertionError("observation input fields mismatch")
            uuid.UUID(item["observation_id"])
            if item["observation_id"] in observation_ids:
                raise AssertionError("duplicate observation input")
            observation_ids.add(item["observation_id"])
            if not SHA_RE.fullmatch(item["observation_sha256"]):
                raise AssertionError("invalid observation SHA-256")
            if item["stance"] not in {"supports", "opposes", "context", "correction_target"}:
                raise AssertionError("invalid observation stance")
        identity = projection["identity"]
        if set(identity) != IDENTITY_KEYS:
            raise AssertionError("identity fields mismatch")
        uuid.UUID(identity["subject_entity_id"])
        object_entity = identity["object_entity_id"]
        object_literal = identity["object_literal_sha256"]
        if identity["object_kind"] == "entity":
            if object_entity is None or object_literal is not None:
                raise AssertionError("entity object identity mismatch")
            uuid.UUID(object_entity)
        elif identity["object_kind"] == "literal":
            if object_entity is not None or not isinstance(object_literal, str) or not SHA_RE.fullmatch(object_literal):
                raise AssertionError("literal object identity mismatch")
        else:
            raise AssertionError("invalid object kind")
        predicate = identity["predicate"]
        registry_entry = registry_by_name.get(predicate)
        if registry_entry is None:
            raise AssertionError("unregistered projection predicate")
        if identity["polarity"] not in {"affirmed", "negated"}:
            raise AssertionError("invalid observation polarity")
        if identity["modality"] not in registry_entry["modalities"]:
            raise AssertionError("modality is not registered for predicate")
        object_contract = registry_entry["object_contract"]
        if object_contract.startswith("entity.") != (identity["object_kind"] == "entity"):
            raise AssertionError("object kind does not match predicate contract")
        payload = projection["payload"]
        if semantic_key_sha256(actor_user_id, lane, identity, payload) != identity["semantic_key_sha256"]:
            raise AssertionError("semantic key mismatch")
        payload_kind = payload.get("kind")
        expected_kind = {
            "claim": "claim",
            "preference": "preference",
            "project_knowledge": "project_knowledge",
        }.get(lane)
        if payload_kind != expected_kind:
            raise AssertionError("lane payload mismatch")
        if payload_kind == "claim":
            if set(payload) != CLAIM_PAYLOAD_KEYS:
                raise AssertionError("claim payload fields mismatch")
            projection_class = payload["claim_class"]
            if projection_class not in registry_entry["projection_classes"]:
                raise AssertionError("claim class is not registered for predicate")
            if payload["surface_policy"] not in registry_entry["surface_policies"]:
                raise AssertionError("claim surface policy is not registered")
            if projection_class == "never_surface" and payload["surface_policy"] != "never":
                raise AssertionError("never-surface claim policy was weakened")
        elif payload_kind == "preference":
            if set(payload) != PREFERENCE_PAYLOAD_KEYS:
                raise AssertionError("preference payload fields mismatch")
            expected_class = f"{payload['preference_class']}_preference"
            if expected_class not in registry_entry["projection_classes"]:
                raise AssertionError("preference class does not match predicate")
            if payload["surface_policy"] not in registry_entry["surface_policies"]:
                raise AssertionError("preference surface policy is not registered")
            if payload["preference_class"] == "response":
                if payload["surface_policy"] != "zero_token_control_only":
                    raise AssertionError("response preference escaped zero-token control")
                if payload["preference_polarity"] != "not_applicable":
                    raise AssertionError("response preference has life-preference polarity")
            elif payload["surface_policy"] == "zero_token_control_only":
                raise AssertionError("life preference entered response control")
        elif payload_kind == "project_knowledge":
            if set(payload) != PROJECT_PAYLOAD_KEYS:
                raise AssertionError("project payload fields mismatch")
            if "project_knowledge" not in registry_entry["projection_classes"]:
                raise AssertionError("project payload does not match predicate")
            uuid.UUID(payload["project_id"])
            component_key = payload["component_key"]
            binding_source = payload["binding_source"]
            if component_key is None:
                if binding_source not in {
                    "explicit_source_text",
                    "trusted_thread_binding",
                }:
                    raise AssertionError("root project scope has invalid binding source")
            elif (
                not isinstance(component_key, str)
                or not re.fullmatch(r"[a-z][a-z0-9-]{0,99}", component_key)
                or "--" in component_key
                or component_key.endswith("-")
                or binding_source != "trusted_component_registry"
            ):
                raise AssertionError("component project scope is not registry-bound")
            if PROJECT_KIND_FOR_PREDICATE.get(predicate) != payload["knowledge_kind"]:
                raise AssertionError("project knowledge kind mismatch")
            if payload["surface_policy"] != "exact_project_scope_only":
                raise AssertionError("project scope policy was weakened")
        target = projection["target"]
        if set(target) != {"action", "aggregate_id", "expected_revision_number", "reason_codes"}:
            raise AssertionError("target fields mismatch")
        action = target["action"]
        if action not in {"create", "reinforce", "revise", "relate", "defer", "reject"}:
            raise AssertionError("invalid target action")
        aggregate_id = target["aggregate_id"]
        revision = target["expected_revision_number"]
        if action == "create" and (aggregate_id is not None or revision is not None):
            raise AssertionError("create target must not accept durable target IDs")
        if action in {"reinforce", "revise", "relate"}:
            if aggregate_id is None or not isinstance(revision, int) or revision < 1:
                raise AssertionError("existing target requires ID and revision lock")
            uuid.UUID(aggregate_id)
        if action in {"defer", "reject"} and (aggregate_id is not None or revision is not None):
            raise AssertionError("non-apply target contains durable target state")
        reason_codes(target["reason_codes"], "target reasons")
        temporal = projection["temporal_policy"]
        if set(temporal) != {"canonical_source", "materialization", "source_observation_id"}:
            raise AssertionError("temporal policy fields mismatch")
        if temporal["canonical_source"] != "memory.observation_temporal":
            raise AssertionError("observation temporal authority was replaced")
        temporal_source = temporal["source_observation_id"]
        if temporal_source is not None and temporal_source not in observation_ids:
            raise AssertionError("temporal source is not a projection input")
        if temporal["materialization"] == "state_validity_only":
            if lane != "project_knowledge" or temporal_source is None:
                raise AssertionError("unsafe temporal interval materialization")
        elif temporal["materialization"] != "link_only":
            raise AssertionError("unknown temporal materialization")
        review = projection["review"]
        if set(review) != {"state", "authorization_required", "reason_codes"}:
            raise AssertionError("review fields mismatch")
        reason_codes(review["reason_codes"], "review reasons")
        if review["state"] not in {
            "auto_apply_eligible",
            "manual_review_required",
            "deferred",
            "rejected",
        }:
            raise AssertionError("invalid review state")
        if review["state"] == "manual_review_required" and review["authorization_required"] is not True:
            raise AssertionError("manual review lacks authorization gate")
        if review["state"] in {"deferred", "rejected"} and review["authorization_required"] is not False:
            raise AssertionError("non-apply review requests authorization")
        relations = projection["relations"]
        if not isinstance(relations, list) or len(relations) > 20:
            raise AssertionError("invalid relations list")
        if relations and review["state"] != "manual_review_required":
            raise AssertionError("relation mutation bypasses manual review")
        for relation in relations:
            if set(relation) != {
                "relation_type",
                "target_lane",
                "target_aggregate_id",
                "reason_code",
            }:
                raise AssertionError("relation fields mismatch")
            if relation["relation_type"] not in {
                "contradicts",
                "supersedes",
                "corrects",
                "qualifies",
                "depends_on",
                "derived_from",
            }:
                raise AssertionError("invalid relation type")
            if relation["target_lane"] != lane:
                raise AssertionError("cross-lane relation is forbidden")
            uuid.UUID(relation["target_aggregate_id"])
            if not REASON_RE.fullmatch(relation["reason_code"]):
                raise AssertionError("invalid relation reason")
        if payload_kind == "claim" and payload["claim_class"] == "correction":
            relation_types = {item["relation_type"] for item in relations}
            if review["state"] != "manual_review_required" or not relation_types & {"corrects", "supersedes"}:
                raise AssertionError("correction lacks reviewed target relation")


def main() -> int:
    args = arguments()
    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    base_cases = load_jsonl(Path(args.base_cases))
    cases = load_jsonl(Path(args.cases))
    validate_projection_schema(schema)
    validate_registry(registry)
    validate_cases(cases, base_cases)
    print(
        stable_json(
            {
                "case_count": len(cases),
                "predicate_count": len(registry["predicates"]),
                "projection_case_sha256": sha256(cases),
                "projection_schema_sha256": sha256(schema),
                "runtime_active": False,
                "zero_write": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
