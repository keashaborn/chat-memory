#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from scripts.memory_v1_relational_v5_live_eval import (
    ComparisonHint,
    Deferral,
    EntityMention,
    LiteralObject,
    ModelPacket,
    Observation,
    SpanOffsets,
    StrictModel,
    Temporal,
)


GRAPH_PREDICATES = {
    "relationship.has_pet",
    "relationship.parent_of",
    "relationship.sibling_of",
    "residence.lives_at",
}
PROJECT_PREDICATES = {
    "project.constraint",
    "project.current_state",
    "project.proposed_feature",
    "project.requirement",
}


ENTITY_GRAPH_INSTRUCTIONS = """
Extract only the source-local entity graph. Return self, people, animals,
organizations, places, objects, and concepts plus atomic relationship.has_pet,
relationship.parent_of, relationship.sibling_of, and residence.lives_at
observations. Project entities belong only to the project pass. Never extract
project knowledge, attributes, health content, preferences, or corrections.
Declare every entity needed as the subject or entity-object of a later content
pass even when no graph relationship is present. In particular, declare
user:self for first-person occupation/profile facts and declare an explicitly
corrected pet-name subject as an animal with role pet:corrected_name_subject.
Every referenced entity must be declared in this packet. Use parent -> self,
self -> sibling, and person/animal -> place directions exactly. Do not infer
names, places, relationships, dates, owner identity, or durable IDs.
Every source span must copy a short exact substring from the record; never
paraphrase, normalize punctuation, or manufacture a long composite quote.
If the source has no eligible entity or graph content, return empty entity and
observation lists.
""".strip()


TEMPORAL_CONTENT_INSTRUCTIONS = """
Using only the server-supplied entity catalog, extract non-project atomic facts,
events, states, health observations and plans, preferences, corrections, and
comparison hints. Do not create entities or emit relationship/residence/project
predicates. Preserve occurrence, state_validity, planned_time, and observation
time distinctly. A planned personal or veterinary procedure is planned
supportive context, never a completed event or structured-domain value. Do not
invent dates, entity links, owner identity, project scope, durable IDs, approval,
or salience.
This pass owns global question and ambiguity deferrals. Add question_only for a
question even when a directly stated fact elsewhere in the record is extracted.
Add context_missing when approval, endorsement, or a question depends on unseen
prior content. Add transient_state for a question that only says a temporary
decision/state has not been considered yet. Add ambiguous_transcription for a
suspicious proper noun, credential, API, or voice transcription without
discarding a separate well-supported fact.
An explicit statement that the user became or is a personal trainer is
occupation.works_as even when they say it is not current paid work; represent
the occupation as a concept entity from the supplied catalog and separately
defer an uncertain credential transcription. An explicit pet-name correction
is identity.name_canonical with corrective modality and correction projection;
add both corrects and supersedes comparison hints against an owner-scoped prior
name lookup. Copy short exact source substrings for every span.
If the source has no compatible content, return empty observations and hints.
""".strip()


PROJECT_KNOWLEDGE_INSTRUCTIONS = """
Extract only project knowledge: current state, requirements, proposed features,
and constraints. Return at most one unresolved project entity proposal. A
question can coexist with a directly stated current project state. Do not emit
personal facts, preferences, health content, owner identity, trusted project
binding, durable IDs, approval, or salience. Project scope remains unresolved
for deterministic server review.
Classify a directly endorsed capability the app should have as
project.requirement, a speculative possibility as project.proposed_feature, and
a directly described implemented/present state as project.current_state. One
source may support more than one of these atomic observations. Project text
literals always use approximate=false; uncertainty belongs in modality.
Questions about external platforms, policies, or coding ability do not alone
create a project entity, observation, or project-scope deferral. If the same
record directly states what the user's app currently is or does, extract that
state while the temporal pass owns question_only. Copy short exact source
substrings for every span.
If the source has no project knowledge, return no project entity and no observations.
""".strip()


