from __future__ import annotations

import asyncio
import inspect
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from scripts.memory_v1_relational_v5_live_eval import (
    EntityObject,
    EntityMention,
    Observation,
    Temporal,
    sha256_text,
)
from scripts.memory_v1_relational_v5_specialized import (
    EntityGraphPassPacket,
    ProjectKnowledgePassPacket,
    ProjectObservationProposal,
    TemporalContentPassPacket,
)
from scripts.memory_v1_relational_v5_specialized_eval import (
    SpecializedExtractionError,
    normalize_explicit_pet_name_correction_content,
    normalize_explicit_pet_name_correction_graph,
    normalize_known_technical_question_deferrals,
    normalize_predicate_entailment_deferrals,
    normalize_plural_sibling_residence,
    normalize_project_lane_deferrals,
    normalize_project_current_state_temporal,
    normalize_repeated_sibling_roles,
    normalize_redundant_source_spans,
    normalize_uncertain_credential_deferral,
    run_specialized_zero_write,
    validate_entity_graph_pass,
    validate_project_knowledge_pass,
)
from scripts.memory_v1_relational_v5_specialized_replay import replay_model_packet


SOURCE = "I prefer concise answers. My application needs export."
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
SOURCE_ID = "synthetic-source-01"
OBSERVED_AT = "2026-07-15T12:00:00Z"


def span(quote: str) -> dict:
    start = SOURCE.index(quote)
    return {"start": start, "end": start + len(quote), "quote": quote}


