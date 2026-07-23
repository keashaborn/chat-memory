from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.memory_v1_predicate_runtime_profile_v2 import load_runtime_profile_v2
from scripts.memory_v1_relational_extraction_v5_provider import (
    SyntheticFixtureProvider,
    TrustedExtractionSource,
    load_registry,
    load_schema,
    sha256_text,
    validate_and_normalize,
)


CASES_PATH = ROOT / "evals/memory_v1_semantic_coverage_v5_2_cases.jsonl"
SOURCE_RECORDED_AT = "2026-07-21T12:00:00+00:00"
NORMALIZED_SOURCE_RECORDED_AT = "2026-07-21T12:00:00.000000Z"


def span(text: str, quote: str) -> dict[str, Any]:
    start = text.index(quote)
    return {"start": start, "end": start + len(quote), "quote": quote}


def temporal_none(semantic: str) -> dict[str, Any]:
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


def past_state_temporal() -> dict[str, Any]:
    return {
        "semantic": "state_validity",
        "shape": "open_interval",
        "basis": "instant",
        "source_form": "implicit_source_time",
        "certainty": "bounded",
        "precision": "unknown",
        "instant": None,
        "calendar_range": None,
        "instant_range": {"lower": None, "upper": None, "bounds": "[)"},
        "relative_offset": None,
        "recurrence": None,
        "anchored_to_source_time": False,
        "reason_codes": ["historical_relationship_ended_before_source"],
    }


def entity(
    text: str,
    *,
    ref: str,
    entity_type: str,
    quote: str,
    mention_kind: str,
    name_text: str | None,
    relationship_role: str | None,
) -> dict[str, Any]:
    return {
        "entity_ref": ref,
        "entity_type": entity_type,
        "mention_kind": mention_kind,
        "name_text": name_text,
        "relationship_role": relationship_role,
        "source_spans": [span(text, quote)],
        "extraction_confidence": 0.99,
        "reason_codes": ["synthetic_semantic_fixture"],
    }


def self_entity(text: str, quote: str = "I") -> dict[str, Any]:
    return entity(
        text,
        ref="e00",
        entity_type="self",
        quote=quote,
        mention_kind="self_reference",
        name_text=None,
        relationship_role="user:self",
    )


def literal(value: Any, datatype: str = "text") -> dict[str, Any]:
    return {
        "kind": "literal",
        "datatype": datatype,
        "value": value,
        "unit": None,
        "approximate": False,
    }


def entity_object(ref: str) -> dict[str, str]:
    return {"kind": "entity", "entity_ref": ref}


def observation(
    text: str,
    *,
    ref: str,
    subject: str,
    predicate: str,
    obj: dict[str, Any],
    modality: str = "asserted",
    projection_class: str = "direct_claim",
    surface_policy: str = "direct_or_relevant",
    temporal: dict[str, Any] | None = None,
    sensitivity: str = "medium",
) -> dict[str, Any]:
    return {
        "observation_ref": ref,
        "subject_entity_ref": subject,
        "predicate": predicate,
        "object": obj,
        "polarity": "affirmed",
        "modality": modality,
        "projection_class": projection_class,
        "surface_policy": surface_policy,
        "temporal": temporal or temporal_none("observation_time"),
        "sensitivity": sensitivity,
        "extraction_confidence": 0.98,
        "source_spans": [span(text, text)],
        "reason_codes": ["synthetic_semantic_fixture"],
    }


def packet(
    entities: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    *,
    deferrals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "entity_mentions": entities,
        "observations": observations,
        "comparison_hints": [],
        "deferrals": deferrals or [],
        "packet_findings": ["synthetic_semantic_coverage"],
    }


