#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from rag_engine.memory_v1_evidence_context_v1 import (
    build_memory_evidence_context_envelope_v1,
    sanitized_evidence_context_report_v1,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a sanitized read-only evidence-context report."
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--expected-owner-user-id", required=True)
    parser.add_argument("--target-evidence-id", required=True)
    parser.add_argument("--expected-target-content-sha256", required=True)
    parser.add_argument("--max-spans", type=int, default=12)
    return parser.parse_args()


def load_input(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("source"), dict)
        or not isinstance(value.get("evidence"), list)
    ):
        raise RuntimeError("input must contain source object and evidence array")
    return value


def main() -> int:
    args = arguments()
    value = load_input(Path(args.input))
    envelope = build_memory_evidence_context_envelope_v1(
        expected_owner_user_id=args.expected_owner_user_id,
        target_evidence_id=args.target_evidence_id,
        expected_target_content_sha256=(
            args.expected_target_content_sha256
        ),
        source_row=value["source"],
        evidence_rows=value["evidence"],
        max_spans=args.max_spans,
    )
    print(
        json.dumps(
            sanitized_evidence_context_report_v1(envelope),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
