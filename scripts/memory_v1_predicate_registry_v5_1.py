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


def _object_kind(contract: dict[str, Any]) -> str:
    value = contract.get("object_contract", "")
    if value.startswith("entity."):
        return "entity"
    if value.startswith("literal."):
        return "literal"
    raise AssertionError(f"unsupported object contract: {contract['predicate']}")


def _storage_cardinality(contract: dict[str, Any]) -> str:
    value = contract["cardinality"]
    if value == "one_active":
        return "one"
    if value in {"one", "many"}:
        return value
    raise AssertionError(f"unsupported cardinality: {contract['predicate']}")


def install_rows(
    integration_path: str | Path = DEFAULT_INTEGRATION,
) -> dict[str, Any]:
    path = Path(integration_path)
    integration = json.loads(path.read_text(encoding="utf-8"))
    composite = build_composite(path)
    payload = composite["payload"]
    contracts: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []

    for contract in payload["active_predicates"]:
        row = {
            "predicate": contract["predicate"],
            "lifecycle": "active",
            "extraction_allowed": True,
            "object_kind": _object_kind(contract),
            "cardinality": _storage_cardinality(contract),
            "successor_predicates": [],
            "description": contract["description"],
            "contract": contract,
            "contract_sha256": sha256_bytes(
                stable_json(contract).encode("utf-8")
            ),
        }
        contracts.append(row)
        relationship = contract.get("relationship_policy")
        if relationship is not None:
            source_policy = relationship["surface_policy"]
            relationships.append(
                {
                    "predicate": contract["predicate"],
                    "family": relationship["family"],
                    "subject_entity_types": relationship[
                        "subject_entity_types"
                    ],
                    "object_entity_types": relationship[
                        "object_entity_types"
                    ],
                    "relation_semantics": relationship[
                        "relation_semantics"
                    ],
                    "canonical_direction": relationship[
                        "canonical_direction"
                    ],
                    "perspective": relationship["perspective"],
                    "temporal_profile": relationship["temporal_profile"],
                    "sensitivity_floor": relationship[
                        "sensitivity_floor"
                    ],
                    "source_surface_policy": source_policy,
                    "observation_surface_policy": integration[
                        "surface_policy_adapter"
                    ][source_policy],
                    "manual_review_rules": relationship[
                        "manual_review_rules"
                    ],
                    "contract": relationship,
                    "contract_sha256": sha256_bytes(
                        stable_json(relationship).encode("utf-8")
                    ),
                }
            )

    for contract in payload["legacy_predicates"]:
        contracts.append(
            {
                "predicate": contract["predicate"],
                "lifecycle": "legacy_read_only",
                "extraction_allowed": False,
                "object_kind": contract["object_kind"],
                "cardinality": contract["cardinality"],
                "successor_predicates": contract["successor_predicates"],
                "description": (
                    "Legacy compatibility predicate retained during cutover."
                ),
                "contract": contract,
                "contract_sha256": sha256_bytes(
                    stable_json(contract).encode("utf-8")
                ),
            }
        )

    contracts.sort(key=lambda row: row["predicate"])
    relationships.sort(key=lambda row: row["predicate"])
    if len(contracts) != 82 or len(relationships) != 41:
        raise AssertionError("install row count mismatch")
    return {
        "registry": {
            "registry_version": payload["registry_version"],
            "contract_version": payload["contract_version"],
            "status": payload["status"],
            "runtime_active": payload["runtime_active"],
            "unknown_predicate_action": payload[
                "unknown_predicate_action"
            ],
            "registry_sha256": composite["registry_sha256"],
        },
        "source_bindings": [
            {
                "source_name": name,
                "source_registry_version": binding["registry_version"],
                "source_path": binding["path"],
                "artifact_sha256": binding["artifact_sha256"],
                "canonical_sha256": binding["canonical_sha256"],
            }
            for name, binding in (
                ("base_registry", integration["base_registry"]),
                (
                    "relationship_registry",
                    integration["relationship_registry"],
                ),
            )
        ],
        "contracts": contracts,
        "relationships": relationships,
    }


