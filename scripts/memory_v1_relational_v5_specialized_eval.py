#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Callable, Literal, TypeVar

from pydantic import BaseModel

from scripts.memory_v1_consolidation_packet_eval import stable_json
from scripts.memory_v1_relational_v5_live_eval import (
    ComparisonHint,
    Deferral,
    EntityMention,
    ModelPacket,
    Observation,
    SENSITIVITY_RANK,
    SpanOffsets,
    StrictModel,
    Temporal,
    enforce_temporal_authority,
    sha256_text,
    source_span,
    validate_object,
    validate_temporal,
)
from scripts.memory_v1_relational_v5_specialized import (
    ENTITY_GRAPH_INSTRUCTIONS,
    PROJECT_KNOWLEDGE_INSTRUCTIONS,
    TEMPORAL_CONTENT_INSTRUCTIONS,
    EntityGraphPassPacket,
    ProjectKnowledgePassPacket,
    TemporalContentPassPacket,
    assemble_specialized_packet,
    entity_catalog,
    execution_plan,
)


PIPELINE_VERSION = "memory_v1_relational_specialized_v5"
PassName = Literal["entity_graph", "temporal_content", "project_knowledge"]
AttemptName = Literal["initial", "repair"]
PacketT = TypeVar("PacketT", bound=BaseModel)

EXPLICIT_PET_NAME_RE = re.compile(
    r"\bmy\s+(?:(?:first|current|former|late)\s+)?"
    r"(?:cat|dog|pet)[\'’]s\s+name\s+(?:is|was)\s+"
    r"(?P<name>[\w][\w\'’\-]{0,63})\b",
    re.IGNORECASE,
)
CORRECTION_MARKER_RE = re.compile(
    r"\b(?:correction|correct\s+that|voice[\s-]+to[\s-]+text\s+error|"
    r"transcription\s+error|spell(?:ing)?\s+error)\b",
    re.IGNORECASE,
)
UNCERTAINTY_RE = re.compile(
    r"\b(?:i\s+think|i\s+believe|maybe|possibly|probably|not\s+sure)\b",
    re.IGNORECASE,
)
REPEATED_SIBLING_ROLE_RE = re.compile(
    r"^(family:(?:sister|brother|sibling))(?::[1-9][0-9]*)?$"
)


class PassAttemptAudit(StrictModel):
    pass_name: PassName
    attempt: AttemptName
    response_id: str | None
    response_status: str
    incomplete_reason: str | None
    refusal: bool
    selected: bool
    validation_reasons: list[str]
    quality: list[int]
    schema_sha256: str
    model: str
    store: Literal[False]


class SpecializedRunResult(StrictModel):
    mode: Literal["zero_write_specialized_v5"]
    source_token: str
    safety_identifier: str
    skipped_reason: Literal["empty_source"] | None
    attempts: list[PassAttemptAudit]
    packet: ModelPacket


class SpecializedExtractionError(RuntimeError):
    def __init__(self, message: str, attempts: list[PassAttemptAudit]) -> None:
        super().__init__(message)
        self.attempts = list(attempts)


class _ResponseContractError(RuntimeError):
    def __init__(self, message: str, audit: PassAttemptAudit) -> None:
        super().__init__(message)
        self.audit = audit


def _schema_sha256(model_type: type[BaseModel]) -> str:
    return sha256_text(stable_json(model_type.model_json_schema()))


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _refusal_present(response: Any) -> bool:
    for output in _value(response, "output", []) or []:
        if _value(output, "type") != "message":
            continue
        for item in _value(output, "content", []) or []:
            if _value(item, "type") == "refusal":
                return True
    return False


def _incomplete_reason(response: Any) -> str | None:
    details = _value(response, "incomplete_details")
    if details is None:
        return None
    reason = _value(details, "reason")
    return str(reason) if reason else "unspecified"


def _source_input(
    *,
    text: str,
    source_recorded_at: str,
    catalog: list[dict[str, Any]] | None,
) -> str:
    sections = [
        "UNTRUSTED SOURCE RECORD",
        f"source_observed_at={source_recorded_at}",
        "Offsets start at zero in the exact text inside <record>.",
        "<record>",
        text,
        "</record>",
    ]
    if catalog is not None:
        sections.extend(
            [
                "SERVER-VALIDATED SOURCE-LOCAL ENTITY CATALOG",
                stable_json(catalog),
                "Use only these entity_ref values. An empty catalog means no "
                "entity may be referenced.",
            ]
        )
    return "\n".join(sections)


