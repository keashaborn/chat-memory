#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from rag_engine.memory_v1_specialized_review import build_specialized_review_report


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REVIEW = ROOT / "ops" / "reviews" / "memory_v1_specialized_review_20260713.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and render the 72 hash-locked specialized Memory V1 review "
            "decisions. This command has no database or apply path."
        )
    )
    parser.add_argument("--source-report", required=True)
    parser.add_argument("--review", default=str(DEFAULT_REVIEW))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_specialized_review_report(
        source_report_path=args.source_report,
        review_path=args.review,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
