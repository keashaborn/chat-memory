from __future__ import annotations

import unittest
from datetime import datetime, timezone

from scripts.memory_v1_relational_v5_live_eval import (
    EntityMention,
    ModelPacket,
    Observation,
    Temporal,
)
from scripts.memory_v1_relational_v5_specialized import (
    ENTITY_GRAPH_INSTRUCTIONS,
    PROJECT_KNOWLEDGE_INSTRUCTIONS,
    TEMPORAL_CONTENT_INSTRUCTIONS,
    EntityGraphPassPacket,
    ProjectKnowledgePassPacket,
    ProjectObservationProposal,
    TemporalContentPassPacket,
    assemble_specialized_packet,
    entity_catalog,
    execution_plan,
)


def span(text: str = "source fact") -> dict:
    return {"start": 0, "end": len(text), "quote": text}


def temporal(semantic: str = "state_validity") -> dict:
    return {
        "semantic": semantic,
        "shape": "none",
        "basis": "none",
        "source_form": "none",
        "certainty": "unknown",
        "precision": "unknown",
        "instant": None,
        "calendar_range": None,
        "instant_range": None,
        "relative_offset": None,
        "recurrence": None,
        "anchored_to_source_time": False,
        "reason_codes": [],
    }


def entity(ref: str, entity_type: str, role: str) -> EntityMention:
    return EntityMention.model_validate(
        {
            "entity_ref": ref,
            "entity_type": entity_type,
            "mention_kind": "self_reference" if entity_type == "self" else "role_only",
            "name_text": None,
            "relationship_role": role,
            "source_spans": [span()],
            "extraction_confidence": 0.95,
            "reason_codes": ["direct_entity_mention"],
        }
    )


def observation(
    ref: str,
    subject: str,
    predicate: str,
    obj: dict,
    *,
    projection: str = "direct_claim",
    policy: str = "direct_or_relevant",
    modality: str = "asserted",
    temporal_semantic: str = "state_validity",
) -> Observation:
    return Observation.model_validate(
        {
            "observation_ref": ref,
            "subject_entity_ref": subject,
            "predicate": predicate,
            "object": obj,
            "polarity": "affirmed",
            "modality": modality,
            "projection_class": projection,
            "surface_policy": policy,
            "temporal": temporal(temporal_semantic),
            "sensitivity": "medium",
            "extraction_confidence": 0.95,
            "source_spans": [span()],
            "reason_codes": ["direct_observation"],
        }
    )


