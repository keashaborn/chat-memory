#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "ops/sql/20260730_memory_v1_v5_local_claim_projection_registry_guard.sql"
).read_text()

required = (
    "observation.predicate_registry_version='memory_predicate_registry_v5'",
    "assessment.owner_user_id=actor",
    "memory.current_actor_user_id()",
    "SECURITY DEFINER",
    "TO brains_app",
)
for token in required:
    if token not in MIGRATION:
        raise SystemExit(f"missing contract token: {token}")

for forbidden in (
    "memory_predicate_registry_v5_2'",
    "UPDATE memory.observation",
    "DELETE FROM memory.observation",
    "INSERT INTO memory.claim",
):
    if forbidden in MIGRATION:
        raise SystemExit(f"forbidden contract token: {forbidden}")

print("registry_guard_contract=pass")
