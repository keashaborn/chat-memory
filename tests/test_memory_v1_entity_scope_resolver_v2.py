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
MONIKA = UUID("ca0724b2-100d-4b15-80aa-4016b39cd8b5")
CINDY = UUID("798a535e-ee11-46b1-b8f7-24dc0bcc5d57")
CLINICAL_PSYCHOLOGIST = UUID("98cc43cd-514a-4f3e-9ba9-7caa84175e57")
BCBA = UUID("d73cc811-1772-4cba-96d7-0e021c8053a7")
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
            entity(MONIKA, "person", "Monika"),
            entity(CINDY, "person", "Cindy"),
            entity(CLINICAL_PSYCHOLOGIST, "concept", "clinical psychologist"),
            entity(BCBA, "concept", "BCBA"),
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
            EntityScopeSnapshotEdgeV2(
                claim_id=UUID("2c3ab91d-c423-43d9-8d55-19e4f1069028"),
                predicate="occupation.works_as",
                subject_entity_id=SELF,
                subject_entity_type="self",
                object_entity_id=CLINICAL_PSYCHOLOGIST,
                object_entity_type="concept",
            ),
            EntityScopeSnapshotEdgeV2(
                claim_id=UUID("3073b518-1f12-4fa3-93d6-eaf0b22f864c"),
                predicate="occupation.works_as",
                subject_entity_id=SELF,
                subject_entity_type="self",
                object_entity_id=BCBA,
                object_entity_type="concept",
            ),
            EntityScopeSnapshotEdgeV2(
                claim_id=UUID("f4688838-7193-4e0e-961f-c9bbbf9904c7"),
                predicate="relationship.caregiver_for",
                subject_entity_id=SELF,
                subject_entity_type="self",
                object_entity_id=MONIKA,
                object_entity_type="person",
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

    def test_profession_recall_scopes_to_governed_occupation_neighbors(self) -> None:
        context = resolve_memory_claim_selector_context_v2(
            request=request("What professions have I worked in?"),
            claim_context={
                "domain": "life_context",
                "allowed_predicates": ["occupation.works_as"],
            },
            snapshot=snapshot(),
        )

        rule = context.entity_scope.predicate_rules[0]
        self.assertEqual(rule.subject_entity_ids, (SELF,))
        self.assertEqual(
            set(rule.object_entity_ids),
            {BCBA, CLINICAL_PSYCHOLOGIST},
        )

    def test_named_profession_recall_narrows_to_exact_concept(self) -> None:
        context = resolve_memory_claim_selector_context_v2(
            request=request("Have I worked as a clinical psychologist?"),
            claim_context={
                "domain": "life_context",
                "allowed_predicates": ["occupation.works_as"],
            },
            snapshot=snapshot(),
        )

        self.assertEqual(
            context.entity_scope.predicate_rules[0].object_entity_ids,
            (CLINICAL_PSYCHOLOGIST,),
        )

    def test_caregiving_recall_scopes_to_governed_recipient(self) -> None:
        context = resolve_memory_claim_selector_context_v2(
            request=request("Do you remember who I care for?"),
            claim_context={
                "domain": "life_context",
                "allowed_predicates": ["relationship.caregiver_for"],
            },
            snapshot=snapshot(),
        )

        self.assertEqual(
            context.entity_scope.predicate_rules[0].object_entity_ids,
            (MONIKA,),
        )

    def test_named_non_recipient_cannot_broaden_caregiving_scope(self) -> None:
        with self.assertRaisesRegex(
            EntityScopeResolutionError,
            "resolved no predicate rules",
        ):
            resolve_memory_claim_selector_context_v2(
                request=request("Am I a caregiver for Cindy?"),
                claim_context={
                    "domain": "life_context",
                    "allowed_predicates": ["relationship.caregiver_for"],
                },
                snapshot=snapshot(),
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
