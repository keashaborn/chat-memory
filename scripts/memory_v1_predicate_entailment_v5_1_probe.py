#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.memory_v1_predicate_entailment_v5_1 import (
    POLICY_VERSION,
    assess_observation_entailment,
)


EXPECTED_OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
EXPECTED_EVIDENCE = "fca9e5dc-83c2-4456-8db8-1fe6102eb74d"
EXPECTED_OBSERVATION = "9bf1e6b2-1840-4524-98dc-142567ebe013"
EXPECTED_SOURCE_SHA256 = (
    "d41de5228017def6a31b3286cfb4033b8c26a251e73b3c16af67990bfb276de6"
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def require(value: bool, message: str) -> None:
    if not value:
        raise RuntimeError(message)


def main() -> int:
    args = arguments()
    payload: dict[str, Any] = json.loads(Path(args.input).read_text())
    evidence = payload.get("evidence") or {}
    observation = payload.get("observation") or {}

    require(evidence.get("owner_user_id") == EXPECTED_OWNER, "evidence owner mismatch")
    require(evidence.get("evidence_id") == EXPECTED_EVIDENCE, "evidence ID mismatch")
    require(
        observation.get("owner_user_id") == EXPECTED_OWNER,
        "observation owner mismatch",
    )
    require(
        observation.get("observation_id") == EXPECTED_OBSERVATION,
        "observation ID mismatch",
    )
    require(
        observation.get("evidence_id") == EXPECTED_EVIDENCE,
        "observation evidence mismatch",
    )
    content = str(evidence.get("content") or "")
    content_sha256 = hashlib.sha256(content.encode()).hexdigest()
    require(content_sha256 == EXPECTED_SOURCE_SHA256, "source content hash mismatch")
    require(
        evidence.get("content_sha256") == EXPECTED_SOURCE_SHA256,
        "stored source hash mismatch",
    )
    require(
        observation.get("predicate") == "occupation.works_as",
        "fixture predicate changed",
    )
    require(observation.get("polarity") == "affirmed", "fixture polarity changed")

    decision = assess_observation_entailment(observation, content)
    require(decision.status == "defer", "overstated occupation was not deferred")
    require(
        decision.reason_code == "source_contradicts_predicate",
        "unexpected entailment reason",
    )

    report = {
        "contract_version": "memory_v1_predicate_entailment_v5_1_probe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "production_clone_read_only",
        "policy_version": POLICY_VERSION,
        "owner_user_id": EXPECTED_OWNER,
        "evidence_id": EXPECTED_EVIDENCE,
        "observation_id": EXPECTED_OBSERVATION,
        "source_sha256": EXPECTED_SOURCE_SHA256,
        "decision": {
            "status": decision.status,
            "reason_code": decision.reason_code,
            "source_span": decision.source_span,
        },
        "predicate_substitution": False,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "prompt_influence": False,
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