def fixture(case: dict[str, Any]) -> dict[str, Any]:
    text = case["text"]
    kind = case["fixture_kind"]
    if kind in {"past_occupation", "current_occupation"}:
        role = "truck driver" if kind == "past_occupation" else "clinical psychologist"
        entities = [
            self_entity(text),
            entity(
                text,
                ref="e01",
                entity_type="concept",
                quote=role,
                mention_kind="named",
                name_text=role,
                relationship_role=None,
            ),
        ]
        return packet(
            entities,
            [
                observation(
                    text,
                    ref="o01",
                    subject="e00",
                    predicate="occupation.works_as",
                    obj=entity_object("e01"),
                    temporal=(
                        past_state_temporal()
                        if kind == "past_occupation"
                        else temporal_none("state_validity")
                    ),
                )
            ],
        )
    if kind == "education_attended":
        organization = "Luxemburg-Casco High School"
        return packet(
            [
                self_entity(text),
                entity(
                    text,
                    ref="e01",
                    entity_type="organization",
                    quote=organization,
                    mention_kind="named",
                    name_text=organization,
                    relationship_role="school",
                ),
            ],
            [
                observation(
                    text,
                    ref="o01",
                    subject="e00",
                    predicate="education.attended",
                    obj=entity_object("e01"),
                    modality="reported_observation",
                    temporal=temporal_none("occurrence"),
                )
            ],
        )
    if kind == "employment_worked_for":
        organization = "Wisconsin Early Autism Project"
        return packet(
            [
                self_entity(text),
                entity(
                    text,
                    ref="e01",
                    entity_type="organization",
                    quote=organization,
                    mention_kind="named",
                    name_text=organization,
                    relationship_role="employer",
                ),
            ],
            [
                observation(
                    text,
                    ref="o01",
                    subject="e00",
                    predicate="employment.worked_for",
                    obj=entity_object("e01"),
                    temporal=temporal_none("state_validity"),
                )
            ],
        )
    if kind == "credential":
        return packet(
            [self_entity(text)],
            [
                observation(
                    text,
                    ref="o01",
                    subject="e00",
                    predicate="credential.reported",
                    obj=literal("BCBA"),
                    modality="reported_observation",
                )
            ],
        )
    if kind == "reported_stance":
        position = text.removeprefix("I think ").removeprefix("I do not believe ")
        topic = "public opinion and evidence" if "public opinion" in text else "serotonin imbalance explanation"
        return packet(
            [self_entity(text)],
            [
                observation(
                    text,
                    ref="o01",
                    subject="e00",
                    predicate="stance.reported",
                    obj=literal(
                        {
                            "topic_key": (
                                "epistemology.public_opinion_evidence"
                                if "public opinion" in text
                                else "health.serotonin_imbalance_explanation"
                            ),
                            "topic_text": topic,
                            "position": position,
                            "orientation": "opposes",
                            "context": None,
                        },
                        "json",
                    ),
                    modality="reported_belief",
                    projection_class="reported_stance",
                    surface_policy="relevant_recall_or_explicit_recall",
                )
            ],
        )
    if kind == "objective_observation":
        return packet(
            [self_entity(text, "My")],
            [
                observation(
                    text,
                    ref="o01",
                    subject="e00",
                    predicate="health.user_reported_observation",
                    obj=literal("blood pressure measured 120 over 80 today"),
                    modality="reported_observation",
                    projection_class="supportive_context",
                    surface_policy="mention_when_directly_relevant",
                    sensitivity="high",
                )
            ],
        )
    if kind in {"family_loss", "pet_loss"}:
        is_family = kind == "family_loss"
        named = "DeeDee" if is_family else "Neko"
        related_type = "person" if is_family else "animal"
        relation = "relationship.parent_of" if is_family else "relationship.has_pet"
        relation_subject = "e01" if is_family else "e00"
        relation_object = "e00" if is_family else "e01"
        entities = [
            self_entity(text, "My"),
            entity(
                text,
                ref="e01",
                entity_type=related_type,
                quote=named,
                mention_kind="named",
                name_text=named,
                relationship_role="mother" if is_family else "pet",
            ),
        ]
        observations = [
            observation(
                text,
                ref="o01",
                subject="e01",
                predicate="identity.name",
                obj=literal(named),
            ),
            observation(
                text,
                ref="o02",
                subject=relation_subject,
                predicate=relation,
                obj=entity_object(relation_object),
                temporal=temporal_none("state_validity"),
                sensitivity="medium" if is_family else "low",
            ),
            observation(
                text,
                ref="o03",
                subject="e01",
                predicate="life_event.died",
                obj=literal(True, "boolean"),
                modality="reported_observation",
                projection_class="supportive_context",
                surface_policy="mention_when_directly_relevant",
                temporal=temporal_none("occurrence"),
                sensitivity="high",
            ),
        ]
        return packet(entities, observations)
    if kind == "self_and_pet_names":
        return packet(
            [
                self_entity(text, "My"),
                entity(
                    text,
                    ref="e01",
                    entity_type="animal",
                    quote="Neko",
                    mention_kind="named",
                    name_text="Neko",
                    relationship_role="pet",
                ),
            ],
            [
                observation(
                    text,
                    ref="o01",
                    subject="e00",
                    predicate="identity.name",
                    obj=literal("Eric"),
                ),
                observation(
                    text,
                    ref="o02",
                    subject="e01",
                    predicate="identity.name",
                    obj=literal("Neko"),
                ),
                observation(
                    text,
                    ref="o03",
                    subject="e00",
                    predicate="relationship.has_pet",
                    obj=entity_object("e01"),
                    temporal=temporal_none("state_validity"),
                    sensitivity="low",
                ),
            ],
        )
    if kind == "transient_deferral":
        return packet(
            [self_entity(text)],
            [],
            deferrals=[
                {
                    "reason_code": "transient_state",
                    "memory_shape": "supportive_context",
                    "source_spans": [span(text, text)],
                    "sensitivity": "medium",
                }
            ],
        )
    if kind == "response_preference":
        return packet(
            [self_entity(text)],
            [
                observation(
                    text,
                    ref="o01",
                    subject="e00",
                    predicate="preference.response",
                    obj=literal(
                        {"dimension": "response_length", "value": "concise"},
                        "json",
                    ),
                    projection_class="response_preference",
                    surface_policy="zero_token_control_only",
                    temporal=temporal_none("state_validity"),
                    sensitivity="low",
                )
            ],
        )
    raise AssertionError(f"unknown fixture kind: {kind}")