class EntityGraphPassPacket(StrictModel):
    entity_mentions: list[EntityMention] = Field(max_length=24)
    relationship_observations: list[Observation] = Field(max_length=24)
    deferrals: list[Deferral] = Field(max_length=16)
    packet_findings: list[str] = Field(max_length=16)


class TemporalContentPassPacket(StrictModel):
    observations: list[Observation] = Field(max_length=32)
    comparison_hints: list[ComparisonHint] = Field(max_length=32)
    deferrals: list[Deferral] = Field(max_length=24)
    packet_findings: list[str] = Field(max_length=16)


class ProjectEntityProposal(StrictModel):
    mention_kind: Literal["named", "role_only", "anonymous"]
    name_text: str | None
    source_spans: list[SpanOffsets] = Field(min_length=1, max_length=8)
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(min_length=1, max_length=20)


class ProjectObservationProposal(StrictModel):
    observation_ref: str = Field(pattern=r"^o[0-9]{2}$")
    predicate: Literal[
        "project.constraint",
        "project.current_state",
        "project.proposed_feature",
        "project.requirement",
    ]
    object: LiteralObject
    polarity: Literal["affirmed", "negated"]
    modality: Literal[
        "asserted",
        "endorsed",
        "planned",
        "proposed",
        "reported_observation",
        "uncertain",
    ]
    projection_class: Literal["project_knowledge"]
    surface_policy: Literal["exact_project_scope_only"]
    temporal: Temporal
    sensitivity: Literal["low", "medium", "high", "restricted"]
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    source_spans: list[SpanOffsets] = Field(min_length=1, max_length=8)
    reason_codes: list[str] = Field(min_length=1, max_length=20)


class ProjectKnowledgePassPacket(StrictModel):
    project_entity: ProjectEntityProposal | None
    observations: list[ProjectObservationProposal] = Field(max_length=24)
    deferrals: list[Deferral] = Field(max_length=16)
    packet_findings: list[str] = Field(max_length=16)


class SpecializedExecutionPlan(StrictModel):
    mode: Literal["zero_write_specialized_v5"]
    pass_order: list[Literal["entity_graph", "temporal_content", "project_knowledge"]]
    max_calls_per_pass: int = Field(ge=1, le=2)
    model_may_assign_owner: Literal[False]
    model_may_bind_project: Literal[False]
    model_may_write: Literal[False]


def execution_plan() -> SpecializedExecutionPlan:
    return SpecializedExecutionPlan(
        mode="zero_write_specialized_v5",
        pass_order=["entity_graph", "temporal_content", "project_knowledge"],
        max_calls_per_pass=2,
        model_may_assign_owner=False,
        model_may_bind_project=False,
        model_may_write=False,
    )


def entity_catalog(packet: EntityGraphPassPacket) -> list[dict[str, Any]]:
    return [
        {
            "entity_ref": item.entity_ref,
            "entity_type": item.entity_type,
            "mention_kind": item.mention_kind,
            "name_text": item.name_text,
            "relationship_role": item.relationship_role,
        }
        for item in packet.entity_mentions
    ]


def _require_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")


def _entity_refs(observation: Observation) -> set[str]:
    refs = {observation.subject_entity_ref}
    if observation.object.kind == "entity":
        refs.add(observation.object.entity_ref)
    return refs


def _next_entity_ref(existing: set[str]) -> str:
    for ordinal in range(1, 100):
        candidate = f"e{ordinal:02d}"
        if candidate not in existing:
            return candidate
    raise ValueError("specialized packet exceeds the V5 entity reference budget")


