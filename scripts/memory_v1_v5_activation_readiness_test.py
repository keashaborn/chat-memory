#!/usr/bin/env python3
from __future__ import annotations

from memory_v1_v5_activation_readiness import REQUIRED_APIS, evaluate


TABLES = ["observation", "projection_plan", "preference_head_v5"]


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
        "brains_app_protected_table_grants": [],
        "brains_app_shared_durable_write_grants": (
            [{"table_name": "entity", "privilege_type": "INSERT"}]
            if legacy_writes
            else []
        ),
        "brains_app_controlled_apis": sorted(REQUIRED_APIS),
    }


def main() -> None:
    blocked = evaluate(snapshot(legacy_writes=True), TABLES)
    assert blocked["readiness"]["schema_installed_and_restricted"] is True
    assert blocked["readiness"]["manual_shadow_stage"] is True
    assert blocked["readiness"]["durable_apply"] is False
    assert blocked["readiness"]["shadow_retrieval"] is False
    assert blocked["readiness"]["prompt_influence"] is False

    ready = evaluate(snapshot(legacy_writes=False), TABLES)
    assert ready["readiness"]["durable_apply"] is True

    dirty = snapshot(legacy_writes=False)
    dirty["exact_row_counts"]["observation"] = 1
    dirty_result = evaluate(dirty, TABLES)
    assert dirty_result["readiness"]["manual_shadow_stage"] is False

    exposed = snapshot(legacy_writes=False)
    exposed["brains_app_protected_table_grants"] = [
        {"table_name": "observation", "privilege_type": "SELECT"}
    ]
    exposed_result = evaluate(exposed, TABLES)
    assert exposed_result["readiness"]["schema_installed_and_restricted"] is False

    print("memory_v1_v5_activation_readiness: PASS")


if __name__ == "__main__":
    main()
