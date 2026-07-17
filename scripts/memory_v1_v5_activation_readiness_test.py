#!/usr/bin/env python3
from __future__ import annotations

from memory_v1_v5_activation_readiness import REQUIRED_APIS, evaluate


TABLES = ["observation", "projection_plan", "preference_head_v5"]
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


def expected_state(
    *, counts: dict[str, int] | None = None, owner_counts: dict[str, int] | None = None
) -> dict:
    return {
        "contract_version": "memory_v1_v5_governed_state_baseline_v1",
        "source_commit": "a" * 40,
        "exact_row_counts": counts or {table: 0 for table in TABLES},
        "owner_row_counts": owner_counts or {},
    }


def snapshot(*, legacy_writes: bool) -> dict:
    return {
        "expected_registry": {
            "registry_version": "memory_predicate_registry_v5",
            "registry_sha256": "a" * 64,
            "status": "proposed",
            "runtime_active": False,
            "expected_contract_rows": 44,
        },
        "registry": {
            "registry_version": "memory_predicate_registry_v5",
            "registry_sha256": "a" * 64,
            "status": "proposed",
            "runtime_active": False,
            "contract_rows": 44,
        },
        "writer_role": {
            "rolcanlogin": False,
            "rolinherit": False,
            "rolbypassrls": False,
            "rolsuper": False,
            "rolcreatedb": False,
            "rolcreaterole": False,
        },
        "relations": {
            table: {
                "relname": table,
                "relrowsecurity": True,
                "relforcerowsecurity": True,
            }
            for table in TABLES
        },
        "exact_row_counts": {table: 0 for table in TABLES},
        "owner_row_counts": {},
        "brains_app_protected_table_grants": [],
        "brains_app_shared_durable_write_grants": (
            [{"table_name": "entity", "privilege_type": "INSERT"}]
            if legacy_writes
            else []
        ),
        "brains_app_controlled_apis": sorted(REQUIRED_APIS),
    }


def main() -> None:
    baseline = expected_state()
    blocked = evaluate(snapshot(legacy_writes=True), TABLES, baseline)
    assert blocked["readiness"]["schema_installed_and_restricted"] is True
    assert blocked["readiness"]["manual_shadow_stage"] is True
    assert blocked["readiness"]["durable_apply"] is False
    assert blocked["readiness"]["shadow_retrieval"] is False
    assert blocked["readiness"]["prompt_influence"] is False

    ready = evaluate(snapshot(legacy_writes=False), TABLES, baseline)
    assert ready["readiness"]["durable_apply"] is True

    dirty = snapshot(legacy_writes=False)
    dirty["exact_row_counts"]["observation"] = 1
    dirty["owner_row_counts"] = {OWNER: 1}
    dirty_result = evaluate(dirty, TABLES, baseline)
    assert dirty_result["readiness"]["manual_shadow_stage"] is False
    assert dirty_result["blockers"]["manual_shadow_stage"] == [
        "governed_v5_state_matches_baseline"
    ]

    populated_counts = {table: 0 for table in TABLES}
    populated_counts["observation"] = 1
    populated = snapshot(legacy_writes=False)
    populated["exact_row_counts"] = populated_counts
    populated["owner_row_counts"] = {OWNER: 1}
    populated_result = evaluate(
        populated,
        TABLES,
        expected_state(counts=populated_counts, owner_counts={OWNER: 1}),
    )
    assert populated_result["readiness"]["manual_shadow_stage"] is True

    exposed = snapshot(legacy_writes=False)
    exposed["brains_app_protected_table_grants"] = [
        {"table_name": "observation", "privilege_type": "SELECT"}
    ]
    exposed_result = evaluate(exposed, TABLES, baseline)
    assert exposed_result["readiness"]["schema_installed_and_restricted"] is False

    print("memory_v1_v5_activation_readiness: PASS")


if __name__ == "__main__":
    main()
