#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Mapping

from memory_v1_projection_v5_2_contract import semantic_key_sha256, sha256


REGISTRY_VERSION = "memory_predicate_registry_v5_2"
CONTRACT_VERSION = "memory_v1_projection_plan_v5"
POLICY_VERSION = "memory_projection_policy_v5"
PROJECTOR = "memory_v1_deterministic_projection_v5_2"
PROJECTOR_VERSION = "semantic_dispatch_v2"
RECONCILIATION_PROJECTOR_VERSION = "stance_reconciliation_v1"
CONTROL_CHARS = {chr(value) for value in range(32)} | {chr(127)}
KEY_RE = re.compile(r"[^a-z0-9]+")


class ProjectionDispatchError(RuntimeError):
    pass


RELATION_PHRASES = {
    "relationship.acquaintance_of": "an acquaintance of",
    "relationship.aunt_or_uncle_of": "an aunt or uncle of",
    "relationship.business_partner_of": "a business partner of",
    "relationship.caregiver_for": "a caregiver for",
    "relationship.coach_of": "a coach of",
    "relationship.collaborator_with": "a collaborator with",
    "relationship.cousin_of": "a cousin of",
    "relationship.coworker_of": "a coworker of",
    "relationship.friend_of": "a friend of",
    "relationship.grandparent_of": "a grandparent of",
    "relationship.guardian_of": "a guardian of",
    "relationship.healthcare_provider_for": "a healthcare provider for",
    "relationship.in_law_of": "an in-law of",
    "relationship.manager_of": "a manager of",
    "relationship.mentor_of": "a mentor of",
    "relationship.neighbor_of": "a neighbor of",
    "relationship.parent_of": "a parent of",
    "relationship.plan_helper_for": "a plan helper for",
    "relationship.relative_of": "a relative of",
    "relationship.romantic_partner_of": "a romantic partner of",
    "relationship.roommate_of": "a roommate of",
    "relationship.sibling_of": "a sibling of",
    "relationship.spouse_of": "a spouse of",
    "relationship.teacher_of": "a teacher of",
    "relationship.teammate_of": "a teammate of",
    "relationship.training_partner_of": "a training partner of",
}

SOCIAL_VERBS = {
    "social.avoids": "avoids",
    "social.competes_with": "competes with",
    "social.depends_on": "depends on",
    "social.distrusts": "distrusts",
    "social.estranged_from": "is estranged from",
    "social.experiences_tension_with": "experiences tension with",
    "social.feels_close_to": "feels close to",
    "social.feels_unsafe_with": "feels unsafe with",
    "social.in_conflict_with": "is in conflict with",
    "social.no_contact_with": "has no contact with",
    "social.perceives_as_adversary": "perceives as an adversary",
    "social.supports": "supports",
    "social.trusts": "trusts",
}

SOCIAL_NEGATIVE_VERBS = {
    "social.avoids": "does not avoid",
    "social.competes_with": "does not compete with",
    "social.depends_on": "does not depend on",
    "social.distrusts": "does not distrust",
    "social.estranged_from": "is not estranged from",
    "social.experiences_tension_with": "does not experience tension with",
    "social.feels_close_to": "does not feel close to",
    "social.feels_unsafe_with": "does not feel unsafe with",
    "social.in_conflict_with": "is not in conflict with",
    "social.no_contact_with": "is not out of contact with",
    "social.perceives_as_adversary": "does not perceive as an adversary",
    "social.supports": "does not support",
    "social.trusts": "does not trust",
}

PROJECT_KIND = {
    "project.constraint": "constraint",
    "project.current_state": "current_state",
    "project.proposed_feature": "proposed_feature",
    "project.requirement": "requirement",
}


def _safe_text(value: Any, label: str, *, maximum: int = 2000) -> str:
    if not isinstance(value, str):
        raise ProjectionDispatchError(f"{label} must be text")
    normalized = " ".join(value.split()).strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(character in CONTROL_CHARS for character in normalized)
    ):
        raise ProjectionDispatchError(f"{label} is unsafe")
    return normalized