def _repair_instructions(instructions: str, reasons: list[str]) -> str:
    targeted: list[str] = []
    if any(".span[" in item for item in reasons):
        targeted.append(
            "For every span error, recopy the shortest sufficient quote exactly "
            "from inside <record> and recompute Python Unicode start/end offsets. "
            "Do not paraphrase, normalize punctuation, or reuse a composite quote."
        )
    if any("self_entity" in item for item in reasons):
        targeted.append(
            "A self entity must use user:self, self_reference, and a source span "
            "containing I, me, my, mine, or myself. Remove false self entities."
        )
    if "project_scope_deferral_without_observation" in reasons:
        targeted.append(
            "Project scope uncertainty never replaces a directly supported atomic "
            "project observation. Extract the supported project.* observation or "
            "remove the unsupported project-scope deferral."
        )
    return (
        instructions
        + "\n\nSERVER VALIDATION REPAIR REQUEST\n"
        + "Return one complete replacement for this pass, not a patch. Correct "
        + "the deterministic issues below without inventing information or "
        + "dropping unrelated valid output. Expected evaluation labels are not "
        + "available to this request.\n"
        + stable_json(reasons)
        + ("\n" + "\n".join(targeted) if targeted else "")
    )


def _registry_instructions(
    registry: dict[str, Any] | None, pass_name: PassName
) -> str:
    if registry is None:
        return ""
    graph_predicates = {
        "relationship.has_pet",
        "relationship.parent_of",
        "relationship.sibling_of",
        "residence.lives_at",
    }
    project_predicates = {
        "project.constraint",
        "project.current_state",
        "project.proposed_feature",
        "project.requirement",
    }
    if pass_name == "entity_graph":
        allowed = graph_predicates
    elif pass_name == "project_knowledge":
        allowed = project_predicates
    else:
        allowed = {
            str(item["predicate"])
            for item in registry.get("predicates", [])
            if item.get("predicate") not in graph_predicates | project_predicates
        }
    rows = [
        {
            "predicate": item["predicate"],
            "subject_entity_types": item["subject_entity_types"],
            "object_contract": item["object_contract"],
            "temporal_semantics": item["temporal_semantics"],
            "modalities": item["modalities"],
            "projection_classes": item["projection_classes"],
            "sensitivity_floor": item["sensitivity_floor"],
            "surface_policies": item["surface_policies"],
            "description": item["description"],
        }
        for item in registry.get("predicates", [])
        if item.get("predicate") in allowed
    ]
    contracts = registry.get("object_contracts", {})
    required_contracts = {str(item["object_contract"]) for item in rows}
    missing = sorted(required_contracts - set(contracts))
    if missing:
        raise RuntimeError(
            f"registry contracts missing for {pass_name}:{','.join(missing)}"
        )
    governed = {
        "registry_version": registry.get("registry_version"),
        "pass": pass_name,
        "predicates": rows,
        "object_contracts": {
            key: contracts[key] for key in sorted(required_contracts)
        },
    }
    return "GOVERNED PREDICATE REGISTRY FOR THIS PASS\n" + stable_json(governed)


def _pass_instructions(
    base: str, registry: dict[str, Any] | None, pass_name: PassName
) -> str:
    governed = _registry_instructions(registry, pass_name)
    return base if not governed else base + "\n\n" + governed


def _span_reasons(prefix: str, spans: list[Any], text: str) -> list[str]:
    reasons: list[str] = []
    for index, span in enumerate(spans):
        value = span.model_dump(mode="json") if hasattr(span, "model_dump") else span
        try:
            source_span(value, text)
        except Exception as exc:
            reasons.append(f"{prefix}.span[{index}]:{exc}")
    return reasons


def _duplicate_reasons(values: list[str], label: str) -> list[str]:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    return [f"duplicate_{label}:{value}" for value in duplicates]


def _append_server_finding(packet: Any, finding: str) -> None:
    values = [value for value in packet.packet_findings if value != finding]
    packet.packet_findings = values[:15] + [finding]