def provider_registry(
    integration_path: str | Path = DEFAULT_INTEGRATION,
) -> dict[str, Any]:
    path = Path(integration_path)
    integration = json.loads(path.read_text(encoding="utf-8"))
    root = path.resolve().parents[1]
    base = load_bound_json(root, integration["base_registry"])
    composite = build_composite(path)["payload"]
    return {
        "registry_version": composite["registry_version"],
        "contract_version": composite["contract_version"],
        "status": composite["status"],
        "runtime_active": composite["runtime_active"],
        "unknown_predicate_action": composite["unknown_predicate_action"],
        "object_contracts": base["object_contracts"],
        "predicates": composite["active_predicates"],
        "legacy_compatibility": composite["legacy_predicates"],
    }


def emit_install_sql(
    integration_path: str | Path = DEFAULT_INTEGRATION,
) -> str:
    rows = install_rows(integration_path)
    registry = rows["registry"]
    contract_json = stable_json(rows["contracts"])
    relationship_json = stable_json(rows["relationships"])
    binding_json = stable_json(rows["source_bindings"])
    return f"""-- Generated deterministically by memory_v1_predicate_registry_v5_1.py.
-- Additive registry metadata only. No observations, claims, projections, or Qdrant writes.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SELECT pg_advisory_xact_lock(hashtextextended('memory_predicate_registry_v5_1_install', 0));

CREATE TEMP TABLE v5_1_contract_expected (
  predicate text PRIMARY KEY,
  lifecycle text NOT NULL,
  extraction_allowed boolean NOT NULL,
  object_kind text NOT NULL,
  cardinality text NOT NULL,
  successor_predicates text[] NOT NULL,
  description text NOT NULL,
  contract jsonb NOT NULL,
  contract_sha256 text NOT NULL
) ON COMMIT DROP;

INSERT INTO v5_1_contract_expected
SELECT
  value->>'predicate', value->>'lifecycle',
  (value->>'extraction_allowed')::boolean, value->>'object_kind',
  value->>'cardinality',
  ARRAY(SELECT jsonb_array_elements_text(value->'successor_predicates')),
  value->>'description', value->'contract', value->>'contract_sha256'
FROM jsonb_array_elements($v5_1_contracts${contract_json}$v5_1_contracts$::jsonb);

CREATE TEMP TABLE v5_1_predicate_preexisting ON COMMIT DROP AS
SELECT predicate FROM memory.predicate
WHERE predicate IN (SELECT predicate FROM v5_1_contract_expected);

INSERT INTO memory.predicate(predicate, object_kind, cardinality, description, active)
SELECT predicate, object_kind, cardinality, description, true
FROM v5_1_contract_expected
ON CONFLICT (predicate) DO NOTHING;

INSERT INTO memory.predicate_registry_version(
  registry_version, contract_version, status, runtime_active,
  unknown_predicate_action, registry_sha256
) VALUES (
  '{registry['registry_version']}', '{registry['contract_version']}',
  '{registry['status']}', false, '{registry['unknown_predicate_action']}',
  '{registry['registry_sha256']}'
)
ON CONFLICT (registry_version) DO NOTHING;

INSERT INTO memory.predicate_registry_seed(
  registry_version, predicate, base_predicate_created
)
SELECT
  '{registry['registry_version']}', expected.predicate,
  preexisting.predicate IS NULL
FROM v5_1_contract_expected AS expected
LEFT JOIN v5_1_predicate_preexisting AS preexisting USING (predicate)
ON CONFLICT (registry_version, predicate) DO NOTHING;

INSERT INTO memory.predicate_contract(
  predicate, registry_version, lifecycle, extraction_allowed,
  object_kind, cardinality, successor_predicates, contract, contract_sha256
)
SELECT
  predicate, '{registry['registry_version']}', lifecycle,
  extraction_allowed, object_kind, cardinality, successor_predicates,
  contract, contract_sha256
FROM v5_1_contract_expected
ON CONFLICT (predicate, registry_version) DO NOTHING;

CREATE TABLE IF NOT EXISTS memory.predicate_registry_source_binding_v5_1 (
  registry_version text NOT NULL
    REFERENCES memory.predicate_registry_version(registry_version) ON DELETE RESTRICT,
  source_name text NOT NULL,
  source_registry_version text NOT NULL,
  source_path text NOT NULL,
  artifact_sha256 text NOT NULL,
  canonical_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (registry_version, source_name),
  CHECK (registry_version = 'memory_predicate_registry_v5_1'),
  CHECK (source_name IN ('base_registry', 'relationship_registry')),
  CHECK (btrim(source_registry_version) <> ''),
  CHECK (btrim(source_path) <> ''),
  CHECK (memory.v5_sha256_valid(artifact_sha256)),
  CHECK (memory.v5_sha256_valid(canonical_sha256))
);

INSERT INTO memory.predicate_registry_source_binding_v5_1(
  registry_version, source_name, source_registry_version, source_path,
  artifact_sha256, canonical_sha256
)
SELECT
  '{registry['registry_version']}', value->>'source_name',
  value->>'source_registry_version', value->>'source_path',
  value->>'artifact_sha256', value->>'canonical_sha256'
FROM jsonb_array_elements($v5_1_bindings${binding_json}$v5_1_bindings$::jsonb)
ON CONFLICT (registry_version, source_name) DO NOTHING;

CREATE TABLE IF NOT EXISTS memory.relationship_predicate_contract_v5_1 (
  predicate text NOT NULL,
  registry_version text NOT NULL,
  family text NOT NULL,
  subject_entity_types text[] NOT NULL,
  object_entity_types text[] NOT NULL,
  relation_semantics text NOT NULL,
  canonical_direction text NOT NULL,
  perspective text NOT NULL,
  temporal_profile text NOT NULL,
  sensitivity_floor memory.sensitivity_level NOT NULL,
  source_surface_policy text NOT NULL,
  observation_surface_policy memory.observation_surface_policy NOT NULL,
  manual_review_rules text[] NOT NULL DEFAULT '{{}}',
  contract jsonb NOT NULL,
  contract_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (predicate, registry_version),
  FOREIGN KEY (predicate, registry_version)
    REFERENCES memory.predicate_contract(predicate, registry_version)
    ON DELETE RESTRICT,
  CHECK (registry_version = 'memory_predicate_registry_v5_1'),
  CHECK (family IN (
    'household', 'human_animal', 'kinship_legal',
    'professional_support', 'relational_state', 'social_connection'
  )),
  CHECK (cardinality(subject_entity_types) > 0),
  CHECK (cardinality(object_entity_types) > 0),
  CHECK (subject_entity_types <@ ARRAY['person','self']::text[]),
  CHECK (object_entity_types <@ ARRAY['animal','person','self']::text[]),
  CHECK (array_position(subject_entity_types, NULL) IS NULL),
  CHECK (array_position(object_entity_types, NULL) IS NULL),
  CHECK (relation_semantics IN ('directed', 'symmetric')),
  CHECK (btrim(canonical_direction) <> ''),
  CHECK (perspective IN ('owner_reported_connection', 'owner_reported_state')),
  CHECK (temporal_profile IN (
    'active_interval', 'durable_connection', 'dynamic_state',
    'event_independent_connection'
  )),
  CHECK (source_surface_policy IN (
    'direct_or_relevant', 'explicit_person_or_relationship_context_only',
    'mention_when_directly_relevant', 'restricted_explicit_recall_only'
  )),
  CHECK (array_position(manual_review_rules, NULL) IS NULL),
  CHECK (jsonb_typeof(contract) = 'object'),
  CHECK (memory.v5_sha256_valid(contract_sha256))
);

CREATE OR REPLACE FUNCTION memory.reject_predicate_registry_v5_1_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION 'predicate registry V5.1 metadata is append-only'
    USING ERRCODE = '55000';
END
$function$;

DO $triggers$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'memory.predicate_registry_source_binding_v5_1'::regclass
      AND tgname = 'predicate_registry_source_binding_v5_1_immutable'
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER predicate_registry_source_binding_v5_1_immutable
      BEFORE UPDATE OR DELETE ON memory.predicate_registry_source_binding_v5_1
      FOR EACH ROW EXECUTE FUNCTION memory.reject_predicate_registry_v5_1_mutation();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'memory.relationship_predicate_contract_v5_1'::regclass
      AND tgname = 'relationship_predicate_contract_v5_1_immutable'
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER relationship_predicate_contract_v5_1_immutable
      BEFORE UPDATE OR DELETE ON memory.relationship_predicate_contract_v5_1
      FOR EACH ROW EXECUTE FUNCTION memory.reject_predicate_registry_v5_1_mutation();
  END IF;
END
$triggers$;

INSERT INTO memory.relationship_predicate_contract_v5_1(
  predicate, registry_version, family, subject_entity_types,
  object_entity_types, relation_semantics, canonical_direction,
  perspective, temporal_profile, sensitivity_floor,
  source_surface_policy, observation_surface_policy,
  manual_review_rules, contract, contract_sha256
)
SELECT
  value->>'predicate', '{registry['registry_version']}', value->>'family',
  ARRAY(SELECT jsonb_array_elements_text(value->'subject_entity_types')),
  ARRAY(SELECT jsonb_array_elements_text(value->'object_entity_types')),
  value->>'relation_semantics', value->>'canonical_direction',
  value->>'perspective', value->>'temporal_profile',
  (value->>'sensitivity_floor')::memory.sensitivity_level,
  value->>'source_surface_policy',
  (value->>'observation_surface_policy')::memory.observation_surface_policy,
  ARRAY(SELECT jsonb_array_elements_text(value->'manual_review_rules')),
  value->'contract', value->>'contract_sha256'
FROM jsonb_array_elements($v5_1_relationships${relationship_json}$v5_1_relationships$::jsonb)
ON CONFLICT (predicate, registry_version) DO NOTHING;

DO $verify$
DECLARE
  mismatch_count integer;
BEGIN
  SELECT count(*) INTO mismatch_count
  FROM v5_1_contract_expected AS expected
  JOIN memory.predicate AS base USING (predicate)
  JOIN memory.predicate_contract AS contract
    ON contract.predicate = expected.predicate
   AND contract.registry_version = '{registry['registry_version']}'
  WHERE base.object_kind <> expected.object_kind
     OR base.cardinality <> expected.cardinality
     OR NOT base.active
     OR contract.lifecycle <> expected.lifecycle
     OR contract.extraction_allowed <> expected.extraction_allowed
     OR contract.object_kind <> expected.object_kind
     OR contract.cardinality <> expected.cardinality
     OR contract.successor_predicates <> expected.successor_predicates
     OR contract.contract <> expected.contract
     OR contract.contract_sha256 <> expected.contract_sha256;
  IF mismatch_count <> 0
     OR (SELECT count(*) FROM memory.predicate_contract
         WHERE registry_version = '{registry['registry_version']}') <> 82
     OR (SELECT count(*) FROM memory.predicate_contract
         WHERE registry_version = '{registry['registry_version']}'
           AND lifecycle = 'active' AND extraction_allowed) <> 63
     OR (SELECT count(*) FROM memory.predicate_contract
         WHERE registry_version = '{registry['registry_version']}'
           AND lifecycle = 'legacy_read_only' AND NOT extraction_allowed) <> 19
     OR (SELECT count(*) FROM memory.relationship_predicate_contract_v5_1
         WHERE registry_version = '{registry['registry_version']}') <> 41
     OR (SELECT count(*) FROM memory.predicate_registry_source_binding_v5_1
         WHERE registry_version = '{registry['registry_version']}') <> 2 THEN
    RAISE EXCEPTION 'memory predicate registry V5.1 conflict'
      USING ERRCODE = '23514';
  END IF;
  IF EXISTS (
    SELECT 1 FROM memory.predicate_registry_version
    WHERE registry_version = '{registry['registry_version']}'
      AND (
        contract_version <> '{registry['contract_version']}'
        OR status <> 'proposed'
        OR runtime_active
        OR unknown_predicate_action <> '{registry['unknown_predicate_action']}'
        OR registry_sha256 <> '{registry['registry_sha256']}'
      )
  ) THEN
    RAISE EXCEPTION 'memory predicate registry V5.1 metadata conflict'
      USING ERRCODE = '23514';
  END IF;
END
$verify$;

REVOKE ALL ON memory.predicate_registry_source_binding_v5_1 FROM PUBLIC;
REVOKE ALL ON memory.relationship_predicate_contract_v5_1 FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.reject_predicate_registry_v5_1_mutation() FROM PUBLIC;
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memory_v5_writer') THEN
    GRANT SELECT ON memory.predicate_registry_source_binding_v5_1 TO memory_v5_writer;
    GRANT SELECT ON memory.relationship_predicate_contract_v5_1 TO memory_v5_writer;
  END IF;
END
$grants$;

COMMIT;
"""


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--integration", default=str(DEFAULT_INTEGRATION))
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--emit-install-sql", action="store_true")
    parser.add_argument("--emit-provider-registry", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    if args.emit_install_sql:
        print(emit_install_sql(args.integration), end="")
        return 0
    if args.emit_provider_registry:
        print(stable_json(provider_registry(args.integration)))
        return 0
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