def load_cases() -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in CASES_PATH.read_text().splitlines() if line]
    ids = [case["case_id"] for case in cases]
    if ids != sorted(set(ids)):
        raise AssertionError("semantic cases must be sorted and unique")
    return cases


class SemanticCoverageV52Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        if profile.review_only or profile.lifecycle != "shadow_review_staging":
            raise AssertionError(
                "V5.2 semantic eval must be bound to private shadow staging"
            )
        cls.registry = load_registry(
            profile.registry_path,
            profile.registry_artifact_sha256,
        )
        cls.schema = load_schema(
            profile.schema_path,
            profile.schema_artifact_sha256,
            expected_contract_version=profile.contract_version,
        )

    def test_all_synthetic_semantic_cases(self) -> None:
        for ordinal, case in enumerate(load_cases(), start=1):
            with self.subTest(case_id=case["case_id"]):
                text = case["text"]
                source = TrustedExtractionSource.create(
                    job_id=f"00000000-0000-4000-8000-{ordinal:012d}",
                    source_system="public.chat_log",
                    source_external_id=f"00000000-0000-4000-9000-{ordinal:012d}",
                    source_sha256=sha256_text(text),
                    source_recorded_at=SOURCE_RECORDED_AT,
                    content=text,
                )
                result = validate_and_normalize(
                    SyntheticFixtureProvider(fixture(case)),
                    source=source,
                    registry=self.registry,
                    schema=self.schema,
                )
                packet_value = result.normalized_packet
                observations = packet_value["observations"]
                expected = case["expected"]
                predicates = [item["predicate"] for item in observations]
                self.assertEqual(set(predicates), set(expected["predicates"]))
                self.assertFalse(set(predicates) & set(expected.get("forbidden_predicates", [])))
                if expected.get("temporal") == "closed_past":
                    temporal = observations[0]["temporal"]
                    self.assertIsNone(temporal["instant_range"]["lower"])
                    self.assertEqual(
                        temporal["instant_range"]["upper"],
                        NORMALIZED_SOURCE_RECORDED_AT,
                    )
                if expected.get("temporal") == "open_current":
                    temporal = observations[0]["temporal"]
                    self.assertEqual(
                        temporal["instant_range"]["lower"],
                        NORMALIZED_SOURCE_RECORDED_AT,
                    )
                    self.assertIsNone(temporal["instant_range"]["upper"])
                if "modality" in expected:
                    self.assertEqual(observations[0]["modality"], expected["modality"])
                if "projection_class" in expected:
                    self.assertEqual(
                        observations[0]["projection_class"],
                        expected["projection_class"],
                    )
                if "surface_policy" in expected:
                    self.assertEqual(
                        observations[0]["surface_policy"],
                        expected["surface_policy"],
                    )
                if "object_entity_type" in expected:
                    entities = {
                        item["entity_ref"]: item["entity_type"]
                        for item in packet_value["entity_mentions"]
                    }
                    self.assertEqual(
                        entities[observations[0]["object"]["entity_ref"]],
                        expected["object_entity_type"],
                    )
                if "death_subject" in expected:
                    entities = {
                        item["entity_ref"]: item["entity_type"]
                        for item in packet_value["entity_mentions"]
                    }
                    death = next(
                        item for item in observations if item["predicate"] == "life_event.died"
                    )
                    required_type = "person" if expected["death_subject"] == "related_person" else "animal"
                    self.assertEqual(entities[death["subject_entity_ref"]], required_type)
                if "identity_subjects" in expected:
                    entities = {
                        item["entity_ref"]: item["entity_type"]
                        for item in packet_value["entity_mentions"]
                    }
                    subjects = {
                        entities[item["subject_entity_ref"]]
                        for item in observations
                        if item["predicate"] == "identity.name"
                    }
                    self.assertEqual(subjects, set(expected["identity_subjects"]))
                if "deferral" in expected:
                    self.assertEqual(
                        [item["reason_code"] for item in packet_value["deferrals"]],
                        [expected["deferral"]],
                    )


if __name__ == "__main__":
    unittest.main()
