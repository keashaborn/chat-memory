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
observations. Project entities are server-owned and must not originate in this
pass. Never extract project knowledge, attributes, health content, preferences,
or corrections.
Scan declarative clauses even when they are embedded in a long question.
Declare every entity needed as the subject or entity-object of a later content
pass even when no graph relationship is present. In particular, declare
user:self for first-person occupation/profile facts and a concept entity only
for an explicitly stated employment or current professional role. Training,
education, qualification, or credential language alone uses a literal
credential.reported observation and does not create an occupation concept.
A self entity must be
grounded in I, me, my, mine, or myself, use mention_kind=self_reference, and use
relationship_role=user:self; its name_text must be null because a pronoun is not
a name. We, you, they, and they're are not self mentions.
Declare an explicitly corrected pet-name subject as an animal with role
pet:corrected_name_subject.
For repeated same-type siblings, use family:sister:N, family:brother:N, or
family:sibling:N in source order. The server revalidates that ordering from
exact source spans.
When a plural clause such as "they all live in the area" directly follows one
repeated sibling group, declare the explicit place and emit one
residence.lives_at edge for each sibling. Do not guess a plural antecedent when
more than one group could apply.
Every referenced entity must be declared in this packet. Use parent -> self,
self -> sibling, and person/animal -> place directions exactly. Do not infer
names, places, relationships, dates, owner identity, or durable IDs.
For animals directly identified as the user's current or former pets, emit
user:self -> animal relationship.has_pet edges. Use pet:current:N in source
order for current pets and pet:deceased for a pet explicitly reported dead.
Every source span must copy a short exact substring from the record; never
paraphrase, normalize punctuation, or manufacture a long composite quote.
Use one sufficient exact span instead of adding redundant paraphrased spans.
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
prior content. Questions such as "are you saying/thinking..." and "what do you
know/remember about my..." depend on unseen conversational or memory context and
require context_missing in addition to question_only. Add transient_state for a
question that only says a temporary decision/state has not been considered yet.
Add ambiguous_transcription for a suspicious proper noun, credential, API, or
voice transcription without discarding a separate well-supported fact.
Known technical product names are not ambiguous merely because they are
capitalized; defer them only when the source itself supplies uncertainty or a
genuinely malformed product/API phrase.
An uncertain qualifier immediately governing a credential or organization,
such as "I think through [organization]", requires ambiguous_transcription.
Use occupation.works_as only when the source explicitly states employment,
work, practice, or a current professional role. Training, education,
qualification, certification, or becoming qualified is not employment. Use
credential.reported only for an explicit credential report. A statement that
the user does not do a role for a living must never become an affirmed
occupation. If the exact semantics are ambiguous or the registry lacks the
exact predicate, add predicate_semantics_unresolved; never substitute the
nearest predicate. The server independently checks the complete source clause
and removes contradictory or lossy observations. An explicit pet-name correction
is identity.name_canonical with corrective modality and correction projection;
add both corrects and supersedes comparison hints against an owner-scoped prior
name lookup. A self-contained command such as "can you correct that?" does not
become question_only or context_missing when the corrected pet and canonical
name are directly stated in the same record. Copy short exact source substrings
for every span.
An explicitly planned veterinary procedure such as spaying or neutering is a
planned health.user_reported_observation with planned_time, not project scope or
a completed event. Preserve negation: "not deaf" must not become an affirmed
deaf observation.
If the source has no compatible content, return empty observations and hints.
""".strip()


PROJECT_KNOWLEDGE_INSTRUCTIONS = """
Extract only project knowledge: current state, requirements, proposed features,
and constraints. Do not create, name, or bind a project entity; the server will
create one anonymous project:unresolved entity from the first accepted project
observation. A question can coexist with directly stated current project state.
Do not emit personal facts, preferences, health content, owner identity,
trusted project binding, durable IDs, approval, or salience. Project scope
remains unresolved for deterministic server review.
Classify a directly endorsed capability the app should have as
project.requirement, a speculative possibility as project.proposed_feature, and
a directly described implemented/present state as project.current_state. One
source may support more than one of these atomic observations. Project text
literals always use approximate=false; uncertainty belongs in modality.
"I am creating a website" is current state. "I am thinking about turning that
website into an app" is a separate proposed feature. A statement that an app is
mainly for a particular function or that a new system is partially complete is
current state, even when the same turn asks for advice. Generic background such
as "we have systems running" and questions about third-party APIs do not become
project state unless an explicit product, site, app, memory system, or project
referent is directly tied to the stated condition.
Use state_validity for a project.current_state that is stated as true now. The
server will anchor its open validity interval to the source observation time so
later evidence can close or supersede it without erasing the original evidence.
Return that model temporal value with semantic=state_validity and shape/basis/
source_form=none; the server, not the model, constructs the trusted interval.
Questions about external platforms, policies, or coding ability do not alone
create a project entity, observation, or project-scope deferral. If the same
record directly states what the user's app currently is or does, extract that
state while the temporal pass owns question_only. Copy short exact source
substrings for every span. A project_scope_unresolved deferral cannot substitute
for an atomic project observation.
This pass may emit only project_scope_unresolved deferrals. The temporal-content
pass exclusively owns question_only, context_missing, ambiguous_transcription,
transient_state, and every other non-project deferral.
If the source has no project knowledge, return no observations.
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
        if len(mentions) >= 24:
            raise ValueError("specialized packet exceeds the V5 entity budget")
        project_ref = _next_entity_ref(known_entities)
        first_observation = project_knowledge.observations[0]
        mentions.append(
            {
                "entity_ref": project_ref,
                "entity_type": "project",
                "mention_kind": "anonymous",
                "name_text": None,
                "relationship_role": "project:unresolved",
                "source_spans": [
                    item.model_dump(mode="python")
                    for item in first_observation.source_spans
                ],
                "extraction_confidence": first_observation.extraction_confidence,
                "reason_codes": ["server_assigned_unresolved_project_entity"],
            }
        )
        known_entities.add(project_ref)

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

    referenced_entities: set[str] = set()
    for item in observations:
        referenced_entities.add(str(item["subject_entity_ref"]))
        if item["object"]["kind"] == "entity":
            referenced_entities.add(str(item["object"]["entity_ref"]))
    retained_mentions = [
        item for item in mentions if item["entity_ref"] in referenced_entities
    ]
    if len(retained_mentions) != len(mentions):
        mentions = retained_mentions
        findings = sorted(set(findings + ["orphan_entity_mentions_pruned"]))

    return ModelPacket.model_validate(
        {
            "entity_mentions": mentions,
            "observations": observations,
            "comparison_hints": comparisons,
            "deferrals": deferrals,
            "packet_findings": findings,
        }
    )