class SpecializedV5Test(unittest.TestCase):
    def packets(self):
        graph = EntityGraphPassPacket(
            entity_mentions=[
                entity("e01", "self", "user:self"),
                entity("e02", "person", "family:mother"),
            ],
            relationship_observations=[
                observation(
                    "o01",
                    "e02",
                    "relationship.parent_of",
                    {"kind": "entity", "entity_ref": "e01"},
                )
            ],
            deferrals=[],
            packet_findings=[],
        )
        content = TemporalContentPassPacket(
            observations=[
                observation(
                    "o01",
                    "e02",
                    "age.reported",
                    {
                        "kind": "literal",
                        "datatype": "number",
                        "value": 70.0,
                        "unit": "year",
                        "approximate": True,
                    },
                    temporal_semantic="observation_time",
                )
            ],
            comparison_hints=[],
            deferrals=[],
            packet_findings=[],
        )
        project = ProjectKnowledgePassPacket(
            observations=[
                ProjectObservationProposal.model_validate(
                    {
                        "observation_ref": "o01",
                        "predicate": "project.current_state",
                        "object": {
                            "kind": "literal",
                            "datatype": "text",
                            "value": "feature is not implemented",
                            "unit": None,
                            "approximate": False,
                        },
                        "polarity": "affirmed",
                        "modality": "asserted",
                        "projection_class": "project_knowledge",
                        "surface_policy": "exact_project_scope_only",
                        "temporal": temporal(),
                        "sensitivity": "medium",
                        "extraction_confidence": 0.9,
                        "source_spans": [span("my application")],
                        "reason_codes": ["direct_project_state"],
                    }
                )
            ],
            deferrals=[],
            packet_findings=[],
        )
        return graph, content, project

    def test_assembler_namespaces_observations_and_server_project_entity(self) -> None:
        graph, content, project = self.packets()
        packet = assemble_specialized_packet(graph, content, project)
        self.assertIsInstance(packet, ModelPacket)
        self.assertEqual(
            [item.observation_ref for item in packet.observations],
            ["o01", "o02", "o03"],
        )
        self.assertEqual(packet.entity_mentions[-1].entity_ref, "e03")
        self.assertEqual(packet.entity_mentions[-1].mention_kind, "anonymous")
        self.assertEqual(packet.entity_mentions[-1].relationship_role, "project:unresolved")
        self.assertEqual(
            packet.entity_mentions[-1].reason_codes,
            ["server_assigned_unresolved_project_entity"],
        )
        self.assertEqual(packet.observations[-1].subject_entity_ref, "e03")

    def test_assembler_preserves_python_datetime_for_strict_packet(self) -> None:
        graph, content, project = self.packets()
        instant = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        content.observations[0].temporal = Temporal.model_validate(
            {
                "semantic": "observation_time",
                "shape": "instant",
                "basis": "instant",
                "source_form": "implicit_source_time",
                "certainty": "exact",
                "precision": "exact",
                "instant": instant,
                "calendar_range": None,
                "instant_range": None,
                "relative_offset": None,
                "recurrence": None,
                "anchored_to_source_time": True,
                "reason_codes": ["trusted_source_time"],
            }
        )
        packet = assemble_specialized_packet(graph, content, project)
        self.assertEqual(packet.observations[1].temporal.instant, instant)

    def test_entity_catalog_contains_no_authoritative_owner_fields(self) -> None:
        graph, _, _ = self.packets()
        catalog = entity_catalog(graph)
        self.assertEqual(catalog[0]["entity_ref"], "e01")
        serialized = repr(catalog)
        self.assertNotIn("owner_user_id", serialized)
        self.assertNotIn("project_id", serialized)

    def test_content_pass_cannot_emit_project_predicate(self) -> None:
        graph, content, project = self.packets()
        content.observations[0].predicate = "project.current_state"
        with self.assertRaisesRegex(ValueError, "lane boundary"):
            assemble_specialized_packet(graph, content, project)

    def test_graph_pass_cannot_emit_attribute_predicate(self) -> None:
        graph, content, project = self.packets()
        graph.relationship_observations[0].predicate = "age.reported"
        with self.assertRaisesRegex(ValueError, "non-graph predicate"):
            assemble_specialized_packet(graph, content, project)

    def test_dangling_entity_reference_fails_closed(self) -> None:
        graph, content, project = self.packets()
        content.observations[0].subject_entity_ref = "e99"
        with self.assertRaisesRegex(ValueError, "unresolved entity refs"):
            assemble_specialized_packet(graph, content, project)

    def test_empty_project_pass_creates_no_project_entity(self) -> None:
        graph, content, project = self.packets()
        project.observations = []
        packet = assemble_specialized_packet(graph, content, project)
        self.assertFalse(
            any(item.entity_type == "project" for item in packet.entity_mentions)
        )

    def test_assembler_prunes_entity_mentions_unreferenced_by_observations(self) -> None:
        graph = EntityGraphPassPacket(
            entity_mentions=[
                entity("e01", "self", "user:self"),
                entity("e02", "concept", "topic:unresolved"),
            ],
            relationship_observations=[],
            deferrals=[],
            packet_findings=[],
        )
        packet = assemble_specialized_packet(
            graph,
            TemporalContentPassPacket(
                observations=[], comparison_hints=[], deferrals=[], packet_findings=[]
            ),
            ProjectKnowledgePassPacket(
                observations=[], deferrals=[], packet_findings=[]
            ),
        )
        self.assertEqual(packet.entity_mentions, [])
        self.assertIn("orphan_entity_mentions_pruned", packet.packet_findings)

    def test_execution_plan_is_zero_write_and_server_authoritative(self) -> None:
        plan = execution_plan()
        self.assertEqual(
            plan.pass_order,
            ["entity_graph", "temporal_content", "project_knowledge"],
        )
        self.assertFalse(plan.model_may_assign_owner)
        self.assertFalse(plan.model_may_bind_project)
        self.assertFalse(plan.model_may_write)

    def test_pass_schemas_exclude_owner_and_durable_authority(self) -> None:
        for model in (
            EntityGraphPassPacket,
            TemporalContentPassPacket,
            ProjectKnowledgePassPacket,
        ):
            schema = repr(model.model_json_schema())
            for forbidden in (
                "owner_user_id",
                "project_id",
                "claim_id",
                "evidence_id",
                "approved",
                "salience",
            ):
                self.assertNotIn(forbidden, schema)
        self.assertNotIn(
            "project_entity",
            ProjectKnowledgePassPacket.model_json_schema()["properties"],
        )

    def test_instructions_have_disjoint_lane_ownership(self) -> None:
        self.assertIn("only the source-local entity graph", ENTITY_GRAPH_INSTRUCTIONS)
        self.assertIn("non-project", TEMPORAL_CONTENT_INSTRUCTIONS)
        self.assertIn("only project knowledge", PROJECT_KNOWLEDGE_INSTRUCTIONS)
        self.assertIn("question_only", TEMPORAL_CONTENT_INSTRUCTIONS)
        self.assertIn("identity.name_canonical", TEMPORAL_CONTENT_INSTRUCTIONS)
        self.assertIn("project.requirement", PROJECT_KNOWLEDGE_INSTRUCTIONS)
        self.assertIn("server will", PROJECT_KNOWLEDGE_INSTRUCTIONS)
        self.assertIn("approximate=false", PROJECT_KNOWLEDGE_INSTRUCTIONS)
        self.assertIn("state_validity", PROJECT_KNOWLEDGE_INSTRUCTIONS)
        self.assertIn("supersede", PROJECT_KNOWLEDGE_INSTRUCTIONS)
        combined = "\n".join(
            (
                ENTITY_GRAPH_INSTRUCTIONS,
                TEMPORAL_CONTENT_INSTRUCTIONS,
                PROJECT_KNOWLEDGE_INSTRUCTIONS,
            )
        )
        self.assertNotIn("expected outcome", combined.lower())
        self.assertNotIn("case_id", combined)


if __name__ == "__main__":
    unittest.main()
