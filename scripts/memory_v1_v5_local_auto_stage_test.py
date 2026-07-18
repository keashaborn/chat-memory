#!/usr/bin/env python3
"""Deterministic unit checks for the local auto-stage worker."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import uuid

from scripts.memory_v1_v5_local_auto_stage import (
    POLICY_VERSION,
    admission_ids,
    secure_review_root,
)
from scripts.memory_v1_v5_local_packet_router import artifact_paths


def main() -> int:
    owner = uuid.UUID("11111111-1111-4111-8111-111111111111")
    artifact = uuid.UUID("a3300000-0000-4000-8000-000000000001")
    bundle_sha = "a" * 64
    first = admission_ids(owner, artifact, bundle_sha)
    second = admission_ids(owner, artifact, bundle_sha)
    assert first == second
    assert first[0] != first[1]
    assert admission_ids(owner, artifact, "b" * 64) != first
    assert POLICY_VERSION == "memory_v1_v5_local_auto_stage_policy_v1"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        os.chmod(root, 0o700)
        assert secure_review_root(str(root)) == root.resolve()
        report, bundle = artifact_paths(
            root, uuid.UUID("a3000000-0000-4000-8000-000000000001")
        )
        assert report.parent == root and bundle.parent == root
        os.chmod(root, 0o750)
        try:
            secure_review_root(str(root))
        except RuntimeError:
            pass
        else:
            raise AssertionError("peer-readable review root was accepted")
    print("memory_v1_v5_local_auto_stage: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