def normalize_redundant_source_spans(packet: PacketT, text: str) -> PacketT:
    """Prune only invalid spans that are redundant with valid spans on one item."""
    normalized = packet.model_copy(deep=True)
    items: list[Any] = []
    if isinstance(normalized, EntityGraphPassPacket):
        items.extend(normalized.entity_mentions)
        items.extend(normalized.relationship_observations)
    else:
        items.extend(normalized.observations)
    items.extend(normalized.deferrals)
    pruned = 0
    for item in items:
        valid: list[Any] = []
        invalid: list[Any] = []
        allow_repeated = getattr(item, "entity_type", None) == "self"
        for span in item.source_spans:
            value = span.model_dump(mode="json")
            try:
                source_span(value, text, allow_repeated=allow_repeated)
                valid.append(span)
            except Exception:
                invalid.append(span)
        if valid and invalid:
            item.source_spans = valid
            pruned += len(invalid)
    if pruned:
        _append_server_finding(normalized, "redundant_invalid_source_spans_pruned")
    return normalized


def normalize_repeated_sibling_roles(
    packet: EntityGraphPassPacket, text: str
) -> EntityGraphPassPacket:
    self_entity_refs = {
        entity.entity_ref
        for entity in packet.entity_mentions
        if entity.entity_type == "self"
        and entity.mention_kind == "self_reference"
        and entity.relationship_role == "user:self"
    }
    linked_siblings = {
        item.object.entity_ref
        for item in packet.relationship_observations
        if item.predicate == "relationship.sibling_of"
        and item.subject_entity_ref in self_entity_refs
        and item.object.kind == "entity"
    }
    groups: dict[str, list[Any]] = {}
    for entity in packet.entity_mentions:
        role = str(entity.relationship_role or "")
        match = REPEATED_SIBLING_ROLE_RE.fullmatch(role)
        if (
            match is None
            or entity.entity_type != "person"
            or entity.entity_ref not in linked_siblings
        ):
            continue
        groups.setdefault(match.group(1), []).append(entity)

    changed = False
    for base_role, entities in groups.items():
        if len(entities) < 2:
            continue
        positioned: list[tuple[int, Any]] = []
        for entity in entities:
            starts: list[int] = []
            for raw_span in entity.source_spans:
                try:
                    trusted = source_span(raw_span.model_dump(mode="json"), text)
                except Exception:
                    continue
                starts.append(int(trusted["start"]))
            if not starts:
                positioned = []
                break
            positioned.append((min(starts), entity))
        if len(positioned) != len(entities):
            continue
        source_positions = [start for start, _ in positioned]
        if len(source_positions) != len(set(source_positions)):
            continue
        for ordinal, (_, entity) in enumerate(sorted(positioned), 1):
            normalized_role = f"{base_role}:{ordinal}"
            if entity.relationship_role != normalized_role:
                entity.relationship_role = normalized_role
                changed = True
    if changed:
        _append_server_finding(packet, "repeated_sibling_roles_source_ordered")
    return packet


def normalize_explicit_pet_name_correction_graph(
    packet: EntityGraphPassPacket, text: str
) -> dict[str, Any] | None:
    match = EXPLICIT_PET_NAME_RE.search(text)
    if match is None or CORRECTION_MARKER_RE.search(text) is None:
        return None
    name = match.group("name")
    entity = next(
        (
            item
            for item in packet.entity_mentions
            if item.entity_type == "animal"
            and (
                item.relationship_role == "pet:corrected_name_subject"
                or str(item.name_text or "").casefold() == name.casefold()
            )
        ),
        None,
    )
    span = SpanOffsets(start=match.start(), end=match.end(), quote=match.group(0))
    if entity is None:
        if len(packet.entity_mentions) >= 24:
            return None
        existing = {item.entity_ref for item in packet.entity_mentions}
        entity_ref = next(
            (f"e{ordinal:02d}" for ordinal in range(1, 100) if f"e{ordinal:02d}" not in existing),
            None,
        )
        if entity_ref is None:
            return None
        entity = EntityMention(
            entity_ref=entity_ref,
            entity_type="animal",
            mention_kind="named",
            name_text=name,
            relationship_role="pet:corrected_name_subject",
            source_spans=[span],
            extraction_confidence=0.99,
            reason_codes=["server_detected_explicit_pet_name_correction"],
        )
        packet.entity_mentions.append(entity)
    else:
        entity.relationship_role = "pet:corrected_name_subject"
    _append_server_finding(packet, "explicit_pet_name_correction_subject_normalized")
    return {
        "entity_ref": entity.entity_ref,
        "name": name,
        "span": span,
    }