def temporal() -> dict:
    return {
        "semantic": "observation_time",
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


def self_entity(*, entity_type: str = "self") -> EntityMention:
    return EntityMention.model_validate(
        {
            "entity_ref": "e01",
            "entity_type": entity_type,
            "mention_kind": "self_reference" if entity_type == "self" else "role_only",
            "name_text": None,
            "relationship_role": "user:self" if entity_type == "self" else "project:unresolved",
            "source_spans": [span("I")],
            "extraction_confidence": 0.95,
            "reason_codes": ["direct_entity_mention"],
        }
    )


def graph_packet(*, invalid_project: bool = False) -> EntityGraphPassPacket:
    return EntityGraphPassPacket(
        entity_mentions=[
            self_entity(entity_type="project" if invalid_project else "self")
        ],
        relationship_observations=[],
        deferrals=[],
        packet_findings=[],
    )


def sibling_graph_packet(
    source: str, names: list[str], *, shared_entity_span: bool = False
) -> EntityGraphPassPacket:
    mentions = [
        EntityMention.model_validate(
            {
                "entity_ref": "e01",
                "entity_type": "self",
                "mention_kind": "self_reference",
                "name_text": None,
                "relationship_role": "user:self",
                "source_spans": [
                    {"start": source.index("I"), "end": source.index("I") + 1, "quote": "I"}
                ],
                "extraction_confidence": 0.99,
                "reason_codes": ["grounded_self_reference"],
            }
        )
    ]
    observations: list[Observation] = []
    shared_start = source.index("sisters") if "sisters" in source else -1
    for ordinal, name in enumerate(names, 2):
        name_start = source.index(name)
        entity_span = (
            {
                "start": shared_start,
                "end": shared_start + len("sisters"),
                "quote": "sisters",
            }
            if shared_entity_span
            else {
                "start": name_start,
                "end": name_start + len(name),
                "quote": name,
            }
        )
        mentions.append(
            EntityMention.model_validate(
                {
                    "entity_ref": f"e{ordinal:02d}",
                    "entity_type": "person",
                    "mention_kind": "named",
                    "name_text": name,
                    "relationship_role": "family:sister",
                    "source_spans": [entity_span],
                    "extraction_confidence": 0.95,
                    "reason_codes": ["named_sibling"],
                }
            )
        )
        state_temporal = temporal()
        state_temporal["semantic"] = "state_validity"
        observations.append(
            Observation.model_validate(
                {
                    "observation_ref": f"o{ordinal - 1:02d}",
                    "subject_entity_ref": "e01",
                    "predicate": "relationship.sibling_of",
                    "object": {"kind": "entity", "entity_ref": f"e{ordinal:02d}"},
                    "polarity": "affirmed",
                    "modality": "asserted",
                    "projection_class": "direct_claim",
                    "surface_policy": "direct_or_relevant",
                    "temporal": state_temporal,
                    "sensitivity": "medium",
                    "extraction_confidence": 0.95,
                    "source_spans": [
                        {
                            "start": name_start,
                            "end": name_start + len(name),
                            "quote": name,
                        }
                    ],
                    "reason_codes": ["direct_sibling_relationship"],
                }
            )
        )
    return EntityGraphPassPacket(
        entity_mentions=mentions,
        relationship_observations=observations,
        deferrals=[],
        packet_findings=[],
    )


def content_packet() -> TemporalContentPassPacket:
    observation = Observation.model_validate(
        {
            "observation_ref": "o01",
            "subject_entity_ref": "e01",
            "predicate": "preference.response",
            "object": {
                "kind": "literal",
                "datatype": "json",
                "value": {
                    "dimension": "response_length",
                    "value": "concise",
                },
                "unit": None,
                "approximate": False,
            },
            "polarity": "affirmed",
            "modality": "asserted",
            "projection_class": "response_preference",
            "surface_policy": "zero_token_control_only",
            "temporal": temporal(),
            "sensitivity": "low",
            "extraction_confidence": 0.95,
            "source_spans": [span("I prefer concise answers")],
            "reason_codes": ["direct_response_preference"],
        }
    )
    return TemporalContentPassPacket(
        observations=[observation],
        comparison_hints=[],
        deferrals=[],
        packet_findings=[],
    )


def project_packet(*, sensitivity: str = "medium") -> ProjectKnowledgePassPacket:
    return ProjectKnowledgePassPacket(
        observations=[
            ProjectObservationProposal.model_validate(
                {
                    "observation_ref": "o01",
                    "predicate": "project.requirement",
                    "object": {
                        "kind": "literal",
                        "datatype": "text",
                        "value": "export",
                        "unit": None,
                        "approximate": False,
                    },
                    "polarity": "affirmed",
                    "modality": "endorsed",
                    "projection_class": "project_knowledge",
                    "surface_policy": "exact_project_scope_only",
                    "temporal": temporal(),
                    "sensitivity": sensitivity,
                    "extraction_confidence": 0.9,
                    "source_spans": [span("My application needs export")],
                    "reason_codes": ["direct_project_requirement"],
                }
            )
        ],
        deferrals=[],
        packet_findings=[],
    )


def response(
    parsed=None,
    *,
    response_id: str = "resp_test",
    status: str = "completed",
    refusal: bool = False,
    incomplete_reason: str | None = None,
):
    content = [SimpleNamespace(type="refusal", refusal="declined")] if refusal else []
    output = [SimpleNamespace(type="message", content=content)]
    details = (
        SimpleNamespace(reason=incomplete_reason) if incomplete_reason is not None else None
    )
    return SimpleNamespace(
        id=response_id,
        status=status,
        incomplete_details=details,
        output=output,
        output_parsed=parsed,
    )


class FakeResponses:
    def __init__(self, values: list[object]) -> None:
        self.values = list(values)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if not self.values:
            raise AssertionError("unexpected extra Responses API call")
        return self.values.pop(0)


class RaisingResponses:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        raise TimeoutError("synthetic timeout")


def client(values: list[object]):
    responses = FakeResponses(values)
    return SimpleNamespace(responses=responses), responses


def checked_in_registry() -> dict:
    return json.loads(
        Path("specs/memory_v1_predicate_registry_v5.json").read_text(encoding="utf-8")
    )


def run(fake_client, *, registry: dict | None = None):
    return asyncio.run(
        run_specialized_zero_write(
            fake_client,
            model="gpt-synthetic",
            owner_user_id=OWNER,
            source_external_id=SOURCE_ID,
            source_recorded_at=OBSERVED_AT,
            text=SOURCE,
            registry=registry,
        )
    )


class SpecializedV5OrchestrationTest(unittest.TestCase):
    def test_saved_model_packet_replay_applies_only_sibling_role_ordering(self) -> None:
        text = "I have three sisters, Cindy one year older Lori and Heidi."
        graph = sibling_graph_packet(text, ["Cindy", "Lori", "Heidi"])
        model_packet = {
            "entity_mentions": [
                item.model_dump(mode="json") for item in graph.entity_mentions
            ],
            "observations": [
                item.model_dump(mode="json")
                for item in graph.relationship_observations
            ],
            "comparison_hints": [],
            "deferrals": [],
            "packet_findings": [],
        }
        source_sha256 = sha256_text(text)
        source = {
            "job_id": "11111111-1111-4111-8111-111111111111",
            "source_external_id": SOURCE_ID,
            "source_sha256": source_sha256,
            "source_recorded_at": OBSERVED_AT,
        }
        result = replay_model_packet(
            saved_row={
                "case_id": "synthetic-siblings",
                "source_external_id": SOURCE_ID,
                "source_sha256": source_sha256,
                "model_packet": model_packet,
            },
            case={
                "case_id": "synthetic-siblings",
                "expected": {
                    "outcome": "extract",
                    "required_projection_classes": ["direct_claim"],
                    "required_entity_roles": [
                        "family:sister:1",
                        "family:sister:2",
                        "family:sister:3",
                    ],
                    "required_predicate_families": ["relationship.sibling_of"],
                    "required_temporal_features": ["open_state_validity"],
                    "required_comparison_relations": [],
                    "required_deferrals": [],
                    "forbidden_projection_classes": [],
                    "forbidden_predicates": [],
                    "require_manual_review": True,
                },
            },
            manifest_source=source,
            live_source={"source_external_id": SOURCE_ID, "text": text},
            registry=checked_in_registry(),
        )
        self.assertTrue(result["evaluation"]["passed"])
        self.assertEqual(len(result["role_changes"]), 3)
        self.assertEqual(
            [item["after"] for item in result["role_changes"]],
            ["family:sister:1", "family:sister:2", "family:sister:3"],
        )

    def test_repeated_sibling_roles_are_numbered_by_exact_source_order(self) -> None:
        text = "I have three sisters, Cindy one year older Lori and Heidi."
        packet = sibling_graph_packet(text, ["Cindy", "Lori", "Heidi"])
        normalized = normalize_repeated_sibling_roles(packet, text)
        self.assertEqual(
            [item.relationship_role for item in normalized.entity_mentions[1:]],
            ["family:sister:1", "family:sister:2", "family:sister:3"],
        )
        self.assertIn(
            "repeated_sibling_roles_source_ordered", normalized.packet_findings
        )

    def test_explicit_plural_sibling_residence_is_server_resolved(self) -> None:
        text = (
            "I have three sisters, Cindy one year older Lori and Heidi; "
            "they all live in the Green Bay area."
        )
        packet = sibling_graph_packet(text, ["Cindy", "Lori", "Heidi"])
        normalize_repeated_sibling_roles(packet, text)
        normalized = normalize_plural_sibling_residence(packet, text)
        places = [
            item for item in normalized.entity_mentions if item.entity_type == "place"
        ]
        residence = [
            item
            for item in normalized.relationship_observations
            if item.predicate == "residence.lives_at"
        ]
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0].source_spans[0].quote, "Green Bay area")
        self.assertEqual(len(residence), 3)
        self.assertEqual(
            {item.object.entity_ref for item in residence}, {places[0].entity_ref}
        )
        self.assertEqual(
            [item.source_spans[0].quote for item in residence],
            ["they all live in the Green Bay area"] * 3,
        )
        self.assertIn(
            "plural_sibling_residence_server_resolved",
            normalized.packet_findings,
        )

    def test_plural_residence_fails_closed_without_linked_sibling_group(self) -> None:
        text = "I know Cindy and Lori; they all live in the area."
        packet = sibling_graph_packet(text, ["Cindy", "Lori"])
        packet.relationship_observations = []
        normalized = normalize_plural_sibling_residence(packet, text)
        self.assertFalse(
            any(
                item.predicate == "residence.lives_at"
                for item in normalized.relationship_observations
            )
        )

    def test_sibling_numbering_fails_closed_without_distinct_source_positions(self) -> None:
        text = "I have three sisters, Cindy and Lori."
        packet = sibling_graph_packet(
            text, ["Cindy", "Lori"], shared_entity_span=True
        )
        normalized = normalize_repeated_sibling_roles(packet, text)
        self.assertEqual(
            [item.relationship_role for item in normalized.entity_mentions[1:]],
            ["family:sister", "family:sister"],
        )
        self.assertNotIn(
            "repeated_sibling_roles_source_ordered", normalized.packet_findings
        )

        single_text = "I have a sister, Cindy."
        single = sibling_graph_packet(single_text, ["Cindy"])
        normalize_repeated_sibling_roles(single, single_text)
        self.assertEqual(single.entity_mentions[1].relationship_role, "family:sister")

    def test_sibling_numbering_requires_user_self_relationships(self) -> None:
        text = "I know sisters Cindy and Lori."
        packet = sibling_graph_packet(text, ["Cindy", "Lori"])
        packet.relationship_observations[0].subject_entity_ref = "e03"
        packet.relationship_observations[1].subject_entity_ref = "e02"
        normalized = normalize_repeated_sibling_roles(packet, text)
        self.assertEqual(
            [item.relationship_role for item in normalized.entity_mentions[1:]],
            ["family:sister", "family:sister"],
        )
        self.assertNotIn(
            "repeated_sibling_roles_source_ordered", normalized.packet_findings
        )

    def test_sibling_source_order_normalization_runs_in_three_pass_pipeline(self) -> None:
        text = "I have three sisters, Cindy one year older Lori and Heidi."
        graph = sibling_graph_packet(text, ["Cindy", "Lori", "Heidi"])
        content = TemporalContentPassPacket(
            observations=[], comparison_hints=[], deferrals=[], packet_findings=[]
        )
        project = ProjectKnowledgePassPacket(
            observations=[], deferrals=[], packet_findings=[]
        )
        fake_client, responses = client(
            [response(graph), response(content), response(project)]
        )
        result = asyncio.run(
            run_specialized_zero_write(
                fake_client,
                model="gpt-synthetic",
                owner_user_id=OWNER,
                source_external_id=SOURCE_ID,
                source_recorded_at=OBSERVED_AT,
                text=text,
                registry=checked_in_registry(),
            )
        )
        self.assertEqual(len(responses.calls), 3)
        sibling_roles = [
            item.relationship_role
            for item in result.packet.entity_mentions
            if str(item.relationship_role or "").startswith("family:sister")
        ]
        self.assertEqual(
            sibling_roles,
            ["family:sister:1", "family:sister:2", "family:sister:3"],
        )

    def test_redundant_invalid_span_is_pruned_only_when_valid_span_remains(self) -> None:
        packet = graph_packet()
        packet.entity_mentions[0].source_spans.append(
            packet.entity_mentions[0].source_spans[0].model_copy(
                update={"start": 0, "end": 9, "quote": "not there"}
            )
        )
        normalized = normalize_redundant_source_spans(packet, SOURCE)
        self.assertEqual(len(normalized.entity_mentions[0].source_spans), 1)
        self.assertIn(
            "redundant_invalid_source_spans_pruned", normalized.packet_findings
        )

        packet.entity_mentions[0].source_spans = [
            packet.entity_mentions[0].source_spans[0].model_copy(
                update={"start": 0, "end": 9, "quote": "not there"}
            )
        ]
        normalized = normalize_redundant_source_spans(packet, SOURCE)
        self.assertEqual(len(normalized.entity_mentions[0].source_spans), 1)
        self.assertNotIn(
            "redundant_invalid_source_spans_pruned", normalized.packet_findings
        )

    def test_self_contained_pet_name_correction_is_server_normalized(self) -> None:
        text = (
            "this one correction they're my first cat's name was neko There was a "
            "voice to text error so can you correct that?"
        )
        graph = EntityGraphPassPacket(
            entity_mentions=[],
            relationship_observations=[],
            deferrals=[],
            packet_findings=[],
        )
        correction = normalize_explicit_pet_name_correction_graph(graph, text)
        self.assertIsNotNone(correction)
        self.assertEqual(
            graph.entity_mentions[0].relationship_role,
            "pet:corrected_name_subject",
        )
        content = TemporalContentPassPacket.model_validate(
            {
                "observations": [],
                "comparison_hints": [],
                "deferrals": [
                    {
                        "reason_code": "context_missing",
                        "memory_shape": "correction",
                        "source_spans": [
                            {
                                "start": 5,
                                "end": 19,
                                "quote": "one correction",
                            }
                        ],
                        "sensitivity": "low",
                    },
                    {
                        "reason_code": "question_only",
                        "memory_shape": "correction",
                        "source_spans": [
                            {
                                "start": text.index("can you"),
                                "end": len(text),
                                "quote": "can you correct that?",
                            }
                        ],
                        "sensitivity": "low",
                    },
                ],
                "packet_findings": [],
            }
        )
        normalized = normalize_explicit_pet_name_correction_content(
            content, correction
        )
        self.assertEqual(
            [item.predicate for item in normalized.observations],
            ["identity.name_canonical"],
        )
        self.assertEqual(normalized.deferrals, [])
        self.assertEqual(
            {item.relation_type for item in normalized.comparison_hints},
            {"corrects", "supersedes"},
        )

    def test_explicit_pet_name_correction_runs_through_all_three_passes(self) -> None:
        text = (
            "this one correction they're my first cat's name was neko There was a "
            "voice to text error so can you correct that?"
        )
        correction_start = text.index("one correction")
        question_start = text.index("can you")
        graph = EntityGraphPassPacket(
            entity_mentions=[],
            relationship_observations=[],
            deferrals=[],
            packet_findings=[],
        )
        content = TemporalContentPassPacket.model_validate(
            {
                "observations": [],
                "comparison_hints": [],
                "deferrals": [
                    {
                        "reason_code": "context_missing",
                        "memory_shape": "correction",
                        "source_spans": [
                            {
                                "start": correction_start,
                                "end": correction_start + len("one correction"),
                                "quote": "one correction",
                            }
                        ],
                        "sensitivity": "low",
                    },
                    {
                        "reason_code": "question_only",
                        "memory_shape": "correction",
                        "source_spans": [
                            {
                                "start": question_start,
                                "end": len(text),
                                "quote": "can you correct that?",
                            }
                        ],
                        "sensitivity": "low",
                    },
                ],
                "packet_findings": [],
            }
        )
        project = ProjectKnowledgePassPacket.model_validate(
            {
                "observations": [],
                "deferrals": [
                    {
                        "reason_code": "question_only",
                        "memory_shape": "none",
                        "source_spans": [
                            {
                                "start": question_start,
                                "end": len(text),
                                "quote": "can you correct that?",
                            }
                        ],
                        "sensitivity": "low",
                    }
                ],
                "packet_findings": [],
            }
        )
        fake_client, responses = client(
            [response(graph), response(content), response(project)]
        )
        result = asyncio.run(
            run_specialized_zero_write(
                fake_client,
                model="gpt-synthetic",
                owner_user_id=OWNER,
                source_external_id=SOURCE_ID,
                source_recorded_at=OBSERVED_AT,
                text=text,
                registry=checked_in_registry(),
            )
        )
        self.assertEqual(len(responses.calls), 3)
        self.assertEqual(
            [item.predicate for item in result.packet.observations],
            ["identity.name_canonical"],
        )
        self.assertEqual(result.packet.deferrals, [])
        self.assertEqual(
            {item.relation_type for item in result.packet.comparison_hints},
            {"corrects", "supersedes"},
        )
        self.assertEqual(
            result.packet.entity_mentions[0].relationship_role,
            "pet:corrected_name_subject",
        )

    def test_uncertain_credential_and_nonemployment_defer_occupation(self) -> None:
        text = (
            "I became a personal trainer through education, but I don't "
            "actually do it for a living. I think through Example Academy."
        )
        self_span = {"start": 0, "end": 1, "quote": "I"}
        concept_start = text.index("personal trainer")
        organization_start = text.index("Example Academy")
        graph = EntityGraphPassPacket.model_validate(
            {
                "entity_mentions": [
                    {
                        "entity_ref": "e01",
                        "entity_type": "self",
                        "mention_kind": "self_reference",
                        "name_text": None,
                        "relationship_role": "user:self",
                        "source_spans": [self_span],
                        "extraction_confidence": 0.99,
                        "reason_codes": ["direct_entity_mention"],
                    },
                    {
                        "entity_ref": "e02",
                        "entity_type": "concept",
                        "mention_kind": "named",
                        "name_text": "personal trainer",
                        "relationship_role": None,
                        "source_spans": [
                            {
                                "start": concept_start,
                                "end": concept_start + len("personal trainer"),
                                "quote": "personal trainer",
                            }
                        ],
                        "extraction_confidence": 0.9,
                        "reason_codes": ["direct_entity_mention"],
                    },
                    {
                        "entity_ref": "e03",
                        "entity_type": "organization",
                        "mention_kind": "named",
                        "name_text": "Example Academy",
                        "relationship_role": None,
                        "source_spans": [
                            {
                                "start": organization_start,
                                "end": organization_start + len("Example Academy"),
                                "quote": "Example Academy",
                            }
                        ],
                        "extraction_confidence": 0.7,
                        "reason_codes": ["direct_entity_mention"],
                    },
                ],
                "relationship_observations": [],
                "deferrals": [],
                "packet_findings": [],
            }
        )
        observation = content_packet().observations[0]
        observation.predicate = "occupation.works_as"
        observation.object = EntityObject(kind="entity", entity_ref="e02")
        content = TemporalContentPassPacket(
            observations=[observation],
            comparison_hints=[],
            deferrals=[],
            packet_findings=[],
        )
        normalized = normalize_uncertain_credential_deferral(content, graph, text)
        normalized = normalize_predicate_entailment_deferrals(normalized, text)
        self.assertEqual(normalized.observations, [])
        self.assertEqual(
            [item.reason_code for item in normalized.deferrals],
            ["ambiguous_transcription", "source_contradicts_predicate"],
        )
        self.assertIn(
            "memory_v1_predicate_entailment_v5_1_deferred_observation",
            normalized.packet_findings,
        )

    def test_known_technical_term_is_not_an_ambiguous_personal_memory(self) -> None:
        text = "How good is Codex at changing an app or do I need a programmer?"
        codex_start = text.index("Codex")
        packet = TemporalContentPassPacket.model_validate(
            {
                "observations": [],
                "comparison_hints": [],
                "deferrals": [
                    {
                        "reason_code": "question_only",
                        "memory_shape": "none",
                        "source_spans": [
                            {"start": 0, "end": len(text), "quote": text}
                        ],
                        "sensitivity": "low",
                    },
                    {
                        "reason_code": "context_missing",
                        "memory_shape": "none",
                        "source_spans": [
                            {"start": 0, "end": len(text), "quote": text}
                        ],
                        "sensitivity": "low",
                    },
                    {
                        "reason_code": "ambiguous_transcription",
                        "memory_shape": "none",
                        "source_spans": [
                            {
                                "start": codex_start,
                                "end": codex_start + len("Codex"),
                                "quote": "Codex",
                            }
                        ],
                        "sensitivity": "low",
                    },
                ],
                "packet_findings": [],
            }
        )
        personal_packet = packet.model_copy(deep=True)
        normalized = normalize_known_technical_question_deferrals(packet, text)
        self.assertEqual(
            [item.reason_code for item in normalized.deferrals],
            ["question_only", "context_missing"],
        )
        self.assertIn(
            "known_technical_term_ambiguity_deferral_removed",
            normalized.packet_findings,
        )

        personal = "Was my cat named Codex?"
        personal_packet.deferrals[0].source_spans = [
            personal_packet.deferrals[0].source_spans[0].model_copy(
                update={"start": 0, "end": len(personal), "quote": personal}
            )
        ]
        normalized = normalize_known_technical_question_deferrals(
            personal_packet, personal
        )
        self.assertTrue(
            any(
                item.reason_code == "ambiguous_transcription"
                for item in normalized.deferrals
            )
        )

    def test_project_current_state_temporal_is_server_authoritative(self) -> None:
        packet = project_packet()
        packet.observations[0].predicate = "project.current_state"
        packet.observations[0].temporal = packet.observations[0].temporal.model_copy(
            update={
                "semantic": "state_validity",
                "shape": "bounded_interval",
                "basis": "instant",
                "source_form": "implicit_source_time",
                "instant_range": {
                    "lower": "2026-01-01T00:00:00Z",
                    "upper": "2026-02-01T00:00:00Z",
                    "bounds": "[)",
                },
            }
        )
        normalized = normalize_project_current_state_temporal(packet, OBSERVED_AT)
        value = normalized.observations[0].temporal
        self.assertEqual(value.shape, "open_interval")
        self.assertEqual(value.instant_range.lower.isoformat(), "2026-07-15T12:00:00+00:00")
        self.assertIsNone(value.instant_range.upper)
        self.assertTrue(value.anchored_to_source_time)

    def test_project_current_state_preserves_explicit_temporal_evidence(self) -> None:
        packet = project_packet()
        packet.observations[0].predicate = "project.current_state"
        explicit = Temporal.model_validate(
            {
                "semantic": "state_validity",
                "shape": "bounded_interval",
                "basis": "instant",
                "source_form": "absolute",
                "certainty": "bounded",
                "precision": "exact",
                "instant": None,
                "calendar_range": None,
                "instant_range": {
                    "lower": datetime(2026, 1, 1, tzinfo=timezone.utc),
                    "upper": datetime(2026, 2, 1, tzinfo=timezone.utc),
                    "bounds": "[)",
                },
                "relative_offset": None,
                "recurrence": None,
                "anchored_to_source_time": False,
                "reason_codes": ["explicit_project_interval"],
            }
        )
        packet.observations[0].temporal = explicit
        normalized = normalize_project_current_state_temporal(packet, OBSERVED_AT)
        self.assertEqual(normalized.observations[0].temporal, explicit)
        self.assertNotIn(
            "project_current_state_temporal_server_anchored",
            normalized.packet_findings,
        )

    def test_graph_validator_rejects_noncanonical_self_before_assembly(self) -> None:
        packet = graph_packet()
        packet.entity_mentions[0].relationship_role = None
        reasons = validate_entity_graph_pass(packet, SOURCE)
        self.assertIn("self_entity_role:e01:user:self_required", reasons)

        packet.entity_mentions[0].relationship_role = "user:self"
        packet.entity_mentions[0].source_spans = [
            packet.entity_mentions[0].source_spans[0].model_copy(
                update=span("application")
            )
        ]
        reasons = validate_entity_graph_pass(packet, SOURCE)
        self.assertIn(
            "self_entity_grounding:e01:first_person_span_required",
            reasons,
        )

    def test_project_scope_deferral_without_observation_forces_repair(self) -> None:
        packet = ProjectKnowledgePassPacket.model_validate(
            {
                "observations": [],
                "deferrals": [
                    {
                        "reason_code": "project_scope_unresolved",
                        "memory_shape": "project_knowledge",
                        "source_spans": [span("My application")],
                        "sensitivity": "medium",
                    }
                ],
                "packet_findings": [],
            }
        )
        self.assertIn(
            "project_scope_deferral_without_observation",
            validate_project_knowledge_pass(packet, SOURCE),
        )

        payload = packet.model_dump(mode="python")
        payload["deferrals"][0]["reason_code"] = "question_only"
        payload["deferrals"][0]["memory_shape"] = "none"
        reasons = validate_project_knowledge_pass(
            ProjectKnowledgePassPacket.model_validate(payload), SOURCE
        )
        self.assertIn(
            "non_project_deferral_in_project_pass:0:question_only",
            reasons,
        )

    def test_project_pass_discards_deferrals_owned_by_temporal_content(self) -> None:
        packet = ProjectKnowledgePassPacket.model_validate(
            {
                "observations": [],
                "deferrals": [
                    {
                        "reason_code": "question_only",
                        "memory_shape": "none",
                        "source_spans": [span("application")],
                        "sensitivity": "low",
                    }
                ],
                "packet_findings": [],
            }
        )
        normalized = normalize_project_lane_deferrals(packet)
        self.assertEqual(normalized.deferrals, [])
        self.assertEqual(validate_project_knowledge_pass(normalized, SOURCE), [])
        self.assertIn("non_project_lane_deferrals_removed", normalized.packet_findings)

    def test_three_pass_order_store_false_and_owner_not_exposed(self) -> None:
        fake_client, responses = client(
            [
                response(graph_packet(), response_id="resp_graph"),
                response(content_packet(), response_id="resp_content"),
                response(project_packet(), response_id="resp_project"),
            ]
        )
        result = run(fake_client, registry=checked_in_registry())
        self.assertEqual(len(responses.calls), 3)
        self.assertEqual(
            [item["metadata"]["pass"] for item in responses.calls],
            ["entity_graph", "temporal_content", "project_knowledge"],
        )
        self.assertTrue(all(item["store"] is False for item in responses.calls))
        self.assertNotIn("ENTITY CATALOG", responses.calls[0]["input"])
        self.assertIn("SERVER-VALIDATED SOURCE-LOCAL ENTITY CATALOG", responses.calls[1]["input"])
        self.assertNotIn("ENTITY CATALOG", responses.calls[2]["input"])
        self.assertIn("relationship.parent_of", responses.calls[0]["instructions"])
        self.assertNotIn("preference.response", responses.calls[0]["instructions"])
        self.assertIn("preference.response", responses.calls[1]["instructions"])
        self.assertNotIn("project.requirement", responses.calls[1]["instructions"])
        self.assertIn("project.requirement", responses.calls[2]["instructions"])
        self.assertNotIn("preference.response", responses.calls[2]["instructions"])
        serialized_calls = repr(responses.calls)
        self.assertNotIn(OWNER, serialized_calls)
        self.assertNotIn(SOURCE_ID, serialized_calls)
        self.assertTrue(
            all(item["safety_identifier"] == sha256_text(OWNER) for item in responses.calls)
        )
        self.assertEqual(len(result.packet.observations), 2)
        self.assertEqual(result.packet.entity_mentions[-1].entity_type, "project")
        self.assertEqual([item.selected for item in result.attempts], [True, True, True])
        self.assertNotIn(OWNER, result.model_dump_json())

    def test_refusal_fails_closed_before_next_pass(self) -> None:
        fake_client, responses = client([response(refusal=True)])
        with self.assertRaises(SpecializedExtractionError) as caught:
            run(fake_client)
        self.assertEqual(len(responses.calls), 1)
        self.assertTrue(caught.exception.attempts[0].refusal)
        self.assertFalse(caught.exception.attempts[0].selected)

    def test_incomplete_response_fails_closed_before_next_pass(self) -> None:
        fake_client, responses = client(
            [response(status="incomplete", incomplete_reason="max_output_tokens")]
        )
        with self.assertRaises(SpecializedExtractionError) as caught:
            run(fake_client)
        self.assertEqual(len(responses.calls), 1)
        audit = caught.exception.attempts[0]
        self.assertEqual(audit.response_status, "incomplete")
        self.assertEqual(audit.incomplete_reason, "max_output_tokens")

    def test_missing_parsed_output_fails_closed(self) -> None:
        fake_client, responses = client([response(parsed=None)])
        with self.assertRaisesRegex(SpecializedExtractionError, "no parsed output"):
            run(fake_client)
        self.assertEqual(len(responses.calls), 1)

    def test_request_error_fails_closed_with_non_sensitive_audit(self) -> None:
        responses = RaisingResponses()
        fake_client = SimpleNamespace(responses=responses)
        with self.assertRaisesRegex(SpecializedExtractionError, "TimeoutError") as caught:
            run(fake_client)
        self.assertEqual(len(responses.calls), 1)
        audit = caught.exception.attempts[0]
        self.assertEqual(audit.response_status, "request_error")
        self.assertEqual(audit.validation_reasons, ["request_error:TimeoutError"])
        self.assertNotIn("synthetic timeout", audit.model_dump_json())

    def test_one_validation_repair_is_selected_only_when_strictly_better(self) -> None:
        fake_client, responses = client(
            [
                response(graph_packet(invalid_project=True), response_id="resp_bad"),
                response(graph_packet(), response_id="resp_repaired"),
                response(content_packet()),
                response(project_packet()),
            ]
        )
        result = run(fake_client)
        self.assertEqual(len(responses.calls), 4)
        graph_attempts = [item for item in result.attempts if item.pass_name == "entity_graph"]
        self.assertEqual([item.attempt for item in graph_attempts], ["initial", "repair"])
        self.assertEqual([item.selected for item in graph_attempts], [False, True])
        self.assertGreater(graph_attempts[0].quality, graph_attempts[1].quality)
        repair_instructions = responses.calls[1]["instructions"].lower()
        self.assertIn("project_entity_in_graph", repair_instructions)
        self.assertNotIn("case_id", repair_instructions)
        self.assertNotIn("expected outcome", repair_instructions)

    def test_registry_violation_triggers_one_pass_local_repair(self) -> None:
        fake_client, responses = client(
            [
                response(graph_packet()),
                response(content_packet()),
                response(project_packet(sensitivity="low"), response_id="resp_project_bad"),
                response(project_packet(), response_id="resp_project_repaired"),
            ]
        )
        result = run(fake_client, registry=checked_in_registry())
        self.assertEqual(len(responses.calls), 4)
        project_attempts = [
            item for item in result.attempts if item.pass_name == "project_knowledge"
        ]
        self.assertEqual([item.selected for item in project_attempts], [False, True])
        self.assertIn(
            "registry:o01:sensitivity_floor",
            project_attempts[0].validation_reasons,
        )
        self.assertIn(
            "registry:o01:sensitivity_floor",
            responses.calls[3]["instructions"],
        )

    def test_no_improvement_stops_after_second_call(self) -> None:
        fake_client, responses = client(
            [
                response(graph_packet(invalid_project=True), response_id="resp_bad_1"),
                response(graph_packet(invalid_project=True), response_id="resp_bad_2"),
            ]
        )
        with self.assertRaisesRegex(SpecializedExtractionError, "one bounded repair") as caught:
            run(fake_client)
        self.assertEqual(len(responses.calls), 2)
        self.assertEqual([item.selected for item in caught.exception.attempts], [True, False])

    def test_empty_source_is_zero_call_and_zero_observation(self) -> None:
        fake_client, responses = client([])
        result = asyncio.run(
            run_specialized_zero_write(
                fake_client,
                model="gpt-synthetic",
                owner_user_id=OWNER,
                source_external_id=SOURCE_ID,
                source_recorded_at=OBSERVED_AT,
                text="  \n",
            )
        )
        self.assertEqual(responses.calls, [])
        self.assertEqual(result.skipped_reason, "empty_source")
        self.assertEqual(result.packet.observations, [])

    def test_orchestrator_has_no_persistence_or_runtime_client_construction(self) -> None:
        path = Path(inspect.getfile(run_specialized_zero_write))
        source = path.read_text(encoding="utf-8").lower()
        for forbidden in (
            "import psycopg",
            "from psycopg",
            "qdrantclient(",
            "make_qdrant_client(",
            "insert into",
            "update ",
            "delete from",
            "upsert",
            "openai(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
