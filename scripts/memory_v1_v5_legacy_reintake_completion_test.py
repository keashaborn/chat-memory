#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path

from memory_v1_v5_legacy_reintake_completion import evaluation, secure_write


def snapshot(status_counts: dict[str, int], attempts: list[int]) -> dict:
    return {
        "job_count": 5,
        "status_counts": status_counts,
        "active_lease_count": 0,
        "job_bindings": [
            {
                "job_id_sha256": str(index) * 64,
                "evidence_content_sha256": "a" * 64,
                "status": status,
                "attempts": attempts[index - 1],
                "has_lease": False,
            }
            for index, status in enumerate(
                [key for key, count in status_counts.items() for _ in range(count)], 1
            )
        ],
        "completion_inventory": [],
        "protected_direct_write_privileges": {
            "entity": False,
            "candidate": False,
            "claim": False,
            "claim_revision": False,
        },
    }


def main() -> None:
    baseline_snapshot = snapshot({"pending": 5}, [1, 1, 0, 0, 0])
    baseline = {
        "snapshot": baseline_snapshot,
        "qdrant": {"point_count": 4, "inventory_sha256": "a" * 64},
    }
    baseline_result = evaluation(
        current=baseline_snapshot,
        expected_jobs=5,
        cross_owner_count=0,
        qdrant=baseline["qdrant"],
        baseline=None,
    )
    assert baseline_result["pass"] is True
    assert baseline_result["ready_for_restoration"] is False

    terminal = snapshot({"review_required": 2, "skipped": 3}, [2, 2, 1, 1, 1])
    ready = evaluation(
        current=terminal,
        expected_jobs=5,
        cross_owner_count=0,
        qdrant=baseline["qdrant"],
        baseline=baseline,
    )
    assert ready["pass"] is True
    assert ready["ready_for_restoration"] is True
    assert ready["attempt_delta"] == 5

    pending = snapshot({"review_required": 2, "pending": 3}, [2, 2, 0, 0, 0])
    not_ready = evaluation(
        current=pending,
        expected_jobs=5,
        cross_owner_count=0,
        qdrant=baseline["qdrant"],
        baseline=baseline,
    )
    assert not_ready["pass"] is False
    assert not_ready["ready_for_restoration"] is False

    changed_qdrant = evaluation(
        current=terminal,
        expected_jobs=5,
        cross_owner_count=0,
        qdrant={"point_count": 5, "inventory_sha256": "b" * 64},
        baseline=baseline,
    )
    assert changed_qdrant["pass"] is False

    cross_owner = evaluation(
        current=terminal,
        expected_jobs=5,
        cross_owner_count=1,
        qdrant=baseline["qdrant"],
        baseline=baseline,
    )
    assert cross_owner["pass"] is False

    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "audit.json"
        secure_write(output, {"test": True})
        try:
            secure_write(output, {"test": False})
        except RuntimeError as exc:
            assert str(exc) == "completion audit output already exists"
        else:
            raise AssertionError("audit output overwrite was not rejected")

    print("memory_v1_v5_legacy_reintake_completion: PASS")


if __name__ == "__main__":
    main()
