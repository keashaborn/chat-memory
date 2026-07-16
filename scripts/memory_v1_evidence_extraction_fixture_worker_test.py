#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

from scripts.memory_v1_evidence_extraction_fixture_worker import (
    canonical_owners,
    load_fixture,
    operation_id,
    plan_report,
    validate_limits,
    worker_reference,
)


OWNER_A = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OWNER_B = "557ea042-cb82-48f8-9429-472e96c957ef"
RUN_ID = "f1111111-1111-4111-8111-111111111111"


def args(**updates):
    values = {
        "route": "relational_extraction",
        "run_id": RUN_ID,
        "lease_seconds": 300,
        "max_attempts": 3,
        "max_jobs": 1,
        "verify_replay": False,
        "apply_fixture": False,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def expect_error(callback, message: str) -> None:
    try:
        callback()
    except Exception:
        return
    raise AssertionError(message)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    fixture_path = (
        repo_root
        / "tests"
        / "fixtures"
        / "memory_v1_evidence_extraction_fixture_v1.json"
    )
    fixture_sha256 = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    fixture = load_fixture(str(fixture_path), fixture_sha256)
    if fixture.route != "relational_extraction":
        raise AssertionError("fixture route changed")
    if fixture.finish_status != "review_required":
        raise AssertionError("fixture finish status changed")

    expect_error(
        lambda: load_fixture(str(fixture_path), "0" * 64),
        "fixture hash mismatch was accepted",
    )
    changed = json.loads(fixture_path.read_text())
    changed["unexpected"] = True
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
        json.dump(changed, handle)
        handle.flush()
        changed_sha256 = hashlib.sha256(Path(handle.name).read_bytes()).hexdigest()
        expect_error(
            lambda: load_fixture(handle.name, changed_sha256),
            "fixture with unexpected keys was accepted",
        )

    expect_error(
        lambda: canonical_owners([]),
        "missing owner allowlist was accepted",
    )
    owners = canonical_owners([OWNER_B, OWNER_A, OWNER_A])
    if [str(owner) for owner in owners] != [OWNER_A, OWNER_B]:
        raise AssertionError("owner allowlist was not sorted and deduplicated")

    run_id = validate_limits(args(), fixture)
    if run_id != uuid.UUID(RUN_ID):
        raise AssertionError("run id was not canonicalized")
    for invalid_args in (
        args(route="artifact_assessment"),
        args(run_id="not-a-uuid"),
        args(lease_seconds=29),
        args(lease_seconds=3601),
        args(max_attempts=0),
        args(max_attempts=21),
        args(max_jobs=0),
        args(max_jobs=11),
        args(verify_replay=True),
    ):
        expect_error(
            lambda invalid_args=invalid_args: validate_limits(
                invalid_args,
                fixture,
            ),
            f"invalid limits were accepted: {invalid_args}",
        )

    if worker_reference("fixture-worker-test") != "fixture-worker-test":
        raise AssertionError("explicit worker id changed")
    expect_error(
        lambda: worker_reference(""),
        "empty worker id was accepted",
    )

    first = operation_id(run_id, f"{owners[0]}:claim:0")
    second = operation_id(run_id, f"{owners[0]}:claim:0")
    different = operation_id(run_id, f"{owners[0]}:claim:1")
    if first != second or first == different:
        raise AssertionError("operation ids are not deterministic")

    report = plan_report(
        fixture=fixture,
        owners=owners,
        run_id=run_id,
        worker_id="fixture-worker-test",
    )
    if report["apply"] is not False or report["fixture_only"] is not True:
        raise AssertionError("plan report is not fail-closed")
    for key in ("model_calls", "candidate_writes", "claim_writes", "staging_writes"):
        if report[key] != 0:
            raise AssertionError(f"plan report {key} is not zero")

    print("memory_v1_evidence_extraction_fixture_worker_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
