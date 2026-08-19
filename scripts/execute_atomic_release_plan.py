#!/usr/bin/env python3
from __future__ import annotations

"""Deterministic transaction state machine for an authorized release plan.

The state machine has no shell or network adapter. A production adapter must
implement the exact semantic actions and return only content-free evidence.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

if __package__:
    from scripts.control_atomic_release_activation import (
        FORWARD_STEPS,
        PLAN_SCHEMA,
        ROLLBACK_STEPS,
        validate_target_bindings,
    )
else:
    from control_atomic_release_activation import (  # type: ignore[no-redef]
        FORWARD_STEPS,
        PLAN_SCHEMA,
        ROLLBACK_STEPS,
        validate_target_bindings,
    )


EXECUTION_SCHEMA = "seebx-atomic-release-execution-receipt-v1"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")


class ReleaseExecutionError(RuntimeError):
    pass


class ReleaseExecutionFailed(ReleaseExecutionError):
    def __init__(self, receipt: Mapping[str, Any]):
        super().__init__(str(receipt.get("status") or "execution_failed"))
        self.receipt = dict(receipt)


class ReleaseActionDriver(Protocol):
    def execute(self, phase: str, action: Mapping[str, Any]) -> Mapping[str, Any]: ...


def _canonical_sha256(value: Any) -> str:
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return hashlib.sha256(raw).hexdigest()


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReleaseExecutionError("authorization_expiry_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ReleaseExecutionError("authorization_expiry_invalid") from error
    if parsed.tzinfo != timezone.utc:
        raise ReleaseExecutionError("authorization_expiry_invalid")
    return parsed


def _expected_actions(source: tuple[tuple[str, str, str], ...]) -> list[dict[str, Any]]:
    return [
        {"order": index, "action": action, "server": server, "effect": effect}
        for index, (action, server, effect) in enumerate(source, start=1)
    ]


def validate_execution_plan(
    plan: Mapping[str, Any], *, now: datetime | None = None
) -> None:
    expected_keys = {
        "schema_version",
        "status",
        "production_mutated",
        "bindings",
        "forward",
        "rollback_on_any_failure",
        "database_rollback_gate",
        "plan_sha256",
    }
    if set(plan) != expected_keys:
        raise ReleaseExecutionError("plan_fields_invalid")
    if plan["schema_version"] != PLAN_SCHEMA:
        raise ReleaseExecutionError("plan_schema_invalid")
    if plan["status"] != "authorized_not_executed" or plan["production_mutated"] is not False:
        raise ReleaseExecutionError("plan_state_invalid")
    supplied_hash = plan["plan_sha256"]
    unsigned = dict(plan)
    unsigned.pop("plan_sha256")
    if not isinstance(supplied_hash, str) or not HEX64.fullmatch(supplied_hash):
        raise ReleaseExecutionError("plan_hash_invalid")
    if supplied_hash != _canonical_sha256(unsigned):
        raise ReleaseExecutionError("plan_hash_mismatch")
    if plan["forward"] != _expected_actions(FORWARD_STEPS):
        raise ReleaseExecutionError("forward_plan_invalid")
    if plan["rollback_on_any_failure"] != _expected_actions(ROLLBACK_STEPS):
        raise ReleaseExecutionError("rollback_plan_invalid")
    if plan["database_rollback_gate"] != "zep_outbox_empty":
        raise ReleaseExecutionError("database_rollback_gate_invalid")
    bindings = plan["bindings"]
    if not isinstance(bindings, Mapping):
        raise ReleaseExecutionError("plan_bindings_invalid")
    required_bindings = {
        "package_id",
        "package_sha256",
        "authorization_id",
        "authorization_issued_at_utc",
        "authorization_expires_at_utc",
        "backend_commit",
        "frontend_commit",
        "runtime_archive_sha256",
        "database_backup_sha256",
        "migration_package_sha256",
        "targets",
    }
    if set(bindings) != required_bindings:
        raise ReleaseExecutionError("plan_bindings_invalid")
    for name in (
        "package_sha256",
        "runtime_archive_sha256",
        "database_backup_sha256",
        "migration_package_sha256",
    ):
        if not isinstance(bindings[name], str) or not HEX64.fullmatch(bindings[name]):
            raise ReleaseExecutionError("plan_binding_hash_invalid")
    for name in ("backend_commit", "frontend_commit"):
        if not isinstance(bindings[name], str) or not HEX40.fullmatch(bindings[name]):
            raise ReleaseExecutionError("plan_binding_commit_invalid")
    try:
        validate_target_bindings(bindings["targets"])
    except Exception as error:
        raise ReleaseExecutionError("plan_binding_targets_invalid") from error
    current = now or datetime.now(timezone.utc)
    issued = _parse_utc(bindings["authorization_issued_at_utc"])
    expires = _parse_utc(bindings["authorization_expires_at_utc"])
    if issued > current or expires <= issued or current >= expires:
        raise ReleaseExecutionError("authorization_expired")


def _run_action(
    driver: ReleaseActionDriver,
    *,
    phase: str,
    action: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        result = driver.execute(phase, action)
    except Exception as error:
        raise ReleaseExecutionError(f"{phase}_action_failed:{action['action']}") from error
    if set(result) != {"status", "evidence_sha256"}:
        raise ReleaseExecutionError(f"{phase}_evidence_invalid:{action['action']}")
    evidence = result["evidence_sha256"]
    if result["status"] != "pass" or not isinstance(evidence, str) or not HEX64.fullmatch(evidence):
        raise ReleaseExecutionError(f"{phase}_evidence_invalid:{action['action']}")
    return {
        "order": action["order"],
        "action": action["action"],
        "server": action["server"],
        "effect": action["effect"],
        "status": "pass",
        "evidence_sha256": evidence,
    }


def execute_authorized_plan(
    plan: Mapping[str, Any],
    driver: ReleaseActionDriver,
    *,
    run_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not RUN_ID.fullmatch(run_id):
        raise ReleaseExecutionError("run_id_invalid")
    validate_execution_plan(plan, now=now)
    forward_events: list[dict[str, Any]] = []
    rollback_events: list[dict[str, Any]] = []
    rollback_errors: list[str] = []
    production_mutated = False
    failed_action: str | None = None
    try:
        for action in plan["forward"]:
            event = _run_action(driver, phase="forward", action=action)
            forward_events.append(event)
            if action["effect"] == "write":
                production_mutated = True
    except ReleaseExecutionError as error:
        failed_action = str(error).split(":", 1)[-1]
        if production_mutated:
            for action in plan["rollback_on_any_failure"]:
                try:
                    rollback_events.append(
                        _run_action(driver, phase="rollback", action=action)
                    )
                except ReleaseExecutionError as rollback_error:
                    rollback_errors.append(str(rollback_error))
        status = (
            "rollback_failed"
            if rollback_errors
            else "rolled_back"
            if production_mutated
            else "preflight_failed"
        )
        receipt: dict[str, Any] = {
            "schema_version": EXECUTION_SCHEMA,
            "run_id": run_id,
            "status": status,
            "plan_sha256": plan["plan_sha256"],
            "authorization_id": plan["bindings"]["authorization_id"],
            "failed_action": failed_action,
            "production_mutated": production_mutated,
            "forward_events": forward_events,
            "rollback_events": rollback_events,
            "rollback_errors": rollback_errors,
        }
        receipt["receipt_sha256"] = _canonical_sha256(receipt)
        raise ReleaseExecutionFailed(receipt) from error
    receipt = {
        "schema_version": EXECUTION_SCHEMA,
        "run_id": run_id,
        "status": "activated",
        "plan_sha256": plan["plan_sha256"],
        "authorization_id": plan["bindings"]["authorization_id"],
        "failed_action": None,
        "production_mutated": production_mutated,
        "forward_events": forward_events,
        "rollback_events": rollback_events,
        "rollback_errors": rollback_errors,
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    return receipt