def normalize_explicit_pet_name_correction_content(
    packet: TemporalContentPassPacket,
    correction: dict[str, Any] | None,
) -> TemporalContentPassPacket:
    if correction is None:
        return packet
    entity_ref = str(correction["entity_ref"])
    name = str(correction["name"])
    existing = next(
        (
            item
            for item in packet.observations
            if item.predicate == "identity.name_canonical"
            and item.subject_entity_ref == entity_ref
            and item.object.kind == "literal"
            and str(item.object.value).casefold() == name.casefold()
        ),
        None,
    )
    if existing is None and len(packet.observations) < 32:
        used = {item.observation_ref for item in packet.observations}
        observation_ref = next(
            (f"o{ordinal:02d}" for ordinal in range(1, 100) if f"o{ordinal:02d}" not in used),
            None,
        )
        if observation_ref is not None:
            existing = Observation.model_validate(
                {
                    "observation_ref": observation_ref,
                    "subject_entity_ref": entity_ref,
                    "predicate": "identity.name_canonical",
                    "object": {
                        "kind": "literal",
                        "datatype": "text",
                        "value": name,
                        "unit": None,
                        "approximate": False,
                    },
                    "polarity": "affirmed",
                    "modality": "corrective",
                    "projection_class": "correction",
                    "surface_policy": "normalization_only",
                    "temporal": {
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
                        "reason_codes": ["explicit_correction_observed"],
                    },
                    "sensitivity": "medium",
                    "extraction_confidence": 0.99,
                    "source_spans": [correction["span"]],
                    "reason_codes": ["server_detected_explicit_pet_name_correction"],
                }
            )
            packet.observations.append(existing)
    if existing is None:
        return packet
    packet.deferrals = [
        item
        for item in packet.deferrals
        if not (
            item.memory_shape == "correction"
            and item.reason_code in {"question_only", "context_missing"}
        )
    ]
    relations = {
        item.relation_type
        for item in packet.comparison_hints
        if item.observation_ref == existing.observation_ref
    }
    for relation in ("corrects", "supersedes"):
        if relation not in relations and len(packet.comparison_hints) < 32:
            packet.comparison_hints.append(
                ComparisonHint(
                    observation_ref=existing.observation_ref,
                    relation_type=relation,
                    target_lookup_key="owner_scoped_prior_name_claim",
                    reason_codes=["owner_scoped_target_resolution_required"],
                )
            )
    _append_server_finding(packet, "explicit_pet_name_correction_normalized")
    return packet


def normalize_uncertain_credential_deferral(
    packet: TemporalContentPassPacket,
    graph: EntityGraphPassPacket,
    text: str,
) -> TemporalContentPassPacket:
    if not any(item.predicate == "occupation.works_as" for item in packet.observations):
        return packet
    if any(item.reason_code == "ambiguous_transcription" for item in packet.deferrals):
        return packet
    for entity in graph.entity_mentions:
        if entity.entity_type != "organization":
            continue
        for raw_span in entity.source_spans:
            try:
                trusted = source_span(raw_span.model_dump(mode="json"), text)
            except Exception:
                continue
            start = int(trusted["start"])
            window_start = max(0, start - 64)
            matches = list(UNCERTAINTY_RE.finditer(text[window_start:start]))
            if not matches:
                continue
            match = matches[-1]
            cue_start = window_start + match.start()
            cue_end = window_start + match.end()
            gap = text[cue_end:start]
            if len(gap) > 48 or re.search(r"[.!?]", gap):
                continue
            end = int(trusted["end"])
            packet.deferrals.append(
                Deferral(
                    reason_code="ambiguous_transcription",
                    memory_shape="direct_claim",
                    source_spans=[
                        SpanOffsets(start=cue_start, end=end, quote=text[cue_start:end])
                    ],
                    sensitivity="medium",
                )
            )
            _append_server_finding(
                packet, "uncertain_credential_deferral_server_added"
            )
            return packet
    return packet


def normalize_project_current_state_temporal(
    packet: ProjectKnowledgePassPacket, source_recorded_at: str
) -> ProjectKnowledgePassPacket:
    changed = False
    for item in packet.observations:
        if item.predicate != "project.current_state":
            continue
        current = item.temporal
        server_anchorable = current.semantic == "state_validity" and (
            current.source_form == "implicit_source_time"
            or (
                current.source_form == "none"
                and (
                    current.shape == "none"
                    or (
                        current.shape == "open_interval"
                        and current.basis == "none"
                        and current.instant_range is None
                    )
                )
            )
        )
        if not server_anchorable:
            continue
        temporal = {
            "semantic": "state_validity",
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
            "reason_codes": ["server_anchored_project_current_state"],
        }
        enforce_temporal_authority(temporal, source_recorded_at)
        temporal["instant_range"]["lower"] = datetime.fromisoformat(
            str(temporal["instant_range"]["lower"]).replace("Z", "+00:00")
        )
        normalized = Temporal.model_validate(temporal)
        if item.temporal != normalized:
            item.temporal = normalized
            changed = True
    if changed:
        _append_server_finding(
            packet, "project_current_state_temporal_server_anchored"
        )
    return packet


