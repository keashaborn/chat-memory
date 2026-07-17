#!/usr/bin/env python3
from __future__ import annotations

from memory_v1_v5_source_evidence import load_report_sources, max_sensitivity
import uuid


def main() -> None:
    packet = {
        "observations": [{"sensitivity": "medium"}, {"sensitivity": "high"}],
        "deferrals": [{"sensitivity": "low"}],
    }
    assert max_sensitivity(packet) == "high"
    owner = uuid.UUID("11111111-1111-4111-8111-111111111111")
    report = {
        "owner_user_id": str(owner),
        "store": False,
        "zero_write_proof": {"passed": True},
        "sources": [{
            "case_id": "v5-01",
            "evaluation": {"passed": True},
            "packet": {
                "source_envelope": {
                    "source_external_id": "22222222-2222-4222-8222-222222222222",
                    "source_sha256": "a" * 64,
                    "source_recorded_at": "2026-07-16T00:00:00+00:00",
                },
                "observations": [],
                "deferrals": [],
            },
        }],
    }
    rows = load_report_sources(report, owner=owner, case_ids=["v5-01"])
    assert len(rows) == 1 and rows[0]["sensitivity"] == "medium"
    print("memory_v1_v5_source_evidence: PASS")


if __name__ == "__main__":
    main()
