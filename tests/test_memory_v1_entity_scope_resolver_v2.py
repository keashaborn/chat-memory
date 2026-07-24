from __future__ import annotations

import unittest
from datetime import datetime, timezone
from uuid import UUID

from rag_engine.memory_v1_entity_scope_resolver_v2 import (
    EntityScopeResolutionError,
    EntityScopeSnapshotEdgeV2,
    EntityScopeSnapshotEntityV2,
    MemoryEntityScopeSnapshotV2,
    resolve_memory_claim_selector_context_v2,
)
from rag_engine.memory_v1_selection_envelope import (
    MemoryLane,
    MemorySelectionBudgetPolicyV1,
    MemorySelectionRequestV1,
    QueryEmbeddingArtifactV1,
    QueryEmbeddingSource,
    SelectionDirective,
    Sensitivity,
    SourceContractVersionV1,
)


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER_OWNER = UUID("557ea042-cb82-48f8-9429-472e96c957ef")
SELF = UUID("35029129-27bd-457b-8cb5-82dd37ba32ba")
MOTHER = UUID("f02c2941-a18e-426b-bfa2-3059d7c564c0")
DAD = UUID("3cf07024-5b6b-4ae0-b453-b6cae4860720")
DAHLIA = UUID("0c620047-b302-44ef-b04a-810de3b311fc")
NEKO = UUID("09308a2b-3019-4f59-8fc3-bb1fe1408a0d")
SOURCE = SourceContractVersionV1(
    name="claim_projection",
    version="memory_projection_v5",
)


def entity(
    entity_id: UUID,
    entity_type: str,
    name: str,
    *,
    identity_state: str = "named",
    relationship_role: str | None = None,
) -> EntityScopeSnapshotEntityV2:
    return EntityScopeSnapshotEntityV2(
        entity_id=entity_id,
        entity_type=entity_type,
        canonical_name=name,
        normalized_name=name.casefold(),
        aliases=(),
        identity_state=identity_state,
        relationship_role=relationship_role,
    )


def snapshot(owner: UUID = OWNER) -> MemoryEntityScopeSnapshotV2:
    return MemoryEntityScopeSnapshotV2.create(
        owner_user_id=owner,
        entities=(
            entity(
                SELF,
                "self",
                "Self",
                identity_state="trusted_owner_self",
            ),
            entity(
                MOTHER,
                "person",
                "mother",
                identity_state="role_only",
                relationship_role="family:mother",
            ),
            entity(DAD, "person", "dad"),
            entity(DAHLIA, "animal", "Dahlia"),
            entity(NEKO, "animal", "Neko"),
        ),
        edges=(
            EntityScopeSnapshotEdgeV2(
                claim_id=UUID("1f0c47b4-4717-4574-b926-c5800fd88692"),
                predicate="relationship.has_pet",
                subject_entity_id=SELF,
                subject_entity_type="self",
                object_entity_id=DAHLIA,
                object_entity_type="animal",
            ),
            EntityScopeSnapshotEdgeV2(
                claim_id=UUID("5307fe13-7b61-4303-8a97-d9d632dd5242"),
                predicate="relationship.parent_of",
                subject_entity_id=DAD,
                subject_entity_type="person",
                object_entity_id=SELF,
                object_entity_type="self",
            ),
        ),
    )


def request(query: str, *, owner: UUID = OWNER) -> MemorySelectionRequestV1:
    return MemorySelectionRequestV1.create(
        intent_adapter_version="memory_intent_adapter_v13",
        source_contract_versions=(SOURCE,),
        selection_trace_id=UUID("10000000-0000-4000-8000-000000000001"),
        authenticated_actor_user_id=owner,
        owner_user_id=owner,
        request_id="entity-scope-resolver-test",
        thread_id=UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d"),
        query_text=query,
        query_vector=(0.125, -0.25, 0.5),
        query_embedding=QueryEmbeddingArtifactV1.from_vector(
            source=QueryEmbeddingSource.PRIVATE_LOCAL,
            model_version="test",
            vector=(0.125, -0.25, 0.5),
        ),
        memory_intent="personal_recall",
        domains=("personal",),
        requested_lanes=(MemoryLane.CLAIM,),
        selection_directive=SelectionDirective.EVALUATE,
        explicit_recall=True,
        project_key=None,
        component_key=None,
        max_sensitivity=Sensitivity.HIGH,
        selected_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
        budget_policy=MemorySelectionBudgetPolicyV1.standard(),
    )


