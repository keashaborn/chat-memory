#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from scripts.memory_v1_consolidation_packet_eval import secure_write_json
from scripts.memory_v1_relational_v5_live_eval import sha256_bytes
from scripts.memory_v1_relational_v5_specialized_replay import (
    MODE as REPLAY_MODE,
    repository_commit,
)
from scripts.memory_v1_v5_stage_preflight import _validate_extraction_packet


MODE = "zero_write_relational_v5_specialized_materialized_evaluation"
NORMALIZATION_POLICY_VERSION = "memory_v1_relational_specialized_v5_1"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize one hash-locked, passed offline V5 replay as a full "
            "extraction report without model or persistence calls"
        )
    )
    parser.add_argument("--replay-report", required=True)
    parser.add_argument("--replay-report-sha256", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def _digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def build_materialized_report(
    replay_report: dict[str, Any],
    *,
    replay_report_sha256: str,
    materializer_commit: str,
) -> dict[str, Any]:
    if replay_report.get("mode") != REPLAY_MODE:
        raise RuntimeError("replay report mode mismatch")
    if replay_report.get("store") is not False:
        raise RuntimeError("replay report is not store=false")
    if replay_report.get("external_model_calls") != 0:
        raise RuntimeError("replay report contains external model calls")
    proof = replay_report.get("zero_write_proof") or {}
    if (
        proof.get("passed") is not True
        or proof.get("database_unchanged") is not True
        or proof.get("qdrant_unchanged") is not True
    ):
        raise RuntimeError("replay report did not prove zero writes")
    if not _digest(replay_report_sha256):
        raise RuntimeError("replay report SHA-256 is invalid")
    if (
        not isinstance(materializer_commit, str)
        or len(materializer_commit) != 40
        or any(character not in "0123456789abcdef" for character in materializer_commit)
    ):
        raise RuntimeError("materializer commit is invalid")
    owner = str(uuid.UUID(replay_report["owner_user_id"]))
    replay = replay_report.get("replay") or {}
    if (replay.get("evaluation") or {}).get("passed") is not True:
        raise RuntimeError("replayed extraction did not pass evaluation")
    if replay.get("deterministic_rejections") or replay.get("integrity_reasons"):
        raise RuntimeError("replayed extraction has unresolved validation findings")
    packet = replay.get("packet")
    if not isinstance(packet, dict):
        raise RuntimeError("replayed extraction packet is absent")
    _validate_extraction_packet(packet)
    envelope = packet["source_envelope"]
    if (
        replay.get("source_external_id") != envelope.get("source_external_id")
        or replay.get("source_sha256") != envelope.get("source_sha256")
    ):
        raise RuntimeError("replay source identity does not match extraction packet")
    provenance = {
        "source_replay_report_sha256": replay_report_sha256,
        "materializer_commit": materializer_commit,
        "normalization_policy_version": NORMALIZATION_POLICY_VERSION,
    }
    source = {
        "case_id": replay["case_id"],
        "ordinal": int(replay["ordinal"]),
        "source_external_id": replay["source_external_id"],
        "source_sha256": replay["source_sha256"],
        "attempts": [],
        "model_packet": replay.get("model_packet"),
        "packet": packet,
        "deterministic_rejections": [],
        "integrity_reasons": [],
        "evaluation": replay["evaluation"],
        "materialization_provenance": provenance,
    }
    return {
        "mode": MODE,
        "pipeline_version": replay_report["pipeline_version"],
        "evaluator_commit": replay_report["evaluator_commit"],
        "materializer_commit": materializer_commit,
        "manifest_sha256": replay_report["manifest_sha256"],
        "case_contract_sha256": replay_report["case_contract_sha256"],
        "source_replay_report_sha256": replay_report_sha256,
        "owner_user_id": owner,
        "source_count": 1,
        "external_model_calls": 0,
        "store": False,
        "sources": [source],
        "zero_write_proof": proof,
    }


def main() -> int:
    args = arguments()
    replay_path = Path(args.replay_report).resolve()
    replay_bytes = replay_path.read_bytes()
    actual_sha256 = sha256_bytes(replay_bytes)
    if actual_sha256 != args.replay_report_sha256:
        raise RuntimeError("replay report SHA-256 mismatch")
    replay_report = json.loads(replay_bytes)
    report = build_materialized_report(
        replay_report,
        replay_report_sha256=actual_sha256,
        materializer_commit=repository_commit(),
    )
    output = Path(args.output).resolve()
    secure_write_json(output, report)
    output_sha256 = sha256_bytes(output.read_bytes())
    print(
        json.dumps(
            {
                "mode": MODE,
                "output": str(output),
                "sha256": output_sha256,
                "case_id": report["sources"][0]["case_id"],
                "external_model_calls": 0,
                "database_writes": 0,
                "qdrant_writes": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}:{exc}")
        raise SystemExit(1)