def _registry_observation_reasons(
    observations: list[Any],
    *,
    catalog: list[dict[str, Any]],
    registry: dict[str, Any] | None,
    source_recorded_at: str | None,
    project_subject: bool = False,
) -> list[str]:
    if registry is None:
        return []
    if not source_recorded_at:
        return ["registry_validation_missing_source_recorded_at"]
    rules = {str(item["predicate"]): item for item in registry.get("predicates", [])}
    entities = {str(item["entity_ref"]): item for item in catalog}
    reasons: list[str] = []
    for item in observations:
        ref = str(item.observation_ref)
        rule = rules.get(str(item.predicate))
        if rule is None:
            reasons.append(f"registry:{ref}:unregistered_predicate:{item.predicate}")
            continue
        subject_type = "project" if project_subject else None
        if not project_subject:
            subject = entities.get(str(item.subject_entity_ref))
            subject_type = str(subject["entity_type"]) if subject is not None else None
        if subject_type not in rule["subject_entity_types"]:
            reasons.append(f"registry:{ref}:subject_entity_type")
        if item.modality not in rule["modalities"]:
            reasons.append(f"registry:{ref}:modality")
        if item.projection_class not in rule["projection_classes"]:
            reasons.append(f"registry:{ref}:projection_class")
        if item.surface_policy not in rule["surface_policies"]:
            reasons.append(f"registry:{ref}:surface_policy")
        temporal = item.temporal.model_dump(mode="json")
        if temporal["semantic"] not in rule["temporal_semantics"]:
            reasons.append(f"registry:{ref}:temporal_semantic")
        if SENSITIVITY_RANK[item.sensitivity] < SENSITIVITY_RANK[rule["sensitivity_floor"]]:
            reasons.append(f"registry:{ref}:sensitivity_floor")
        contract_key = str(rule["object_contract"])
        contract = registry.get("object_contracts", {}).get(contract_key)
        if contract is None:
            reasons.append(f"registry:{ref}:missing_object_contract:{contract_key}")
        else:
            obj = item.object.model_dump(mode="json")
            for error in validate_object(obj, contract, entities):
                reasons.append(f"registry:{ref}:object:{error}")
        try:
            enforce_temporal_authority(temporal, source_recorded_at)
            for error in validate_temporal(temporal):
                reasons.append(f"registry:{ref}:temporal:{error}")
        except Exception as exc:
            reasons.append(f"registry:{ref}:temporal:{exc}")
    return sorted(set(reasons))


def validate_entity_graph_pass(
    packet: EntityGraphPassPacket,
    text: str,
    *,
    registry: dict[str, Any] | None = None,
    source_recorded_at: str | None = None,
) -> list[str]:
    reasons: list[str] = []
    refs = [item.entity_ref for item in packet.entity_mentions]
    known = set(refs)
    reasons.extend(_duplicate_reasons(refs, "entity_ref"))
    for item in packet.entity_mentions:
        if item.entity_type == "project":
            reasons.append(f"project_entity_in_graph:{item.entity_ref}")
        if item.entity_type == "self":
            if item.relationship_role != "user:self":
                reasons.append(f"self_entity_role:{item.entity_ref}:user:self_required")
            if item.mention_kind != "self_reference":
                reasons.append(
                    f"self_entity_mention_kind:{item.entity_ref}:self_reference_required"
                )
            quotes = " ".join(span.quote for span in item.source_spans)
            if not re.search(r"(?<![\w])(?:i|me|my|mine|myself)(?![\w])", quotes, re.I):
                reasons.append(
                    f"self_entity_grounding:{item.entity_ref}:first_person_span_required"
                )
        reasons.extend(_span_reasons(f"entity:{item.entity_ref}", item.source_spans, text))
    observation_refs = [item.observation_ref for item in packet.relationship_observations]
    reasons.extend(_duplicate_reasons(observation_refs, "observation_ref"))
    for item in packet.relationship_observations:
        if item.predicate not in {
            "relationship.has_pet",
            "relationship.parent_of",
            "relationship.sibling_of",
            "residence.lives_at",
        }:
            reasons.append(f"non_graph_predicate:{item.observation_ref}:{item.predicate}")
        unresolved = {item.subject_entity_ref} - known
        if item.object.kind == "entity":
            unresolved.add(item.object.entity_ref)
            unresolved -= known
        if unresolved:
            reasons.append(
                f"unresolved_entity_ref:{item.observation_ref}:{','.join(sorted(unresolved))}"
            )
        reasons.extend(
            _span_reasons(f"observation:{item.observation_ref}", item.source_spans, text)
        )
    for index, item in enumerate(packet.deferrals):
        reasons.extend(_span_reasons(f"deferral:{index}", item.source_spans, text))
    reasons.extend(
        _registry_observation_reasons(
            list(packet.relationship_observations),
            catalog=entity_catalog(packet),
            registry=registry,
            source_recorded_at=source_recorded_at,
        )
    )
    return sorted(set(reasons))


