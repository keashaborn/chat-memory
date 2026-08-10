"""Pure projection-contract tests; no embedder or Qdrant client is imported."""

from __future__ import annotations

from hashlib import sha256
import inspect
from pathlib import Path
import struct
import unittest
from uuid import UUID

from rag_engine.governed_memory.admission import recompute_claim_state_sha256
from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.projection import (
    PROJECTION_CONTRACT_SHA256,
    PROJECTION_DELETE_COMMAND_FIELDS,
    PROJECTION_OUTBOX_FIELDS,
    PROJECTION_TEST_VECTORS,
    QDRANT_PAYLOAD_FIELDS,
    RELATIONAL_RENDERER_SHA256,
    build_projection_delete,
    build_projection_point,
    normalize_embedding,
    projection_manifest_sha256,
    recompute_projection_contract_sha256,
    supersede_stale_projection,
)
from tests.memory._fixtures import (
    CLAIM_A,
    REVISION_B,
    assert_unit_vector,
    deterministic_vector,
    make_claim_row,
    make_projection_outbox,
)


ROOT = Path(__file__).resolve().parents[2]
FOUNDATION_SQL = (
    ROOT / "governed-memory-migrations" / "0001_foundation" / "forward.pgsql"
)


def independent_framed_sha256(
    domain: str,
    fields: tuple[tuple[str, str | None], ...],
) -> str:
    material = bytearray(f"{domain}\n".encode("utf-8"))
    for name, value in fields:
        if value is None:
            material.extend(f"{name}:-:\n".encode("utf-8"))
        else:
            encoded = value.encode("utf-8")
            material.extend(f"{name}:{len(encoded)}:".encode("utf-8"))
            material.extend(encoded)
            material.extend(b"\n")
    return sha256(material).hexdigest()


def build(
    claim: dict[str, object] | None = None,
    *,
    outbox: dict[str, object] | None = None,
    vector: object | None = None,
) -> dict[str, object]:
    claim = make_claim_row() if claim is None else claim
    outbox = make_projection_outbox(claim) if outbox is None else outbox
    vector = deterministic_vector() if vector is None else vector
    return build_projection_point(claim, outbox, vector)  # type: ignore[arg-type]


