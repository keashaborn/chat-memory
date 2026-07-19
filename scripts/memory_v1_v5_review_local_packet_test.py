#!/usr/bin/env python3
"""Offline tests for sanitized local packet review quality gates."""

from __future__ import annotations

import copy
import json

from scripts.memory_v1_v5_review_local_packet import (
    _sha256_valid,
    packet_quality_findings,
    review_artifact_eligible,
)


def packet() -> dict:
    return {
        "observations": [],
        "packet_findings": [],
        "deferrals": [],
        "comparison_hints": [],
    }


def observation(
    *,
    ref: str,
    predicate: str,
    projection_class: str = "direct_claim",
    project_state: str = "not_applicable",
    literal: str = "secret literal",
) -> dict:
    return {
        "observation_ref": ref,
        "predicate": predicate,
        "projection_class": projection_class,
        "project_scope": {"state": project_state},
        "object": {"kind": "literal", "datatype": "text", "value": literal},
    }


def main() -> int:
    species = packet()
    species["observations"] = [
        observation(ref="o00", predicate="pet.species", literal="private breed")
    ]
    findings = packet_quality_findings(species)
    assert findings == [
        {
            "code": "open_pet_species_domain_review_required",
            "observation_ref": "o00",
            "predicate": "pet.species",
            "blocking": True,
        }
    ]
    assert "private breed" not in json.dumps(findings)

    breed = copy.deepcopy(species)
    breed["observations"][0]["predicate"] = "pet.breed"
    assert packet_quality_findings(breed) == []

    project = packet()
    project["observations"] = [
        observation(
            ref="o01",
            predicate="project.current_state",
            projection_class="project_knowledge",
            project_state="unresolved",
        )
    ]
    assert packet_quality_findings(project)[0]["code"] == (
        "project_scope_resolution_required"
    )
    project["observations"][0]["project_scope"]["state"] = "resolved"
    assert packet_quality_findings(project) == []

    routed = packet()
    routed["packet_findings"] = ["mixed_authorship"]
    routed["deferrals"] = [{"reason_code": "ambiguous_transcription"}]
    routed["comparison_hints"] = [{"comparison_ref": "c00"}]
    codes = [item["code"] for item in packet_quality_findings(routed)]
    assert codes == [
        "comparison_hint_review_required",
        "extractor_deferral_ambiguous_transcription",
        "extractor_finding_mixed_authorship",
    ]
    assert len(codes) == len(set(codes))

    assert not review_artifact_eligible(
        {
            "manual_review_required": False,
            "entity_mention_count": 0,
            "observation_count": 0,
            "comparison_hint_count": 0,
        }
    )
    assert review_artifact_eligible(
        {
            "manual_review_required": False,
            "entity_mention_count": 1,
            "observation_count": 1,
            "comparison_hint_count": 0,
        }
    )
    assert review_artifact_eligible(
        {
            "manual_review_required": True,
            "entity_mention_count": 0,
            "observation_count": 0,
            "comparison_hint_count": 0,
        }
    )

    assert _sha256_valid("a" * 64)
    assert not _sha256_valid("A" * 64)
    assert not _sha256_valid("a" * 63)

    print("memory_v1_v5_review_local_packet: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