def validate_temporal_content_pass(
    packet: TemporalContentPassPacket,
    text: str,
    catalog: list[dict[str, Any]],
    *,
    registry: dict[str, Any] | None = None,
    source_recorded_at: str | None = None,
) -> list[str]:
    reasons: list[str] = []
    known = {str(item["entity_ref"]) for item in catalog}
    observation_refs = [item.observation_ref for item in packet.observations]
    reasons.extend(_duplicate_reasons(observation_refs, "observation_ref"))
    for item in packet.observations:
        if item.predicate.startswith("relationship.") or item.predicate == "residence.lives_at":
            reasons.append(f"graph_predicate_in_content:{item.observation_ref}:{item.predicate}")
        if item.predicate.startswith("project."):
            reasons.append(f"project_predicate_in_content:{item.observation_ref}:{item.predicate}")
        unresolved = {item.subject_entity_ref} - known
        if item.object.kind == "entity":
            unresolved.add(item.object.entity_ref)
            unresolved -= known
        if unresolved:
            reasons.append(
                f"unresolved_entity_ref:{item.observation_ref}:{','.join(sorted(unresolved))}"
            )
        reasons.extend(
            _span_reasons(f"observation:{item.observation_ref}", item.source_spans, text)
        )
    valid_observations = set(observation_refs)
    for index, hint in enumerate(packet.comparison_hints):
        if hint.observation_ref not in valid_observations:
            reasons.append(f"comparison_ref_unresolved:{index}:{hint.observation_ref}")
    for index, item in enumerate(packet.deferrals):
        reasons.extend(_span_reasons(f"deferral:{index}", item.source_spans, text))
    reasons.extend(
        _registry_observation_reasons(
            list(packet.observations),
            catalog=catalog,
            registry=registry,
            source_recorded_at=source_recorded_at,
        )
    )
    return sorted(set(reasons))


def validate_project_knowledge_pass(
    packet: ProjectKnowledgePassPacket,
    text: str,
    *,
    registry: dict[str, Any] | None = None,
    source_recorded_at: str | None = None,
) -> list[str]:
    reasons: list[str] = []
    refs = [item.observation_ref for item in packet.observations]
    reasons.extend(_duplicate_reasons(refs, "observation_ref"))
    for item in packet.observations:
        reasons.extend(
            _span_reasons(f"observation:{item.observation_ref}", item.source_spans, text)
        )
    for index, item in enumerate(packet.deferrals):
        reasons.extend(_span_reasons(f"deferral:{index}", item.source_spans, text))
        if item.reason_code != "project_scope_unresolved":
            reasons.append(
                f"non_project_deferral_in_project_pass:{index}:{item.reason_code}"
            )
    if any(
        item.reason_code == "project_scope_unresolved"
        and item.memory_shape == "project_knowledge"
        for item in packet.deferrals
    ) and not packet.observations:
        reasons.append("project_scope_deferral_without_observation")
    reasons.extend(
        _registry_observation_reasons(
            list(packet.observations),
            catalog=[],
            registry=registry,
            source_recorded_at=source_recorded_at,
            project_subject=True,
        )
    )
    return sorted(set(reasons))


