#!/usr/bin/env python3

from __future__ import annotations

import copy
import unittest

from scripts.memory_v1_v5_review_extraction_packet import (
    PacketReviewError,
    resolve_packet_project_scopes,
)


def packet(name: str = "Verbal Sage Memory V1/V5") -> dict:
    span = {"start": 0, "end": 1, "span_sha256": "a" * 64}
    return {
        "entity_mentions": [{
            "entity_ref": "e01",
            "entity_type": "project",
            "mention_kind": "named",
            "name_text": name,
            "relationship_role": None,
            "source_spans": [span],
            "extraction_confidence": 1.0,
            "reason_codes": ["named_project"],
        }],
        "observations": [{
            "observation_ref": "o01",
            "subject_entity_ref": "e01",
            "projection_class": "project_knowledge",
            "project_scope": {
                "state": "unresolved",
                "project_key": None,
                "component_key": None,
                "binding_source": "unresolved",
            },
        }],
        "deferrals": [{
            "reason_code": "project_scope_unresolved",
            "memory_shape": "project_knowledge",
            "source_spans": [span],
            "sensitivity": "medium",
            "review_required": True,
        }],
        "packet_findings": ["project_scope_unresolved"],
    }


COMPONENTS = [{
    "component_id": "00000000-0000-4000-8000-000000000001",
    "component_key": "memory-v1",
    "display_name": "Memory V1",
    "parent_component_id": None,
    "aliases": ["memory-v1", "verbal-sage-memory-v1-v5"],
}]


class ReviewPacketTest(unittest.TestCase):
    def test_unique_component_alias_resolves_without_mutating_input(self) -> None:
        source = packet()
        original = copy.deepcopy(source)
        resolved, transformations = resolve_packet_project_scopes(
            source,
            project_key="verbal-sage",
            components=COMPONENTS,
        )
        self.assertEqual(source, original)
        self.assertEqual(
            resolved["observations"][0]["project_scope"],
            {
                "state": "resolved",
                "project_key": "verbal-sage",
                "component_key": "memory-v1",
                "binding_source": "trusted_component_registry",
            },
        )
        self.assertEqual(resolved["deferrals"], [])
        self.assertEqual(resolved["packet_findings"], [])
        self.assertEqual(
            [item["kind"] for item in transformations],
            ["resolve_project_scope", "discharge_deferral"],
        )

    def test_unknown_alias_fails_closed(self) -> None:
        with self.assertRaises(PacketReviewError):
            resolve_packet_project_scopes(
                packet("Unknown Project"),
                project_key="verbal-sage",
                components=COMPONENTS,
            )

    def test_ambiguous_alias_fails_closed(self) -> None:
        components = COMPONENTS + [{
            "component_id": "00000000-0000-4000-8000-000000000002",
            "component_key": "other",
            "display_name": "Other",
            "parent_component_id": None,
            "aliases": ["verbal-sage-memory-v1-v5"],
        }]
        with self.assertRaises(PacketReviewError):
            resolve_packet_project_scopes(
                packet(),
                project_key="verbal-sage",
                components=components,
            )

    def test_root_name_resolves_root_only(self) -> None:
        resolved, _ = resolve_packet_project_scopes(
            packet("Verbal Sage"),
            project_key="verbal-sage",
            components=COMPONENTS,
        )
        scope = resolved["observations"][0]["project_scope"]
        self.assertIsNone(scope["component_key"])
        self.assertEqual(scope["binding_source"], "trusted_thread_binding")


if __name__ == "__main__":
    unittest.main()