class ProjectionTests(unittest.TestCase):
    def test_projection_contract_and_manifest_fixed_vectors_are_exact(self) -> None:
        self.assertEqual(tuple(sorted(QDRANT_PAYLOAD_FIELDS)), QDRANT_PAYLOAD_FIELDS)
        self.assertEqual(
            PROJECTION_CONTRACT_SHA256,
            "9a54cf123493a25646b29cadf2be068039ac7062ed6d58e69e8b6e7a58dfe59a",
        )
        self.assertEqual(
            recompute_projection_contract_sha256(), PROJECTION_CONTRACT_SHA256
        )

        contract = PROJECTION_TEST_VECTORS["projection_contract"]
        self.assertEqual(contract["domain"], "governed_memory.projection_contract.v1")
        self.assertEqual(
            independent_framed_sha256(contract["domain"], contract["ordered_fields"]),
            contract["sha256"],
        )
        operation = PROJECTION_TEST_VECTORS["projection_operation"]
        self.assertEqual(
            operation["sha256"],
            "bd26e95f74ddaf4dc6fc3c06da17851e65f50a86629531db56ecd70b5ef885be",
        )
        self.assertEqual(
            independent_framed_sha256(operation["domain"], operation["ordered_fields"]),
            operation["sha256"],
        )
        values = dict(operation["ordered_fields"])
        self.assertEqual(
            projection_manifest_sha256(
                owner_user_id=UUID(values["owner_user_id"]),
                claim_id=UUID(values["claim_id"]),
                revision_id=UUID(values["revision_id"]),
                operation_id=UUID(values["operation_id"]),
                operation=values["operation"],
                sequence_number=int(values["sequence_number"]),
                revision_sha256=values["revision_sha256"],
                selection_binding_sha256=values["selection_binding_sha256"],
                retrieval_text_sha256=values["retrieval_text_sha256"],
                embedding_input_sha256=values["embedding_input_sha256"],
            ),
            operation["sha256"],
        )

    def test_renderer_fixed_vector_is_exact_canonical_utf8(self) -> None:
        renderer = PROJECTION_TEST_VECTORS["renderer"]
        expected = (
            b'{"object":{"kind":"literal","literal":"caf\xc3\xa9:\\n\xe7\x8c\xab"},'
            b'"predicate":"preference.personal","subject":{"display_name":null,'
            b'"entity_key":"self","entity_type":"self"}}'
        )
        self.assertEqual(renderer["rendered_utf8"], expected)
        self.assertEqual(sha256(expected).hexdigest(), renderer["rendered_sha256"])
        self.assertEqual(
            RELATIONAL_RENDERER_SHA256,
            "f77b782b3e549b30a46b4beb7e25b248018f6c0f3597b36d0020103570e66442",
        )

    def test_builder_accepts_only_claim_outbox_and_vector(self) -> None:
        self.assertEqual(
            tuple(inspect.signature(build_projection_point).parameters),
            ("claim", "outbox_record", "vector"),
        )
        self.assertEqual(tuple(make_projection_outbox()), PROJECTION_OUTBOX_FIELDS)

    def test_normalization_is_deterministic_float32_and_unit_length(self) -> None:
        normalized = normalize_embedding(deterministic_vector())
        self.assertEqual(len(normalized), 3_072)
        self.assertAlmostEqual(normalized[0], 0.6, places=7)
        self.assertAlmostEqual(normalized[1], 0.8, places=7)
        assert_unit_vector(list(normalized))
        for value in normalized:
            self.assertEqual(value, struct.unpack("<f", struct.pack("<f", value))[0])

    def test_malformed_and_huge_integer_embeddings_fail_as_contract_violations(self) -> None:
        malformed = (
            [0.0] * 3_072,
            [float("nan")] + [0.0] * 3_071,
            [float("inf")] + [0.0] * 3_071,
            [True] + [0.0] * 3_071,
            [3.0, 4.0],
            [10**10_000] + [0.0] * 3_071,
        )
        for vector in malformed:
            with self.subTest(first=vector[:2]), self.assertRaises(ContractViolation):
                normalize_embedding(vector)

    def test_point_id_payload_vector_and_hash_are_exact(self) -> None:
        point = build()
        self.assertEqual(point["point_id"], str(CLAIM_A))
        self.assertEqual(tuple(sorted(point["payload"])), QDRANT_PAYLOAD_FIELDS)
        self.assertEqual(point["payload"]["projection_sequence"], 1)
        self.assertEqual(
            point["payload"]["projection_manifest_sha256"],
            make_projection_outbox()["projection_manifest_sha256"],
        )
        vector = point["vector"]
        expected_vector_hash = sha256(
            b"".join(struct.pack("<f", value) for value in vector)
        ).hexdigest()
        self.assertEqual(point["payload"]["vector_sha256"], expected_vector_hash)
        for prohibited in ("text", "retrieval_text", "subject", "object"):
            self.assertNotIn(prohibited, point["payload"])

    def test_only_current_active_projectable_authority_can_upsert(self) -> None:
        malformed: list[dict[str, object]] = [
            make_claim_row(lifecycle_state="retracted"),
            make_claim_row(lifecycle_state="correction_pending"),
            make_claim_row(current=False),
            make_claim_row(epistemic_status="unreviewed"),
        ]
        not_projectable = make_claim_row()
        not_projectable["projectable"] = False
        not_projectable["state_sha256"] = recompute_claim_state_sha256(not_projectable)
        malformed.append(not_projectable)
        for claim in malformed:
            with self.subTest(state=claim["lifecycle_state"]), self.assertRaises(
                ContractViolation
            ):
                build(claim)

    def test_policy_catalog_renderer_and_claim_hashes_are_authoritative(self) -> None:
        mutations = (
            ("surface", "implicit"),
            ("requires_explicit", True),
            ("sensitivity", "restricted_identifier"),
            ("predicate_catalog_sha256", "0" * 64),
            ("retrieval_text_sha256", "0" * 64),
            ("state_sha256", "0" * 64),
        )
        for field, value in mutations:
            claim = make_claim_row()
            claim[field] = value
            with self.subTest(field=field), self.assertRaises(ContractViolation):
                build(claim)

    def test_outbox_must_bind_exact_current_claim_and_sequence(self) -> None:
        claim = make_claim_row(projection_sequence=3)
        valid = make_projection_outbox(claim)
        self.assertEqual(build(claim, outbox=valid)["payload"]["projection_sequence"], 3)
        mutations = {
            "owner_user_id": "00000000-0000-4000-8000-000000000001",
            "claim_id": "00000000-0000-4000-8000-000000000002",
            "revision_id": str(REVISION_B),
            "sequence_number": 2,
            "revision_sha256": "0" * 64,
            "selection_binding_sha256": "0" * 64,
            "retrieval_text_sha256": "0" * 64,
            "embedding_input_sha256": "0" * 64,
            "projection_contract_sha256": "0" * 64,
            "projection_manifest_sha256": "0" * 64,
            "operation": "delete",
            "state": "pending",
        }
        for field, value in mutations.items():
            with self.subTest(field=field), self.assertRaises(ContractViolation):
                build(claim, outbox={**valid, field: value})

    def test_correction_reuses_point_id_and_requires_new_exact_revision(self) -> None:
        original = build()
        corrected = make_claim_row(
            revision_id=REVISION_B,
            revision_number=2,
            projection_sequence=2,
            object_literal="amber",
        )
        replacement = build(corrected)
        self.assertEqual(original["point_id"], replacement["point_id"])
        self.assertNotEqual(
            original["payload"]["revision_id"], replacement["payload"]["revision_id"]
        )
        self.assertNotEqual(
            original["payload"]["projection_manifest_sha256"],
            replacement["payload"]["projection_manifest_sha256"],
        )

    def test_delete_builder_is_exact_and_contains_no_vector_or_text(self) -> None:
        claim = make_claim_row(lifecycle_state="retracted", projection_sequence=2)
        outbox = make_projection_outbox(claim, operation="delete")
        command = build_projection_delete(
            outbox,
            collection_alias="governed_memory_active",
            physical_collection="governed_memory_build_deadbeef0000",
        )
        self.assertEqual(tuple(sorted(command)), PROJECTION_DELETE_COMMAND_FIELDS)
        self.assertEqual(command["point_id"], str(CLAIM_A))
        self.assertEqual(command["projection_sequence"], 2)
        self.assertEqual(command["operation"], "delete")
        serialized_keys = set(command)
        self.assertFalse(serialized_keys & {"vector", "text", "payload"})

    def test_stale_lower_sequence_has_one_non_qdrant_terminal_result(self) -> None:
        stale_claim = make_claim_row(projection_sequence=1)
        stale = make_projection_outbox(stale_claim, state="claimed")
        result = supersede_stale_projection(
            stale,
            authoritative_projection_sequence=2,
        )
        self.assertEqual(result["from_state"], "claimed")
        self.assertEqual(result["to_state"], "superseded")
        self.assertEqual(result["outbox_sequence"], 1)
        self.assertEqual(result["authoritative_projection_sequence"], 2)
        self.assertEqual(result["reason_code"], "stale_projection_sequence")
        self.assertFalse(result["applied"])
        self.assertFalse(result["retryable"])
        serialized = " ".join(result)
        for prohibited in (
            "qdrant",
            "physical_collection",
            "vector_sha256",
            "applied_at",
            "verification_sha256",
        ):
            self.assertNotIn(prohibited, serialized)

    def test_equal_higher_or_terminal_sequence_cannot_be_superseded(self) -> None:
        row = make_projection_outbox(state="claimed")
        for authoritative in (1, 0):
            with self.subTest(authoritative=authoritative), self.assertRaises(
                ContractViolation
            ):
                supersede_stale_projection(
                    row, authoritative_projection_sequence=authoritative
                )
        for terminal in ("applied", "superseded", "failed_terminal"):
            with self.subTest(state=terminal), self.assertRaises(ContractViolation):
                supersede_stale_projection(
                    {**row, "state": terminal},
                    authoritative_projection_sequence=2,
                )

    def test_sql_supersession_matches_lower_sequence_only_contract(self) -> None:
        sql = FOUNDATION_SQL.read_text(encoding="utf-8")
        start = sql.index("WITH superseded AS")
        end = sql.index("FOR candidate IN", start)
        block = sql[start:end]
        self.assertIn("outbox.sequence_number < claim.projection_sequence", block)
        self.assertIn("last_error_code = 'stale_projection_sequence'", block)
        self.assertIn("'projection_superseded'", block)
        self.assertNotIn("state = 'applied'", block)
        self.assertNotIn("state = 'retryable'", block)


if __name__ == "__main__":
    unittest.main()
