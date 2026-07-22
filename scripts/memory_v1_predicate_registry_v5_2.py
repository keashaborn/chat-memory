from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "specs/memory_v1_predicate_registry_v5_2.json"
REGISTRY_ARTIFACT_SHA256 = (
    "e6ac5dfe7d7939aac23223ae76272b2e4f67777decde814d9bf0b8eee82b277e"
)
REGISTRY_CANONICAL_SHA256 = (
    "446c0f9df2e3f12bab90cea5b056c5dc3da09a81c6d7ffd9c62252cdcf6c4876"
)
SURFACE_POLICY_ADAPTER = {
    "direct_or_relevant": "direct_or_relevant",
    "explicit_person_or_relationship_context_only": "explicit_recall_only",
    "mention_when_directly_relevant": "mention_when_directly_relevant",
    "restricted_explicit_recall_only": "explicit_recall_only",
}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_registry(registry_path: str | Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    path = Path(registry_path)
    raw = path.read_bytes()
    if sha256_bytes(raw) != REGISTRY_ARTIFACT_SHA256:
        raise AssertionError("V5.2 registry artifact hash mismatch")
    value = json.loads(raw)
    canonical = sha256_bytes(stable_json(value).encode("utf-8"))
    if canonical != REGISTRY_CANONICAL_SHA256:
        raise AssertionError("V5.2 registry canonical hash mismatch")
    if (
        value.get("registry_version") != "memory_predicate_registry_v5_2"
        or value.get("contract_version")
        != "memory_v1_relational_extraction_v5_2"
        or value.get("status") != "proposed"
        or value.get("runtime_active") is not False
        or value.get("unknown_predicate_action")
        != "defer_unregistered_predicate"
    ):
        raise AssertionError("V5.2 registry metadata changed")
    return value


def build_composite(
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    registry = load_registry(registry_path)
    active = {row["predicate"]: row for row in registry["predicates"]}
    legacy = {
        row["predicate"]: row for row in registry["legacy_compatibility"]
    }
    if set(active) & set(legacy):
        raise AssertionError("active and legacy predicate overlap")
    payload = {
        "registry_version": registry["registry_version"],
        "contract_version": registry["contract_version"],
        "status": registry["status"],
        "runtime_active": registry["runtime_active"],
        "unknown_predicate_action": registry["unknown_predicate_action"],
        "active_predicates": [active[name] for name in sorted(active)],
        "legacy_predicates": [legacy[name] for name in sorted(legacy)],
    }
    counts = {
        "active_predicate_count": len(payload["active_predicates"]),
        "legacy_predicate_count": len(payload["legacy_predicates"]),
        "total_contract_count": len(payload["active_predicates"])
        + len(payload["legacy_predicates"]),
    }
    if counts != {
        "active_predicate_count": 66,
        "legacy_predicate_count": 19,
        "total_contract_count": 85,
    }:
        raise AssertionError("V5.2 registry counts changed")
    return {
        "payload": payload,
        "registry_sha256": REGISTRY_CANONICAL_SHA256,
        "counts": counts,
        "hash_bound": True,
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
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    path = Path(registry_path)
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
                    "observation_surface_policy": SURFACE_POLICY_ADAPTER[
                        source_policy
                    ],
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
    if len(contracts) != 85 or len(relationships) != 41:
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
        "source_bindings": [{
            "source_name": "canonical_registry",
            "source_registry_version": payload["registry_version"],
            "source_path": "specs/memory_v1_predicate_registry_v5_2.json",
            "artifact_sha256": REGISTRY_ARTIFACT_SHA256,
            "canonical_sha256": REGISTRY_CANONICAL_SHA256,
        }],
        "contracts": contracts,
        "relationships": relationships,
    }


def provider_registry(
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    return load_registry(registry_path)


def emit_install_sql(
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> str:
    rows = install_rows(registry_path)
    registry = rows["registry"]
    contract_json = stable_json(rows["contracts"])
    relationship_json = stable_json(rows["relationships"])
    binding_json = stable_json(rows["source_bindings"])
    return f"""-- Generated deterministically by memory_v1_predicate_registry_v5_2.py.
-- Additive registry metadata only. No observations, claims, projections, or Qdrant writes.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SELECT pg_advisory_xact_lock(hashtextextended('memory_predicate_registry_v5_2_install', 0));

CREATE TEMP TABLE v5_2_contract_expected (
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

INSERT INTO v5_2_contract_expected
SELECT
  value->>'predicate', value->>'lifecycle',
  (value->>'extraction_allowed')::boolean, value->>'object_kind',
  value->>'cardinality',
  ARRAY(SELECT jsonb_array_elements_text(value->'successor_predicates')),
  value->>'description', value->'contract', value->>'contract_sha256'
FROM jsonb_array_elements($v5_2_contracts${contract_json}$v5_2_contracts$::jsonb);

CREATE TEMP TABLE v5_2_predicate_preexisting ON COMMIT DROP AS
SELECT predicate FROM memory.predicate
WHERE predicate IN (SELECT predicate FROM v5_2_contract_expected);

INSERT INTO memory.predicate(predicate, object_kind, cardinality, description, active)
SELECT predicate, object_kind, cardinality, description, true
FROM v5_2_contract_expected
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
FROM v5_2_contract_expected AS expected
LEFT JOIN v5_2_predicate_preexisting AS preexisting USING (predicate)
ON CONFLICT (registry_version, predicate) DO NOTHING;

INSERT INTO memory.predicate_contract(
  predicate, registry_version, lifecycle, extraction_allowed,
  object_kind, cardinality, successor_predicates, contract, contract_sha256
)
SELECT
  predicate, '{registry['registry_version']}', lifecycle,
  extraction_allowed, object_kind, cardinality, successor_predicates,
  contract, contract_sha256
FROM v5_2_contract_expected
ON CONFLICT (predicate, registry_version) DO NOTHING;

CREATE TABLE IF NOT EXISTS memory.predicate_registry_source_binding_v5_2 (
  registry_version text NOT NULL
    REFERENCES memory.predicate_registry_version(registry_version) ON DELETE RESTRICT,
  source_name text NOT NULL,
  source_registry_version text NOT NULL,
  source_path text NOT NULL,
  artifact_sha256 text NOT NULL,
  canonical_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (registry_version, source_name),
  CHECK (registry_version = 'memory_predicate_registry_v5_2'),
  CHECK (source_name = 'canonical_registry'),
  CHECK (btrim(source_registry_version) <> ''),
  CHECK (btrim(source_path) <> ''),
  CHECK (memory.v5_sha256_valid(artifact_sha256)),
  CHECK (memory.v5_sha256_valid(canonical_sha256))
);

INSERT INTO memory.predicate_registry_source_binding_v5_2(
  registry_version, source_name, source_registry_version, source_path,
  artifact_sha256, canonical_sha256
)
SELECT
  '{registry['registry_version']}', value->>'source_name',
  value->>'source_registry_version', value->>'source_path',
  value->>'artifact_sha256', value->>'canonical_sha256'
FROM jsonb_array_elements($v5_2_bindings${binding_json}$v5_2_bindings$::jsonb)
ON CONFLICT (registry_version, source_name) DO NOTHING;

CREATE TABLE IF NOT EXISTS memory.relationship_predicate_contract_v5_2 (
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
  CHECK (registry_version = 'memory_predicate_registry_v5_2'),
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

CREATE OR REPLACE FUNCTION memory.reject_predicate_registry_v5_2_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
  RAISE EXCEPTION 'predicate registry V5.2 metadata is append-only'
    USING ERRCODE = '55000';
END
$function$;

DO $triggers$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'memory.predicate_registry_source_binding_v5_2'::regclass
      AND tgname = 'predicate_registry_source_binding_v5_2_immutable'
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER predicate_registry_source_binding_v5_2_immutable
      BEFORE UPDATE OR DELETE ON memory.predicate_registry_source_binding_v5_2
      FOR EACH ROW EXECUTE FUNCTION memory.reject_predicate_registry_v5_2_mutation();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'memory.relationship_predicate_contract_v5_2'::regclass
      AND tgname = 'relationship_predicate_contract_v5_2_immutable'
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER relationship_predicate_contract_v5_2_immutable
      BEFORE UPDATE OR DELETE ON memory.relationship_predicate_contract_v5_2
      FOR EACH ROW EXECUTE FUNCTION memory.reject_predicate_registry_v5_2_mutation();
  END IF;
END
$triggers$;

INSERT INTO memory.relationship_predicate_contract_v5_2(
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
FROM jsonb_array_elements($v5_2_relationships${relationship_json}$v5_2_relationships$::jsonb)
ON CONFLICT (predicate, registry_version) DO NOTHING;

DO $verify$
DECLARE
  mismatch_count integer;
BEGIN
  SELECT count(*) INTO mismatch_count
  FROM v5_2_contract_expected AS expected
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
         WHERE registry_version = '{registry['registry_version']}') <> 85
     OR (SELECT count(*) FROM memory.predicate_contract
         WHERE registry_version = '{registry['registry_version']}'
           AND lifecycle = 'active' AND extraction_allowed) <> 66
     OR (SELECT count(*) FROM memory.predicate_contract
         WHERE registry_version = '{registry['registry_version']}'
           AND lifecycle = 'legacy_read_only' AND NOT extraction_allowed) <> 19
     OR (SELECT count(*) FROM memory.relationship_predicate_contract_v5_2
         WHERE registry_version = '{registry['registry_version']}') <> 41
     OR (SELECT count(*) FROM memory.predicate_registry_source_binding_v5_2
         WHERE registry_version = '{registry['registry_version']}') <> 1 THEN
    RAISE EXCEPTION 'memory predicate registry V5.2 conflict'
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
    RAISE EXCEPTION 'memory predicate registry V5.2 metadata conflict'
      USING ERRCODE = '23514';
  END IF;
END
$verify$;

REVOKE ALL ON memory.predicate_registry_source_binding_v5_2 FROM PUBLIC;
REVOKE ALL ON memory.relationship_predicate_contract_v5_2 FROM PUBLIC;
REVOKE ALL ON FUNCTION memory.reject_predicate_registry_v5_2_mutation() FROM PUBLIC;
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memory_v5_writer') THEN
    GRANT SELECT ON memory.predicate_registry_source_binding_v5_2 TO memory_v5_writer;
    GRANT SELECT ON memory.relationship_predicate_contract_v5_2 TO memory_v5_writer;
  END IF;
END
$grants$;

COMMIT;
"""


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--emit-install-sql", action="store_true")
    parser.add_argument("--emit-provider-registry", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    if args.emit_install_sql:
        print(emit_install_sql(args.registry), end="")
        return 0
    if args.emit_provider_registry:
        print(stable_json(provider_registry(args.registry)))
        return 0
    result = build_composite(args.registry)
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