def _json_object(value: Any, label: str, *, allow_none: bool = False) -> dict[str, Any] | None:
    if value is None and allow_none:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProjectionDispatchError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ProjectionDispatchError(f"{label} must be a JSON object")
    return value


def _normalize_source(source: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(source)
    normalized["object_literal"] = _json_object(
        normalized.get("object_literal"), "object literal", allow_none=True
    )
    normalized["project_scope"] = _json_object(
        normalized.get("project_scope"), "project scope"
    )
    normalized["temporal"] = _json_object(normalized.get("temporal"), "temporal")
    return normalized


def _entity_label(entity_type: str, canonical_name: Any) -> str:
    if entity_type == "self":
        return "The user"
    return _safe_text(canonical_name, "entity canonical name", maximum=500)


def _possessive(label: str) -> str:
    if label == "The user":
        return "The user's"
    return f"{label}'" if label.endswith("s") else f"{label}'s"


def _literal(source: Mapping[str, Any]) -> dict[str, Any]:
    literal = source.get("object_literal")
    if not isinstance(literal, dict) or set(literal) != {
        "kind",
        "datatype",
        "value",
        "unit",
        "approximate",
    }:
        raise ProjectionDispatchError("literal object contract mismatch")
    if literal["kind"] != "literal":
        raise ProjectionDispatchError("object is not a literal")
    return literal


def _sentence(subject: str, affirmative: str, negative: str, source: Mapping[str, Any]) -> str:
    polarity = str(source["polarity"])
    modality = str(source["modality"])
    if polarity == "negated":
        body = negative
    elif modality == "uncertain":
        body = f"may {affirmative}"
    else:
        body = affirmative
    return f"{subject} {body}."


def render_claim_text(source: Mapping[str, Any]) -> str:
    predicate = str(source["predicate"])
    subject = _entity_label(
        str(source["subject_entity_type"]), source.get("subject_canonical_name")
    )
    object_kind = str(source["object_kind"])
    if object_kind == "entity":
        target = _entity_label(
            str(source["object_entity_type"]), source.get("object_canonical_name")
        )
        if predicate == "relationship.has_pet":
            return _sentence(subject, f"has a pet named {target}", f"does not have a pet named {target}", source)
        if predicate == "relationship.lives_with":
            return _sentence(subject, f"lives with {target}", f"does not live with {target}", source)
        if predicate in RELATION_PHRASES:
            phrase = RELATION_PHRASES[predicate]
            return _sentence(subject, f"is {phrase} {target}", f"is not {phrase} {target}", source)
        if predicate in SOCIAL_VERBS:
            verb = SOCIAL_VERBS[predicate]
            negative_verb = SOCIAL_NEGATIVE_VERBS[predicate]
            if str(source["modality"]) == "reported_observation":
                chosen = negative_verb if source["polarity"] == "negated" else verb
                return f"{subject} reports that {subject} {chosen} {target}."
            affirmative = f"{verb} {target}"
            negative = f"{negative_verb} {target}"
            return _sentence(subject, affirmative, negative, source)
        if predicate == "education.attended":
            return _sentence(subject, f"attended {target}", f"did not attend {target}", source)
        if predicate == "employment.worked_for":
            return _sentence(subject, f"worked for {target}", f"did not work for {target}", source)
        if predicate == "occupation.works_as":
            return _sentence(subject, f"works as {target}", f"does not work as {target}", source)
        if predicate == "residence.lives_at":
            return _sentence(subject, f"lives at {target}", f"does not live at {target}", source)
        raise ProjectionDispatchError(f"no entity renderer for {predicate}")

    literal = _literal(source)
    value = literal["value"]
    if predicate in {"identity.name", "identity.name_canonical"}:
        name = _safe_text(value, "name", maximum=200)
        qualifier = "canonical name" if predicate.endswith("_canonical") else "name"
        return f"{_possessive(subject)} {qualifier} is {name}."
    if predicate == "age.reported":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ProjectionDispatchError("reported age is not numeric")
        unit = _safe_text(literal.get("unit"), "age unit", maximum=20)
        approximation = "approximately " if literal.get("approximate") else ""
        return f"{subject} reported an age of {approximation}{value:g} {unit}."
    if predicate == "credential.reported":
        return f"{subject} reported the credential {_safe_text(value, 'credential', maximum=1000)}."
    if predicate == "health.user_reported_observation":
        about = "" if subject == "The user" else f" about {subject}"
        return f"The user reported{about}: {_safe_text(value, 'health observation', maximum=1000)}."
    if predicate == "health.user_reported_uncertain_label":
        about = "" if subject == "The user" else f" about {subject}"
        return f"The user reported an uncertain health label{about}: {_safe_text(value, 'health label', maximum=1000)}."
    if predicate == "life_event.died":
        if value is not True:
            raise ProjectionDispatchError("death observation must use literal true")
        if str(source["modality"]) == "uncertain":
            return f"{subject} may have died."
        return f"{subject} died."
    pet_labels = {
        "pet.breed": "recorded breed",
        "pet.coat_color": "recorded coat color",
        "pet.eye_color": "recorded eye color",
        "pet.hearing_status": "recorded hearing status",
        "pet.sex": "recorded sex",
        "pet.species": "recorded species",
    }
    if predicate in pet_labels:
        return f"{subject} has {pet_labels[predicate]} {_safe_text(value, 'pet value', maximum=300)}."
    if predicate == "pet.weight_reported":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ProjectionDispatchError("pet weight is not numeric")
        unit = _safe_text(literal.get("unit"), "weight unit", maximum=20)
        approximation = "approximately " if literal.get("approximate") else ""
        return f"{subject} was reported to weigh {approximation}{value:g} {unit}."
    if predicate == "stance.reported":
        if not isinstance(value, dict) or set(value) != {
            "topic_key",
            "topic_text",
            "position",
            "orientation",
            "context",
        }:
            raise ProjectionDispatchError("reported stance contract mismatch")
        position = _safe_text(value["position"], "reported position", maximum=1500)
        if position[-1] not in ".!?":
            position = f"{position}."
        # Preserve the owner's first-person wording as attributed evidence. Do not
        # splice it into a third-person sentence or silently rewrite its meaning.
        return f'{subject} reports this position: "{position}"'
    raise ProjectionDispatchError(f"no literal renderer for {predicate}")


def _key_fragment(value: str, *, fallback: str) -> str:
    normalized = KEY_RE.sub("_", value.casefold()).strip("_")
    return normalized[:48] or fallback


def _identity(source: Mapping[str, Any]) -> dict[str, Any]:
    object_kind = str(source["object_kind"])
    literal_hash = source.get("object_literal_sha256")
    if object_kind == "literal":
        calculated = sha256(_literal(source))
        if literal_hash != calculated:
            raise ProjectionDispatchError("literal hash mismatch")
        object_entity_id = None
    elif object_kind == "entity":
        object_entity_id = str(uuid.UUID(str(source["object_entity_id"])))
        literal_hash = None
    else:
        raise ProjectionDispatchError("unsupported object kind")
    return {
        "subject_entity_id": str(uuid.UUID(str(source["subject_entity_id"]))),
        "predicate": str(source["predicate"]),
        "object_kind": object_kind,
        "object_entity_id": object_entity_id,
        "object_literal_sha256": literal_hash,
        "polarity": str(source["polarity"]),
        "modality": str(source["modality"]),
        "semantic_key_sha256": "",
    }


def _preference_payload(source: Mapping[str, Any]) -> dict[str, Any]:
    value = _literal(source)["value"]
    if not isinstance(value, dict):
        raise ProjectionDispatchError("preference value must be an object")
    if source["predicate"] == "preference.life":
        if set(value) != {"domain", "target", "polarity", "context"}:
            raise ProjectionDispatchError("life preference contract mismatch")
        domain = _safe_text(value["domain"], "preference domain", maximum=63)
        target = _safe_text(value["target"], "preference target", maximum=500)
        context = value["context"]
        if context is not None:
            context = _safe_text(context, "preference context", maximum=500)
        return {
            "kind": "preference",
            "preference_class": "life",
            "domain": domain,
            "preference_key": f"life.{domain}.{hashlib.sha256(target.casefold().encode()).hexdigest()[:24]}",
            "value": {"target": target, "context": context},
            "preference_polarity": value["polarity"],
            "scope": "user_global",
            "stability": "tentative" if source["modality"] == "uncertain" else ("contextual" if context else "stable"),
            "surface_policy": source["surface_policy"],
        }
    if source["predicate"] == "preference.response":
        if set(value) != {"dimension", "value"}:
            raise ProjectionDispatchError("response preference contract mismatch")
        dimension = _safe_text(value["dimension"], "response dimension", maximum=63)
        preference_value = _safe_text(value["value"], "response preference", maximum=500)
        return {
            "kind": "preference",
            "preference_class": "response",
            "domain": "response",
            "preference_key": f"response.{dimension}",
            "value": preference_value,
            "preference_polarity": "not_applicable",
            "scope": "user_global",
            "stability": "stable",
            "surface_policy": source["surface_policy"],
        }
    raise ProjectionDispatchError("predicate is not a preference")


def _project_payload(source: Mapping[str, Any]) -> dict[str, Any]:
    project_scope = source.get("project_scope")
    if not isinstance(project_scope, dict) or project_scope.get("state") != "resolved":
        raise ProjectionDispatchError("project scope is not resolved")
    value = _safe_text(_literal(source)["value"], "project knowledge", maximum=2000)
    predicate = str(source["predicate"])
    kind = PROJECT_KIND.get(predicate)
    if kind is None:
        raise ProjectionDispatchError("project predicate is not supported")
    topic = hashlib.sha256(value.casefold().encode()).hexdigest()[:24]
    return {
        "kind": "project_knowledge",
        "project_id": str(uuid.UUID(str(project_scope["project_id"]))),
        "component_key": project_scope.get("component_key"),
        "binding_source": project_scope["binding_source"],
        "knowledge_kind": kind,
        "knowledge_key": f"{kind}.{topic}",
        "canonical_text": value,
        "document_state": "proposed" if kind == "proposed_feature" else "working",
        "authority_level": "user_reported",
        "surface_policy": source["surface_policy"],
    }


def build_projection(owner_user_id: str, source: Mapping[str, Any]) -> dict[str, Any]:
    source = _normalize_source(source)
    owner = str(uuid.UUID(owner_user_id))
    source_owner = source.get("owner_user_id")
    if source_owner is not None:
        try:
            normalized_source_owner = str(uuid.UUID(str(source_owner)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ProjectionDispatchError("source owner mismatch") from exc
        if normalized_source_owner != owner:
            raise ProjectionDispatchError("source owner mismatch")
    if source.get("predicate_registry_version") != REGISTRY_VERSION:
        raise ProjectionDispatchError("source registry mismatch")
    if source.get("evidence_status") != "active" or source.get("subject_entity_status") != "active":
        raise ProjectionDispatchError("source evidence or subject is inactive")
    if source.get("object_kind") == "entity" and source.get("object_entity_status") != "active":
        raise ProjectionDispatchError("source object entity is inactive")
    projection_class = str(source["projection_class"])
    if projection_class in {"direct_claim", "supportive_context", "correction", "reported_stance", "never_surface"}:
        lane = "claim"
        payload = {
            "kind": "claim",
            "claim_class": projection_class,
            "canonical_text": render_claim_text(source),
            "surface_policy": source["surface_policy"],
        }
    elif projection_class in {"life_preference", "response_preference"}:
        lane = "preference"
        payload = _preference_payload(source)
    elif projection_class == "project_knowledge":
        lane = "project_knowledge"
        payload = _project_payload(source)
    else:
        raise ProjectionDispatchError("observation is outside durable lanes")
    identity = _identity(source)
    identity["semantic_key_sha256"] = semantic_key_sha256(owner, lane, identity, payload)
    temporal = source.get("temporal") or {}
    state_validity = (
        lane == "project_knowledge"
        and temporal.get("semantic") == "state_validity"
        and temporal.get("basis") in {"instant", "calendar"}
    )
    projection = {
        "projection_ref": "p01",
        "lane": lane,
        "observation_inputs": [
            {
                "observation_id": str(uuid.UUID(str(source["observation_id"]))),
                "observation_sha256": str(source["observation_sha256"]),
                "stance": "supports",
            }
        ],
        "identity": identity,
        "target": {
            "action": "create",
            "aggregate_id": None,
            "expected_revision_number": None,
            "reason_codes": [],
        },
        "temporal_policy": {
            "canonical_source": "memory.observation_temporal",
            "materialization": "state_validity_only" if state_validity else "link_only",
            "source_observation_id": str(source["observation_id"]) if state_validity else None,
        },
        "review": {
            "state": "manual_review_required",
            "authorization_required": True,
            "reason_codes": ["initial_v5_2_projection_requires_review"],
        },
        "relations": [],
        "payload": payload,
    }
    return projection


def build_packet(owner_user_id: str, source: Mapping[str, Any]) -> dict[str, Any]:
    projection = build_projection(owner_user_id, source)
    packet = {
        "contract_version": CONTRACT_VERSION,
        "predicate_registry_version": REGISTRY_VERSION,
        "projection_policy_version": POLICY_VERSION,
        "projector": PROJECTOR,
        "projector_version": PROJECTOR_VERSION,
        "projections": [projection],
        "packet_sha256": "",
    }
    packet["packet_sha256"] = sha256(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    )
    return packet


def build_reconciled_stance_packet(
    owner_user_id: str,
    primary_source: Mapping[str, Any],
    context_source: Mapping[str, Any],
) -> dict[str, Any]:
    primary = _normalize_source(primary_source)
    context = _normalize_source(context_source)
    owner = str(uuid.UUID(owner_user_id))
    for label, source in (("primary", primary), ("context", context)):
        try:
            source_owner = str(uuid.UUID(str(source["owner_user_id"])))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ProjectionDispatchError(f"{label} source owner mismatch") from exc
        if source_owner != owner:
            raise ProjectionDispatchError(f"{label} source owner mismatch")
        if (
            source.get("predicate_registry_version") != REGISTRY_VERSION
            or source.get("predicate") != "stance.reported"
            or source.get("projection_class") != "reported_stance"
            or source.get("surface_policy")
            != "relevant_recall_or_explicit_recall"
            or source.get("polarity") != "affirmed"
            or source.get("modality") != "reported_belief"
            or source.get("object_kind") != "literal"
            or source.get("subject_entity_type") != "self"
            or source.get("subject_entity_status") != "active"
            or source.get("evidence_status") != "active"
        ):
            raise ProjectionDispatchError(
                f"{label} source is outside the reconciled stance boundary"
            )
    if (
        str(uuid.UUID(str(primary["evidence_id"])))
        != str(uuid.UUID(str(context["evidence_id"])))
        or str(uuid.UUID(str(primary["subject_entity_id"])))
        != str(uuid.UUID(str(context["subject_entity_id"])))
        or str(uuid.UUID(str(primary["observation_id"])))
        == str(uuid.UUID(str(context["observation_id"])))
    ):
        raise ProjectionDispatchError("reconciled stance sources are incompatible")

    projection = build_projection(owner, primary)
    projection["observation_inputs"] = [
        {
            "observation_id": str(uuid.UUID(str(primary["observation_id"]))),
            "observation_sha256": str(primary["observation_sha256"]),
            "stance": "supports",
        },
        {
            "observation_id": str(uuid.UUID(str(context["observation_id"]))),
            "observation_sha256": str(context["observation_sha256"]),
            "stance": "context",
        },
    ]
    projection["review"]["reason_codes"] = [
        "initial_v5_2_reconciled_stance_requires_review"
    ]
    packet = {
        "contract_version": CONTRACT_VERSION,
        "predicate_registry_version": REGISTRY_VERSION,
        "projection_policy_version": POLICY_VERSION,
        "projector": PROJECTOR,
        "projector_version": RECONCILIATION_PROJECTOR_VERSION,
        "projections": [projection],
        "packet_sha256": "",
    }
    packet["packet_sha256"] = sha256(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    )
    return packet


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
