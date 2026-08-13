#!/usr/bin/env python3
from __future__ import annotations

"""Emit, but never write, the exact dormant-store installation package manifest JSON."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.governed_memory_install.package import (  # noqa: E402
    EXPECTED_ARTIFACTS,
    artifact_sha256,
)


def generate() -> dict[str, object]:
    return {
        "schema_version": (
            "governed-memory-dormant-store-install-inactive-execution-package-manifest-v5"
        ),
        "state": (
            "phase9j-install-ready-closed-runtime-and-store-transports-"
            "packaged-not-installed-not-activated"
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
