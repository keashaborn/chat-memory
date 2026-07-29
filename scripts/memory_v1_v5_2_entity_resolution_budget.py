#!/usr/bin/env python3
"""Verify exact append-only table deltas for one V5.2 entity batch."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any


class EntityResolutionBudgetError(RuntimeError):
    pass


def expected_table_deltas(plan: dict[str, Any]) -> dict[str, int]:
    items = [entry["manifest_item"] for entry in plan["items"]]
    operations = Counter(item["operation"] for item in items)
    unknown = set(operations) - {
        "auto_apply",
        "manual_create_new_and_apply",
        "manual_link_existing_and_apply",
        "reconcile_existing_and_apply",
    }
    if unknown:
        raise EntityResolutionBudgetError(
            f"unsupported entity operations: {sorted(unknown)}"
        )
    reviewed = (
        operations["manual_create_new_and_apply"]
        + operations["manual_link_existing_and_apply"]
        + operations["reconcile_existing_and_apply"]
    )
    reconciled = operations["reconcile_existing_and_apply"]
    expected = {
        "entity": operations["manual_create_new_and_apply"],
        "entity_resolution_reconciliation_v5_2": reconciled,
        "entity_resolution_plan": reconciled,
        "entity_resolution_candidate": reconciled,
        "entity_alias_observation": reviewed,
        "entity_resolution_review": reviewed,
        "entity_resolution_apply": len(items),
        "observation_entity_binding": int(plan["expected_total_bindings"]),
        "relational_operation_request": len(items) + reviewed,
    }
    if sum(expected.values()) != int(plan["expected_new_rows"]):
        raise EntityResolutionBudgetError(
            "plan row budget does not match mapped entity tables"
        )
    return expected


def load_state(path: Path) -> dict[str, tuple[int, str]]:
    rows: dict[str, tuple[int, str]] = {}
    for line in path.read_text().splitlines():
        table, count, digest = line.split("\t")
        rows[table] = (int(count), digest)
    return rows


def verify_table_deltas(
    plan: dict[str, Any],
    before: dict[str, tuple[int, str]],
    after: dict[str, tuple[int, str]],
) -> None:
    if before.keys() != after.keys():
        raise EntityResolutionBudgetError("target-owner table set changed")
    expected = expected_table_deltas(plan)
    for table in before:
        delta = after[table][0] - before[table][0]
        wanted = expected.get(table, 0)
        if delta != wanted:
            raise EntityResolutionBudgetError(
                f"unexpected target-owner delta {table}: {delta} != {wanted}"
            )
        if wanted == 0 and before[table][1] != after[table][1]:
            raise EntityResolutionBudgetError(
                f"unexpected target-owner mutation {table}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    args = parser.parse_args()
    plan = json.loads(Path(args.plan).read_text())
    verify_table_deltas(
        plan,
        load_state(Path(args.before)),
        load_state(Path(args.after)),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
