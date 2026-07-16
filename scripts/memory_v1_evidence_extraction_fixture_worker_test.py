#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from scripts.memory_v1_evidence_extraction_fixture_worker import (
    canonical_owners,
    extraction_checkpoint,
    load_fixture,
    operation_id,
    plan_report,
    validate_limits,
    worker_reference,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    load_registry,
    load_schema,
)


OWNER_A = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OWNER_B = "557ea042-cb82-48f8-9429-472e96c957ef"
RUN_ID = "f1111111-1111-4111-8111-111111111111"
REGISTRY_SHA256 = (
    "4837cc66f8ef41d5b091528c02e06add267586cb170dc0eb4b57fc207bd0f3d8"
)
SCHEMA_SHA256 = (
    "c1d613b16795c181780d60219860f8068cee1369b069735db94887b0c1b8b377"
)


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
    if fixture.provider_id != "synthetic_fixture":
        raise AssertionError("fixture provider is not synthetic")

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
        registry_sha256=REGISTRY_SHA256,
        schema_sha256=SCHEMA_SHA256,
    )
    if report["apply"] is not False or report["fixture_only"] is not True:
        raise AssertionError("plan report is not fail-closed")
    for key in (
        "model_calls",
        "candidate_writes",
        "claim_writes",
        "staging_writes",
        "qdrant_writes",
    ):
        if report[key] != 0:
            raise AssertionError(f"plan report {key} is not zero")

    registry = load_registry(
        repo_root / "specs" / "memory_v1_predicate_registry_v5.json",
        REGISTRY_SHA256,
    )
    schema = load_schema(
        repo_root / "specs" / "memory_v1_relational_extraction_v5.schema.json",
        SCHEMA_SHA256,
    )
    checkpoint, finish = extraction_checkpoint(
        fixture=fixture,
        job={
            "job_id": uuid.UUID("e4444444-4444-4444-8444-444444444444"),
            "evidence_id": uuid.UUID(
                "e5555555-5555-4555-8555-555555555555"
            ),
            "evidence_source_system": "public.chat_log",
            "evidence_external_id": "e3333333-3333-4333-8333-333333333333",
            "evidence_content_sha256": fixture.source_content_sha256,
            "evidence_content": "Synthetic fixture-only extraction source.",
            "evidence_recorded_at": datetime(
                2026,
                7,
                16,
                20,
                30,
                tzinfo=timezone.utc,
            ),
        },
        registry=registry,
        schema=schema,
    )
    packet = checkpoint["normalized_packet"]
    if packet["source_envelope"]["job_id"] != (
        "e4444444-4444-4444-8444-444444444444"
    ):
        raise AssertionError("checkpoint is not bound to the claimed job")
    if packet["source_envelope"]["source_external_id"] != (
        "e3333333-3333-4333-8333-333333333333"
    ):
        raise AssertionError("checkpoint is not bound to the trusted source")
    if packet["deferrals"][0]["review_required"] is not False:
        raise AssertionError("insufficient-evidence deferral changed review policy")
    if "quote" in json.dumps(packet, sort_keys=True):
        raise AssertionError("normalized checkpoint retained raw span quotes")
    if finish["normalized_packet_sha256"] != (
        checkpoint["normalized_packet_sha256"]
    ):
        raise AssertionError("finish payload is not bound to checkpoint packet")
    for key in (
        "model_calls",
        "candidate_writes",
        "claim_writes",
        "staging_writes",
        "qdrant_writes",
    ):
        if finish[key] != 0:
            raise AssertionError(f"finish payload {key} is not zero")

    print("memory_v1_evidence_extraction_fixture_worker_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
