#!/usr/bin/env python3
from __future__ import annotations

"""Emit, but never write, the exact Phase 8B package manifest JSON."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.governed_memory_install.package_v3 import (  # noqa: E402
    EXPECTED_ARTIFACTS,
    artifact_sha256,
)


def generate() -> dict[str, object]:
    return {
        "schema_version": (
            "governed-memory-phase8b-inactive-stores-package-manifest-v1"
        ),
        "state": (
            "inactive_remediation_package_proof_pending_not_staged_"
            "not_installed_not_authorized"
        ),
        "artifacts": {
            relative: artifact_sha256(relative)
            for relative in sorted(EXPECTED_ARTIFACTS)
        },
    }


def main() -> int:
    print(json.dumps(generate(), indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
