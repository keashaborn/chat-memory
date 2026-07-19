#!/usr/bin/env python3
from __future__ import annotations

from copy import deepcopy

from memory_v1_v5_activation_readiness import (
    COMPILER_FUNCTIONS,
    EXPECTED_TIMER_STATES,
    REGISTRY_SHA256,
    REQUIRED_APIS,
    RESTRICTED_ROLES,
    TRACE_TABLES,
    evaluate,
)

OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
CLAIM = "11111111-1111-4111-8111-111111111111"


def fixture() -> tuple[dict, dict, dict, dict, str, list[str], dict]:
    database = {
        "registry": {
            "registry_version": "memory_predicate_registry_v5",
            "registry_sha256": REGISTRY_SHA256,
            "status": "proposed",
            "runtime_active": False,
            "contract_rows": 44,
        },
        "owner_tables": {"count": 105, "not_forced": []},
        "protected_durable_write_grants": [],
        "trace_relations": {
            table: {
                "owner": "memory_v5_trace_writer",
                "row_security": True,
                "force_row_security": True,
                "app_table_privileges": [],
                "columns": ["owner_user_id", "query_sha256", "selected_count"],
                "append_only_trigger": True,
            }
            for table in TRACE_TABLES
        },
        "restricted_roles": {
            role: {
                "can_login": False,
                "inherit": False,
                "bypass_rls": False,
                "superuser": False,
                "create_db": False,
                "create_role": False,
            }
            for role in RESTRICTED_ROLES
        },
        "controlled_apis": sorted(REQUIRED_APIS),
        "compiler_gate": {
            function: {
                "owner": (
                    "memory_v5_local_entity_validation_maintainer"
                    if "entity_validation" in function
                    else "memory_v5_local_disposition_maintainer"
                ),
                "security_definer": True,
                "current_compiler": True,
            }
            for function in COMPILER_FUNCTIONS
        },
        "trace_counts": {
            "claim_total": 10,
            "claim_owner_counts": {OWNER: 10},
            "claim_selected": 2,
            "claim_zero_influence": 10,
            "project_total": 3,
            "project_owner_counts": {OWNER: 3},
            "project_selected": 1,
            "project_zero_influence": 3,
        },
        "governed_counts": {
            "entities": 2,
            "claims": 1,
            "supported_claims": 1,
            "claim_owners": 1,
            "assessments": 1,
            "evidence_links": 1,
        },
        "supported_claims": [{"claim_id": CLAIM, "owner_user_id": OWNER}],
    }
    qdrant = {"points": [{"claim_id": CLAIM, "owner_user_id": OWNER}]}
    env = {
        "MEMORY_V1_V5_SHADOW": "1",
        "MEMORY_V1_V5_SHADOW_TRACE_PERSISTENCE": "1",
        "MEMORY_V1_V5_SHADOW_ALL_AUTHENTICATED": "0",
        "MEMORY_V1_V5_SHADOW_USER_IDS": OWNER,
    }
    timers = {
        unit: {"enabled": state[0], "active": state[1]}
        for unit, state in EXPECTED_TIMER_STATES.items()
    }
    return database, qdrant, env, timers, "active", [], {"clean": True}


def run(values: tuple[dict, dict, dict, dict, str, list[str], dict]) -> dict:
    return evaluate(*values)


def main() -> None:
    ready_values = fixture()
    ready = run(ready_values)
    assert all(ready["checks"].values())
    assert ready["readiness"]["ready_for_multi_owner_zero_influence_shadow"] is True
    assert ready["readiness"]["prompt_influence"] is False
    assert ready["readiness"]["general_account_activation"] is False

    # Ordinary governed growth is diagnostic state, not readiness drift.
    growing = deepcopy(ready_values)
    growing[0]["governed_counts"]["entities"] = 500
    assert run(growing)["readiness"]["ready_for_multi_owner_zero_influence_shadow"] is True

    wrong_owner = deepcopy(ready_values)
    wrong_owner[1]["points"][0]["owner_user_id"] = (
        "557ea042-cb82-48f8-9429-472e96c957ef"
    )
    wrong_result = run(wrong_owner)
    assert wrong_result["checks"]["qdrant_payloads_are_owner_scoped_and_unique"] is True
    assert wrong_result["checks"]["qdrant_exactly_projects_supported_postgres_claims"] is False

    stale_compiler = deepcopy(ready_values)
    stale_compiler[0]["compiler_gate"][
        "plan_owner_v5_local_auto_stage_v1"
    ]["current_compiler"] = False
    assert run(stale_compiler)["checks"]["current_compiler_gate_installed"] is False

    prose_trace = deepcopy(ready_values)
    prose_trace[0]["trace_relations"]["v5_shadow_trace_event"]["columns"].append(
        "query_text"
    )
    assert run(prose_trace)["checks"]["trace_schema_contains_no_prose"] is False

    missing_trace = deepcopy(ready_values)
    missing_trace[2]["MEMORY_V1_V5_SHADOW_USER_IDS"] += ",557ea042-cb82-48f8-9429-472e96c957ef"
    assert run(missing_trace)["checks"]["every_allowlisted_owner_has_claim_traces"] is False

    direct_write = deepcopy(ready_values)
    direct_write[0]["protected_durable_write_grants"] = [
        {"table_name": "claim", "privilege_type": "INSERT"}
    ]
    assert run(direct_write)["checks"][
        "protected_durable_tables_have_no_direct_app_writes"
    ] is False

    print("memory_v1_v5_activation_readiness: PASS")


if __name__ == "__main__":
    main()
