#!/usr/bin/env python3
from __future__ import annotations

"""Run the governed-memory unit suite from an isolated interpreter."""

from pathlib import Path
import os
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = ROOT / "tests" / "memory"


def main() -> int:
    if Path.cwd().resolve() != ROOT or not TEST_ROOT.is_dir():
        print(
            "run_memory_unit_tests.py must run from the resolved repository root",
            file=sys.stderr,
        )
        return 2
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONNOUSERSITE"] = "1"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(
        str(TEST_ROOT),
        pattern="test_*.py",
        top_level_dir=str(ROOT),
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