class MemoryEntityScopeResolverV2Tests(unittest.TestCase):
    def test_generic_pet_profile_uses_only_governed_has_pet_neighbors(self) -> None:
        context = resolve_memory_claim_selector_context_v2(
            request=request("Do you know anything about my pets?"),
            claim_context={
                "domain": "pet_profile",
                "allowed_predicates": [
                    "identity.name",
                    "pet.breed",
                    "pet.sex",
                    "relationship.has_pet",
                ],
            },
            snapshot=snapshot(),
        )

        rules = {item.predicate: item for item in context.entity_scope.predicate_rules}
        self.assertEqual(rules["identity.name"].subject_entity_ids, (DAHLIA,))
        self.assertNotIn(NEKO, rules["identity.name"].subject_entity_ids)
        self.assertEqual(rules["relationship.has_pet"].subject_entity_ids, (SELF,))
        self.assertEqual(rules["relationship.has_pet"].object_entity_ids, (DAHLIA,))

    def test_exact_name_correction_scopes_to_neko_not_dahlia(self) -> None:
        context = resolve_memory_claim_selector_context_v2(
            request=request("Was it Nemo or Neko?"),
            claim_context={
                "domain": "name_correction",
                "allowed_predicates": ["identity.name_canonical"],
            },
            snapshot=snapshot(),
        )

        rule = context.entity_scope.predicate_rules[0]
        self.assertEqual(rule.predicate, "identity.name_canonical")
        self.assertEqual(rule.subject_entity_ids, (NEKO,))

    def test_generic_pet_loss_cannot_select_human_death(self) -> None:
        context = resolve_memory_claim_selector_context_v2(
            request=request("Do you remember when I lost my pet?"),
            claim_context={
                "domain": "pet_loss",
                "allowed_predicates": ["life_event.died"],
            },
            snapshot=snapshot(),
        )

        rule = context.entity_scope.predicate_rules[0]
        self.assertEqual(rule.subject_entity_ids, (DAHLIA,))
        self.assertNotIn(MOTHER, rule.subject_entity_ids)

    def test_family_death_includes_role_only_mother_and_graph_linked_dad(self) -> None:
        context = resolve_memory_claim_selector_context_v2(
            request=request("Have I had any deaths in the family?"),
            claim_context={
                "domain": "family_death",
                "allowed_predicates": ["life_event.died"],
            },
            snapshot=snapshot(),
        )

        rule = context.entity_scope.predicate_rules[0]
        self.assertEqual(set(rule.subject_entity_ids), {MOTHER, DAD})
        self.assertNotIn(DAHLIA, rule.subject_entity_ids)

    def test_stance_recall_is_owner_self_only(self) -> None:
        context = resolve_memory_claim_selector_context_v2(
            request=request("What have I said about evidence?"),
            claim_context={
                "domain": "stance_recall",
                "allowed_predicates": ["stance.reported"],
            },
            snapshot=snapshot(),
        )

        self.assertEqual(
            context.entity_scope.predicate_rules[0].subject_entity_ids,
            (SELF,),
        )

    def test_cross_owner_snapshot_fails_closed(self) -> None:
        with self.assertRaisesRegex(EntityScopeResolutionError, "owner boundary"):
            resolve_memory_claim_selector_context_v2(
                request=request("Do you know anything about my pets?"),
                claim_context={
                    "domain": "pet_profile",
                    "allowed_predicates": ["identity.name"],
                },
                snapshot=snapshot(OTHER_OWNER),
            )

    def test_unmatched_named_correction_fails_closed(self) -> None:
        with self.assertRaisesRegex(EntityScopeResolutionError, "one exact"):
            resolve_memory_claim_selector_context_v2(
                request=request("Was it Nemo or Koda?"),
                claim_context={
                    "domain": "name_correction",
                    "allowed_predicates": ["identity.name_canonical"],
                },
                snapshot=snapshot(),
            )


if __name__ == "__main__":
    unittest.main()
