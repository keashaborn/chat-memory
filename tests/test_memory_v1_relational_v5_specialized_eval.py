from __future__ import annotations

import asyncio
import inspect
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.memory_v1_relational_v5_live_eval import (
    EntityMention,
    Observation,
    sha256_text,
)
from scripts.memory_v1_relational_v5_specialized import (
    EntityGraphPassPacket,
    ProjectEntityProposal,
    ProjectKnowledgePassPacket,
    ProjectObservationProposal,
    TemporalContentPassPacket,
)
from scripts.memory_v1_relational_v5_specialized_eval import (
    SpecializedExtractionError,
    run_specialized_zero_write,
)


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
        project_entity=ProjectEntityProposal(
            mention_kind="role_only",
            name_text=None,
            source_spans=[span("My application")],
            extraction_confidence=0.9,
            reason_codes=["unresolved_project_mention"],
        ),
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
            "psycopg",
            "qdrant",
            "insert into",
            "update ",
            "delete from",
            "upsert",
            "openai(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