def assemble_specialized_packet(
    entity_graph: EntityGraphPassPacket,
    temporal_content: TemporalContentPassPacket,
    project_knowledge: ProjectKnowledgePassPacket,
) -> ModelPacket:
    mentions = [item.model_dump(mode="python") for item in entity_graph.entity_mentions]
    mention_refs = [item["entity_ref"] for item in mentions]
    _require_unique(mention_refs, "entity_ref")
    known_entities = set(mention_refs)
    if any(item["entity_type"] == "project" for item in mentions):
        raise ValueError("entity graph pass cannot create project entities")

    graph_observations = list(entity_graph.relationship_observations)
    content_observations = list(temporal_content.observations)
    if any(item.predicate not in GRAPH_PREDICATES for item in graph_observations):
        raise ValueError("entity graph pass emitted a non-graph predicate")
    if any(
        item.predicate in GRAPH_PREDICATES or item.predicate in PROJECT_PREDICATES
        for item in content_observations
    ):
        raise ValueError("temporal content pass crossed a governed lane boundary")
    for item in graph_observations + content_observations:
        unresolved = _entity_refs(item) - known_entities
        if unresolved:
            raise ValueError(
                "observation contains unresolved entity refs: "
                + ",".join(sorted(unresolved))
            )

    project_ref: str | None = None
    if project_knowledge.observations:
        if project_knowledge.project_entity is None:
            raise ValueError("project observations require a project entity proposal")
        if len(mentions) >= 24:
            raise ValueError("specialized packet exceeds the V5 entity budget")
        project_ref = _next_entity_ref(known_entities)
        proposal = project_knowledge.project_entity
        mentions.append(
            {
                "entity_ref": project_ref,
                "entity_type": "project",
                "mention_kind": proposal.mention_kind,
                "name_text": proposal.name_text,
                "relationship_role": "project:unresolved",
                "source_spans": [
                    item.model_dump(mode="python") for item in proposal.source_spans
                ],
                "extraction_confidence": proposal.extraction_confidence,
                "reason_codes": proposal.reason_codes,
            }
        )
        known_entities.add(project_ref)
    elif project_knowledge.project_entity is not None:
        raise ValueError("project entity proposal without an atomic project observation")

    observations: list[dict[str, Any]] = []
    ref_maps: dict[str, dict[str, str]] = {
        "entity_graph": {},
        "temporal_content": {},
        "project_knowledge": {},
    }

    def append_observation(pass_name: str, value: dict[str, Any]) -> None:
        local_ref = str(value["observation_ref"])
        if local_ref in ref_maps[pass_name]:
            raise ValueError(f"duplicate observation_ref inside {pass_name}")
        if len(observations) >= 32:
            raise ValueError("specialized packet exceeds the V5 observation budget")
        final_ref = f"o{len(observations) + 1:02d}"
        ref_maps[pass_name][local_ref] = final_ref
        value["observation_ref"] = final_ref
        observations.append(value)

    for item in graph_observations:
        append_observation("entity_graph", item.model_dump(mode="python"))
    for item in content_observations:
        append_observation("temporal_content", item.model_dump(mode="python"))
    for item in project_knowledge.observations:
        value = item.model_dump(mode="python")
        value["subject_entity_ref"] = project_ref
        append_observation("project_knowledge", value)

    comparisons: list[dict[str, Any]] = []
    for hint in temporal_content.comparison_hints:
        value = hint.model_dump(mode="python")
        mapped = ref_maps["temporal_content"].get(value["observation_ref"])
        if mapped is None:
            raise ValueError("comparison hint references a non-content observation")
        value["observation_ref"] = mapped
        comparisons.append(value)

    deferrals = [
        item.model_dump(mode="python")
        for item in (
            entity_graph.deferrals
            + temporal_content.deferrals
            + project_knowledge.deferrals
        )
    ]
    if len(deferrals) > 32:
        raise ValueError("specialized packet exceeds the V5 deferral budget")

    findings = sorted(
        {
            f"entity_graph_{item}" for item in entity_graph.packet_findings
        }
        | {
            f"temporal_content_{item}" for item in temporal_content.packet_findings
        }
        | {
            f"project_knowledge_{item}" for item in project_knowledge.packet_findings
        }
    )
    if len(findings) > 32:
        raise ValueError("specialized packet exceeds the V5 finding budget")

    return ModelPacket.model_validate(
        {
            "entity_mentions": mentions,
            "observations": observations,
            "comparison_hints": comparisons,
            "deferrals": deferrals,
            "packet_findings": findings,
        }
    )
