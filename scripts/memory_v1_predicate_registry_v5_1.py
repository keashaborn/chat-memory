from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INTEGRATION = (
    ROOT / "specs/memory_v1_predicate_registry_v5_1_integration.json"
)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_bound_json(root: Path, binding: dict[str, Any]) -> dict[str, Any]:
    path = root / binding["path"]
    raw = path.read_bytes()
    if sha256_bytes(raw) != binding["artifact_sha256"]:
        raise AssertionError(f"artifact hash mismatch: {binding['path']}")
    value = json.loads(raw)
    canonical = sha256_bytes(stable_json(value).encode("utf-8"))
    if canonical != binding["canonical_sha256"]:
        raise AssertionError(f"canonical hash mismatch: {binding['path']}")
    if value["registry_version"] != binding["registry_version"]:
        raise AssertionError(f"registry version mismatch: {binding['path']}")
    return value


def relationship_contract(
    row: dict[str, Any], adapter: dict[str, str]
) -> dict[str, Any]:
    object_types = set(row["object_entity_types"])
    if object_types == {"animal"}:
        object_contract = "entity.animal"
    elif object_types == {"person"}:
        object_contract = "entity.person"
    elif object_types == {"person", "self"}:
        object_contract = "entity.person_or_self"
    else:
        raise AssertionError(f"unsupported relationship object types: {row['predicate']}")
    if row["surface_policy"] not in adapter:
        raise AssertionError(f"surface policy is not adapted: {row['predicate']}")
    social_state = row["family"] == "relational_state"
    return {
        "predicate": row["predicate"],
        "subject_entity_types": row["subject_entity_types"],
        "object_contract": object_contract,
        "cardinality": "many",
        "relation_semantics": row["relation_semantics"],
        "temporal_semantics": ["state_validity"],
        "modalities": (
            ["reported_observation", "uncertain", "corrective"]
            if social_state
            else ["asserted", "negated", "uncertain", "corrective"]
        ),
        "projection_classes": ["direct_claim", "supportive_context"],
        "sensitivity_floor": row["sensitivity_floor"],
        "surface_policies": [adapter[row["surface_policy"]]],
        "manual_review_rules": row["manual_review_rules"],
        "description": row["description"],
        "relationship_policy": row,
    }


def build_composite(
    integration_path: str | Path = DEFAULT_INTEGRATION,
) -> dict[str, Any]:
    path = Path(integration_path)
    integration = json.loads(path.read_text(encoding="utf-8"))
    if integration["status"] != "proposed" or integration["runtime_active"] is not False:
        raise AssertionError("integration must remain proposed and runtime-disabled")
    root = path.resolve().parents[1]
    base = load_bound_json(root, integration["base_registry"])
    relationships = load_bound_json(root, integration["relationship_registry"])
    if base["runtime_active"] is not False or relationships["runtime_active"] is not False:
        raise AssertionError("source registries must remain runtime-disabled")

    replacements = set(integration["replacement_predicates"])
    base_active = {row["predicate"]: row for row in base["predicates"]}
    relationship_active = {
        row["predicate"]: relationship_contract(
            row, integration["surface_policy_adapter"]
        )
        for row in relationships["predicates"]
    }
    overlap = set(base_active) & set(relationship_active)
    if overlap != replacements:
        raise AssertionError("relationship replacement set mismatch")
    active = {
        name: row for name, row in base_active.items() if name not in replacements
    }
    active.update(relationship_active)
    legacy = {row["predicate"]: row for row in base["legacy_compatibility"]}
    if set(active) & set(legacy):
        raise AssertionError("active and legacy predicate overlap")

    result = integration["result_registry"]
    payload = {
        "registry_version": result["registry_version"],
        "contract_version": result["contract_version"],
        "status": result["status"],
        "runtime_active": result["runtime_active"],
        "unknown_predicate_action": result["unknown_predicate_action"],
        "source_bindings": {
            "base_registry_canonical_sha256": integration["base_registry"][
                "canonical_sha256"
            ],
            "relationship_registry_canonical_sha256": integration[
                "relationship_registry"
            ]["canonical_sha256"],
        },
        "surface_policy_adapter": integration["surface_policy_adapter"],
        "active_predicates": [active[name] for name in sorted(active)],
        "legacy_predicates": [legacy[name] for name in sorted(legacy)],
        "installation_constraints": integration["installation_constraints"],
    }
    counts = {
        "active_predicate_count": len(payload["active_predicates"]),
        "legacy_predicate_count": len(payload["legacy_predicates"]),
        "total_contract_count": len(payload["active_predicates"])
        + len(payload["legacy_predicates"]),
    }
    for key, value in counts.items():
        if result[key] != value:
            raise AssertionError(f"result count mismatch: {key}")
    digest = sha256_bytes(stable_json(payload).encode("utf-8"))
    expected = result["expected_canonical_sha256"]
    if expected != "PENDING" and digest != expected:
        raise AssertionError("result registry canonical hash mismatch")
    return {
        "payload": payload,
        "registry_sha256": digest,
        "counts": counts,
        "hash_bound": expected != "PENDING",
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--integration", default=str(DEFAULT_INTEGRATION))
    parser.add_argument("--full", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    result = build_composite(args.integration)
    output = result if args.full else {
        "counts": result["counts"],
        "hash_bound": result["hash_bound"],
        "registry_sha256": result["registry_sha256"],
        "registry_version": result["payload"]["registry_version"],
        "runtime_active": result["payload"]["runtime_active"],
    }
    print(stable_json(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