async def _call_pass(
    client: Any,
    *,
    model: str,
    pass_name: PassName,
    attempt: AttemptName,
    instructions: str,
    text: str,
    source_recorded_at: str,
    source_token: str,
    safety_identifier: str,
    packet_type: type[PacketT],
    catalog: list[dict[str, Any]] | None,
) -> tuple[PacketT, PassAttemptAudit]:
    schema_sha256 = _schema_sha256(packet_type)
    try:
        response = await asyncio.to_thread(
            client.responses.parse,
            model=model,
            instructions=instructions,
            input=_source_input(
                text=text,
                source_recorded_at=source_recorded_at,
                catalog=catalog,
            ),
            text_format=packet_type,
            store=False,
            safety_identifier=safety_identifier,
            metadata={
                "pipeline": PIPELINE_VERSION,
                "pass": pass_name,
                "attempt": attempt,
                "source_token": source_token,
            },
            max_output_tokens=8000,
        )
    except Exception as exc:
        audit = PassAttemptAudit(
            pass_name=pass_name,
            attempt=attempt,
            response_id=None,
            response_status="request_error",
            incomplete_reason=None,
            refusal=False,
            selected=False,
            validation_reasons=[f"request_error:{type(exc).__name__}"],
            quality=[1],
            schema_sha256=schema_sha256,
            model=model,
            store=False,
        )
        raise _ResponseContractError(
            f"{pass_name} Responses API request failed:{type(exc).__name__}", audit
        ) from exc
    response_id = str(_value(response, "id")) if _value(response, "id") else None
    status = str(_value(response, "status", "unknown"))
    incomplete_reason = _incomplete_reason(response)
    refusal = _refusal_present(response)
    audit = PassAttemptAudit(
        pass_name=pass_name,
        attempt=attempt,
        response_id=response_id,
        response_status=status,
        incomplete_reason=incomplete_reason,
        refusal=refusal,
        selected=False,
        validation_reasons=[],
        quality=[],
        schema_sha256=schema_sha256,
        model=model,
        store=False,
    )
    if refusal:
        raise _ResponseContractError(f"{pass_name} response was refused", audit)
    if status != "completed":
        raise _ResponseContractError(
            f"{pass_name} response status was {status}:{incomplete_reason or 'unspecified'}",
            audit,
        )
    parsed = _value(response, "output_parsed")
    if parsed is None:
        raise _ResponseContractError(f"{pass_name} response had no parsed output", audit)
    try:
        if not isinstance(parsed, packet_type):
            parsed = packet_type.model_validate(parsed)
    except Exception as exc:
        audit.validation_reasons = [f"parsed_schema_error:{type(exc).__name__}"]
        audit.quality = [1]
        raise _ResponseContractError(
            f"{pass_name} parsed output failed schema validation", audit
        ) from exc
    return parsed, audit


async def _run_pass(
    client: Any,
    *,
    model: str,
    pass_name: PassName,
    instructions: str,
    text: str,
    source_recorded_at: str,
    source_token: str,
    safety_identifier: str,
    packet_type: type[PacketT],
    catalog: list[dict[str, Any]] | None,
    validator: Callable[[PacketT], list[str]],
    normalizer: Callable[[PacketT], PacketT],
    attempts: list[PassAttemptAudit],
) -> PacketT:
    try:
        initial, initial_audit = await _call_pass(
            client,
            model=model,
            pass_name=pass_name,
            attempt="initial",
            instructions=instructions,
            text=text,
            source_recorded_at=source_recorded_at,
            source_token=source_token,
            safety_identifier=safety_identifier,
            packet_type=packet_type,
            catalog=catalog,
        )
    except _ResponseContractError as exc:
        attempts.append(exc.audit)
        raise SpecializedExtractionError(str(exc), attempts) from exc
    initial = normalizer(initial)
    initial_reasons = validator(initial)
    initial_audit.validation_reasons = initial_reasons
    initial_audit.quality = [len(initial_reasons)]
    initial_audit.selected = True
    attempts.append(initial_audit)
    if not initial_reasons:
        return initial

    try:
        repaired, repaired_audit = await _call_pass(
            client,
            model=model,
            pass_name=pass_name,
            attempt="repair",
            instructions=_repair_instructions(instructions, initial_reasons),
            text=text,
            source_recorded_at=source_recorded_at,
            source_token=source_token,
            safety_identifier=safety_identifier,
            packet_type=packet_type,
            catalog=catalog,
        )
    except _ResponseContractError as exc:
        attempts.append(exc.audit)
        raise SpecializedExtractionError(str(exc), attempts) from exc
    repaired = normalizer(repaired)
    repaired_reasons = validator(repaired)
    repaired_audit.validation_reasons = repaired_reasons
    repaired_audit.quality = [len(repaired_reasons)]
    use_repair = len(repaired_reasons) < len(initial_reasons)
    repaired_audit.selected = use_repair
    initial_audit.selected = not use_repair
    attempts.append(repaired_audit)
    selected = repaired if use_repair else initial
    selected_reasons = repaired_reasons if use_repair else initial_reasons
    if selected_reasons:
        raise SpecializedExtractionError(
            f"{pass_name} remained invalid after one bounded repair", attempts
        )
    return selected


