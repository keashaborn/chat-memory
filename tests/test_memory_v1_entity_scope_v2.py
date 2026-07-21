from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_engine.memory_v1_entity_scope_v2 import (
    EntityScopeError,
    EntityScopeModeV2,
    MemoryClaimEntityScopeV2,
    MemoryClaimSelectorContextV2,
    ObjectScopeKindV2,
    PredicateEntityScopeRuleV2,
    claim_row_matches_entity_scope_v2,
)


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER_OWNER = UUID("557ea042-cb82-48f8-9429-472e96c957ef")
TRACE = UUID("10000000-0000-4000-8000-000000000001")
SELF = UUID("20000000-0000-4000-8000-000000000001")
MOTHER = UUID("20000000-0000-4000-8000-000000000002")
PET = UUID("20000000-0000-4000-8000-000000000003")
REQUEST_SHA = "a" * 64


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def rule(
    predicate: str,
    subjects: tuple[UUID, ...],
    *,
    objects: tuple[UUID, ...] = (),
) -> PredicateEntityScopeRuleV2:
    return PredicateEntityScopeRuleV2(
        predicate=predicate,
        subject_entity_ids=tuple(sorted(subjects, key=str)),
        object_scope=(
            ObjectScopeKindV2.ALLOWLISTED_ENTITY
            if objects
            else ObjectScopeKindV2.LITERAL_ONLY
        ),
        object_entity_ids=tuple(sorted(objects, key=str)),
    )


def pet_context() -> MemoryClaimSelectorContextV2:
    scope = MemoryClaimEntityScopeV2.create(
        owner_user_id=OWNER,
        selection_trace_id=TRACE,
        mode=EntityScopeModeV2.PET_PROFILE,
        resolution_policy_version="memory_entity_resolution_policy_v2",
        request_binding_sha256=REQUEST_SHA,
        query_entity_hint_sha256s=(hash_text("pets"),),
        predicate_rules=(
            rule("identity.name", (PET,)),
            rule("pet.breed", (PET,)),
            rule("relationship.has_pet", (SELF,), objects=(PET,)),
        ),
    )
    return MemoryClaimSelectorContextV2.create(entity_scope=scope)


class EntityScopeV2Test(unittest.TestCase):
    def test_pet_scope_accepts_pet_name_and_rejects_self_name(self) -> None:
        context = pet_context()
        pet_name = {
            "owner_user_id": OWNER,
            "predicate": "identity.name",
            "subject_entity_id": PET,
            "object_entity_id": None,
        }
        self_name = {**pet_name, "subject_entity_id": SELF}
        self.assertTrue(claim_row_matches_entity_scope_v2(pet_name, context))
        self.assertFalse(claim_row_matches_entity_scope_v2(self_name, context))

    def test_pet_scope_accepts_only_exact_relationship_edge(self) -> None:
        context = pet_context()
        edge = {
            "owner_user_id": OWNER,
            "predicate": "relationship.has_pet",
            "subject_entity_id": SELF,
            "object_entity_id": PET,
        }
        reversed_edge = {
            **edge,
            "subject_entity_id": PET,
            "object_entity_id": SELF,
        }
        self.assertTrue(claim_row_matches_entity_scope_v2(edge, context))
        self.assertFalse(claim_row_matches_entity_scope_v2(reversed_edge, context))

    def test_family_loss_scope_selects_death_on_related_entity_only(self) -> None:
        scope = MemoryClaimEntityScopeV2.create(
            owner_user_id=OWNER,
            selection_trace_id=TRACE,
            mode=EntityScopeModeV2.FAMILY_PROFILE,
            resolution_policy_version="memory_entity_resolution_policy_v2",
            request_binding_sha256=REQUEST_SHA,
            query_entity_hint_sha256s=(hash_text("family death"),),
            predicate_rules=(rule("life_event.died", (MOTHER,)),),
        )
        context = MemoryClaimSelectorContextV2.create(entity_scope=scope)
        mother_death = {
            "owner_user_id": OWNER,
            "predicate": "life_event.died",
            "subject_entity_id": MOTHER,
            "object_entity_id": None,
        }
        unrelated_death = {**mother_death, "subject_entity_id": PET}
        self.assertTrue(claim_row_matches_entity_scope_v2(mother_death, context))
        self.assertFalse(claim_row_matches_entity_scope_v2(unrelated_death, context))

    def test_cross_owner_row_fails_closed(self) -> None:
        row = {
            "owner_user_id": OTHER_OWNER,
            "predicate": "identity.name",
            "subject_entity_id": PET,
            "object_entity_id": None,
        }
        with self.assertRaisesRegex(EntityScopeError, "owner boundary"):
            claim_row_matches_entity_scope_v2(row, pet_context())

    def test_scope_hash_tamper_fails_closed(self) -> None:
        scope = pet_context().entity_scope
        value = scope.model_dump(mode="python")
        value["scope_manifest_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "manifest hash mismatch"):
            MemoryClaimEntityScopeV2.model_validate(value)

    def test_context_must_bind_exact_predicate_rules(self) -> None:
        scope = pet_context().entity_scope
        with self.assertRaisesRegex(ValueError, "permissions do not match"):
            MemoryClaimSelectorContextV2(
                contract_version="memory_claim_selector_context_v2",
                owner_user_id=OWNER,
                selection_trace_id=TRACE,
                request_binding_sha256=REQUEST_SHA,
                allowed_predicates=("identity.name",),
                entity_scope=scope,
                context_manifest_sha256="0" * 64,
            )

    def test_unscoped_mode_is_disabled_until_registry_proof_exists(self) -> None:
        with self.assertRaisesRegex(ValueError, "disabled"):
            MemoryClaimEntityScopeV2.create(
                owner_user_id=OWNER,
                selection_trace_id=TRACE,
                mode=EntityScopeModeV2.UNSCOPED_PREDICATE_ONLY,
                resolution_policy_version="memory_entity_resolution_policy_v2",
                request_binding_sha256=REQUEST_SHA,
                query_entity_hint_sha256s=(),
                predicate_rules=(rule("identity.name", (SELF,)),),
            )


if __name__ == "__main__":
    unittest.main()