def _empty_result(
    *, source_token: str, safety_identifier: str
) -> SpecializedRunResult:
    return SpecializedRunResult(
        mode="zero_write_specialized_v5",
        source_token=source_token,
        safety_identifier=safety_identifier,
        skipped_reason="empty_source",
        attempts=[],
        packet=ModelPacket(
            entity_mentions=[],
            observations=[],
            comparison_hints=[],
            deferrals=[],
            packet_findings=[],
        ),
    )


async def run_specialized_zero_write(
    client: Any,
    *,
    model: str,
    owner_user_id: str,
    source_external_id: str,
    source_recorded_at: str,
    text: str,
    registry: dict[str, Any] | None = None,
) -> SpecializedRunResult:
    plan = execution_plan()
    if plan.max_calls_per_pass != 2 or plan.model_may_write:
        raise RuntimeError("specialized execution plan is not bounded zero-write")
    source_token = sha256_text(source_external_id)[:32]
    safety_identifier = sha256_text(owner_user_id)
    if not text.strip():
        return _empty_result(
            source_token=source_token,
            safety_identifier=safety_identifier,
        )

    attempts: list[PassAttemptAudit] = []
    graph = await _run_pass(
        client,
        model=model,
        pass_name="entity_graph",
        instructions=_pass_instructions(
            ENTITY_GRAPH_INSTRUCTIONS, registry, "entity_graph"
        ),
        text=text,
        source_recorded_at=source_recorded_at,
        source_token=source_token,
        safety_identifier=safety_identifier,
        packet_type=EntityGraphPassPacket,
        catalog=None,
        validator=lambda packet: validate_entity_graph_pass(
            packet,
            text,
            registry=registry,
            source_recorded_at=source_recorded_at,
        ),
        normalizer=lambda packet: normalize_redundant_source_spans(packet, text),
        attempts=attempts,
    )
    graph = normalize_repeated_sibling_roles(graph, text)
    correction = normalize_explicit_pet_name_correction_graph(graph, text)
    catalog = entity_catalog(graph)

    def normalize_content(packet: TemporalContentPassPacket) -> TemporalContentPassPacket:
        packet = normalize_redundant_source_spans(packet, text)
        packet = normalize_explicit_pet_name_correction_content(packet, correction)
        return normalize_uncertain_credential_deferral(packet, graph, text)

    content = await _run_pass(
        client,
        model=model,
        pass_name="temporal_content",
        instructions=_pass_instructions(
            TEMPORAL_CONTENT_INSTRUCTIONS, registry, "temporal_content"
        ),
        text=text,
        source_recorded_at=source_recorded_at,
        source_token=source_token,
        safety_identifier=safety_identifier,
        packet_type=TemporalContentPassPacket,
        catalog=catalog,
        validator=lambda packet: validate_temporal_content_pass(
            packet,
            text,
            catalog,
            registry=registry,
            source_recorded_at=source_recorded_at,
        ),
        normalizer=normalize_content,
        attempts=attempts,
    )

    def normalize_project(packet: ProjectKnowledgePassPacket) -> ProjectKnowledgePassPacket:
        packet = normalize_redundant_source_spans(packet, text)
        return normalize_project_current_state_temporal(packet, source_recorded_at)

    project = await _run_pass(
        client,
        model=model,
        pass_name="project_knowledge",
        instructions=_pass_instructions(
            PROJECT_KNOWLEDGE_INSTRUCTIONS, registry, "project_knowledge"
        ),
        text=text,
        source_recorded_at=source_recorded_at,
        source_token=source_token,
        safety_identifier=safety_identifier,
        packet_type=ProjectKnowledgePassPacket,
        catalog=None,
        validator=lambda packet: validate_project_knowledge_pass(
            packet,
            text,
            registry=registry,
            source_recorded_at=source_recorded_at,
        ),
        normalizer=normalize_project,
        attempts=attempts,
    )
    try:
        packet = assemble_specialized_packet(graph, content, project)
    except Exception as exc:
        raise SpecializedExtractionError(
            f"specialized packet assembly failed:{type(exc).__name__}:{exc}", attempts
        ) from exc
    return SpecializedRunResult(
        mode="zero_write_specialized_v5",
        source_token=source_token,
        safety_identifier=safety_identifier,
        skipped_reason=None,
        attempts=attempts,
        packet=packet,
    )
