"""Provider-envelope tests; no SDK, credentials, or network are used."""

from __future__ import annotations

import copy
from datetime import timedelta
from hashlib import sha256
import inspect
import json
from pathlib import Path
import unittest
from uuid import UUID

from rag_engine.governed_memory.contracts import (
    ContractViolation,
    canonical_sha256,
    selection_binding_sha256,
)
from rag_engine.governed_memory.extraction import (
    COMPLETE_EXTRACTION_ITEM_FIELDS,
    ENTITY_KEY_TEST_VECTOR,
    PROPOSAL_HASH_BINDING_FIELDS,
    PROPOSAL_TEST_VECTORS,
    build_provider_request,
    derive_source_local_entity_key,
    parse_predicate_catalog,
    policy_for_sensitivity,
    recompute_proposal_sha256,
    validate_provider_result,
)
from rag_engine.governed_memory.postgres_adapter import (
    EXTRACTION_LEASE_FIELDS,
    extraction_lease_to_provider_inputs,
)
from tests.memory._fixtures import (
    EVIDENCE_A,
    FIXTURE_PROVENANCE,
    JOB_A,
    MESSAGE_A,
    NOW,
    OPERATION_A,
    OWNER_A,
    PREDICATE_CATALOG,
    PROVIDER_CALL_A,
    SOURCE_TEXT,
    THREAD_A,
    WINDOW_A,
    make_extraction_job,
    make_provider_output,
    make_selected_evidence,
)


MODEL = "synthetic-extraction-model"
SCHEMA = "governed-memory-extraction"
ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "governed-memory-migrations" / "predicate_catalog.json"
SCHEMA_CONTRACT_PATH = ROOT / "governed-memory-migrations" / "schema_contract.json"
FOUNDATION_SQL_PATH = (
    ROOT / "governed-memory-migrations" / "0001_foundation" / "forward.pgsql"
)


class ExtractionContractTests(unittest.TestCase):
    def request(self) -> dict[str, object]:
        return build_provider_request(
            make_extraction_job(),
            make_selected_evidence(),
            MODEL,
            SCHEMA,
            PREDICATE_CATALOG,
        )

    def test_postgres_lease_reconstructs_cold_provider_inputs(self) -> None:
        selected = make_selected_evidence()
        values = {
            "owner_user_id": OWNER_A,
            "job_id": JOB_A,
            "evidence_id": EVIDENCE_A,
            "source_kind": "conversation_message",
            "source_message_id": MESSAGE_A,
            "source_thread_id": THREAD_A,
            "source_window_id": WINDOW_A,
            "source_window_sha256": selected["window_sha256"],
            "source_sha256": selected["source_sha256"],
            "selected_sha256": selected["selected_sha256"],
            "selection_binding_sha256": selected["selection_binding_sha256"],
            "selected_start_utf8": selected["start_utf8"],
            "selected_end_utf8": selected["end_utf8"],
            "context_message_id": None,
            "context_sha256": None,
            "review_excerpt": selected["selected_text"],
            "predicate_catalog_sha256": parse_predicate_catalog(
                PREDICATE_CATALOG
            ).catalog_sha256,
            "attempt_number": 2,
            "lease_token": UUID("12121212-1212-4212-8212-121212121212"),
            "lease_expires_at": NOW + timedelta(minutes=1),
            "provider_call_id": PROVIDER_CALL_A,
            "provider_operation_id": OPERATION_A,
        }
        lease_row = {field: values[field] for field in EXTRACTION_LEASE_FIELDS}
        inputs = extraction_lease_to_provider_inputs(lease_row)
        request = build_provider_request(
            inputs["job"],
            inputs["evidence"],
            MODEL,
            SCHEMA,
            PREDICATE_CATALOG,
        )
        self.assertEqual(request["source_sha256"], selected["source_sha256"])
        self.assertEqual(request["attempt_number"], 2)
        self.assertIsNone(inputs["context_lookup"])

        missing = dict(lease_row)
        del missing["source_sha256"]
        with self.assertRaisesRegex(
            ContractViolation, "invalid_extraction_lease_row"
        ):
            extraction_lease_to_provider_inputs(missing)

    def test_checked_in_catalog_parses_and_one_exact_hash_binds_schema_and_sql(self) -> None:
        raw = CATALOG_PATH.read_bytes()
        raw_catalog = json.loads(raw.decode("utf-8"))
        catalog = parse_predicate_catalog(raw_catalog)
        expected_semantic_sha256 = canonical_sha256(
            "governed_memory.predicate_catalog",
            {
                "catalog_name": raw_catalog["catalog_name"],
                "schema_revision": raw_catalog["schema_revision"],
                "rules": tuple(rule.material() for rule in catalog.rules),
            },
        )
        self.assertEqual(catalog.catalog_sha256, expected_semantic_sha256)
        self.assertNotEqual(catalog.catalog_sha256, sha256(raw).hexdigest())
        schema_contract = SCHEMA_CONTRACT_PATH.read_text(encoding="utf-8")
        foundation_sql = FOUNDATION_SQL_PATH.read_text(encoding="utf-8")
        self.assertIn(catalog.catalog_sha256, schema_contract)
        self.assertIn(catalog.catalog_sha256, foundation_sql)

    def test_request_is_exactly_bound_to_job_owner_evidence_model_and_schema(self) -> None:
        request = self.request()
        self.assertEqual(request["owner_user_id"], str(OWNER_A))
        self.assertEqual(request["job_id"], str(JOB_A))
        self.assertEqual(request["model"], MODEL)
        self.assertEqual(request["schema"], SCHEMA)
        self.assertEqual(
            set(request["external_payload"]),
            {
                "schema",
                "selected_text",
                "selected_sha256",
                "bounded_context",
                "predicate_catalog",
                "result_contract",
            },
        )
        self.assertEqual(request["external_payload"]["selected_text"], SOURCE_TEXT)
        self.assertEqual(
            request["external_payload"]["predicate_catalog"]["catalog_sha256"],
            request["predicate_catalog_sha256"],
        )
        self.assertRegex(request["request_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            request["external_payload"]["result_contract"]["minimum_facts"],
            0,
        )
        self.assertEqual(
            request["external_payload"]["result_contract"]["fact_keys"],
            [
                "epistemic_status",
                "object",
                "predicate",
                "selected_sha256",
                "sensitivity",
                "subject",
            ],
        )

    def test_external_payload_has_no_internal_identifier_or_uuid(self) -> None:
        evidence = make_selected_evidence()
        selected_text = SOURCE_TEXT[:15]
        selected_sha256 = sha256(selected_text.encode("utf-8")).hexdigest()
        evidence["selected_text"] = selected_text
        evidence["selected_sha256"] = selected_sha256
        evidence["end_utf8"] = len(selected_text.encode("utf-8"))
        evidence["selection_binding_sha256"] = selection_binding_sha256(
            owner_user_id=OWNER_A,
            source_kind=evidence["source_kind"],
            source_message_id=UUID(evidence["source_message_id"]),
            source_thread_id=UUID(evidence["source_thread_id"]),
            source_window_id=UUID(evidence["source_window_id"]),
            window_sha256=evidence["window_sha256"],
            source_sha256=evidence["source_sha256"],
            selected_sha256=selected_sha256,
            start_utf8=0,
            end_utf8=evidence["end_utf8"],
            context_message_id=None,
            context_sha256=None,
        )
        request = build_provider_request(
            make_extraction_job(),
            evidence,
            MODEL,
            SCHEMA,
            PREDICATE_CATALOG,
        )
        external = request["external_payload"]
        serialized = json.dumps(external, sort_keys=True)
        for forbidden_key in (
            "selection_binding_sha256",
            "source_sha256",
            "window_sha256",
            "owner_user_id",
            "job_id",
            "evidence_id",
            "provider_call_id",
            "operation_id",
            "source_message_id",
            "source_thread_id",
            "source_window_id",
            "context_message_id",
            "start_utf8",
            "end_utf8",
        ):
            with self.subTest(forbidden_key=forbidden_key):
                self.assertNotIn(forbidden_key, serialized)
        for identifier in (
            request["owner_user_id"],
            request["job_id"],
            request["evidence_id"],
            request["provider_call_id"],
            request["operation_id"],
            request["source_message_id"],
            request["source_thread_id"],
            request["source_window_id"],
            request["selection_binding_sha256"],
            request["source_sha256"],
            request["window_sha256"],
        ):
            with self.subTest(identifier=identifier):
                self.assertNotIn(identifier, serialized)

    def test_request_hash_changes_for_attempt_and_model_and_catalog_is_closed(self) -> None:
        baseline = self.request()["request_sha256"]
        job = make_extraction_job()
        job["attempt_number"] = 2
        changed_attempt = build_provider_request(
            job,
            make_selected_evidence(),
            MODEL,
            SCHEMA,
            PREDICATE_CATALOG,
        )
        changed_model = build_provider_request(
            make_extraction_job(),
            make_selected_evidence(),
            MODEL + "-other",
            SCHEMA,
            PREDICATE_CATALOG,
        )
        changed_catalog = copy.deepcopy(PREDICATE_CATALOG)
        changed_catalog["schema_revision"] = 2
        for changed in (changed_attempt, changed_model):
            self.assertNotEqual(changed["request_sha256"], baseline)
        with self.assertRaises(ContractViolation):
            build_provider_request(
                make_extraction_job(),
                make_selected_evidence(),
                MODEL,
                SCHEMA,
                changed_catalog,
            )

    def test_valid_result_becomes_one_pending_review_proposal(self) -> None:
        request = self.request()
        batch = validate_provider_result(request, make_provider_output())
        self.assertEqual(len(batch["proposals"]), 1)
        proposal = batch["proposals"][0]
        self.assertEqual(tuple(sorted(proposal)), COMPLETE_EXTRACTION_ITEM_FIELDS)
        self.assertEqual(
            tuple(sorted(batch["proposal_hash_binding"])),
            tuple(sorted(PROPOSAL_HASH_BINDING_FIELDS)),
        )
        self.assertEqual(
            batch["proposal_hash_binding"]["owner_user_id"],
            request["owner_user_id"],
        )
        self.assertEqual(
            batch["proposal_hash_binding"]["job_id"], request["job_id"]
        )
        self.assertEqual(
            batch["proposal_hash_binding"]["selection_binding_sha256"],
            request["selection_binding_sha256"],
        )
        self.assertEqual(proposal["fact_index"], 0)
        self.assertEqual(proposal["subject_entity_key"], "self")
        self.assertIsNone(proposal["subject_display_name"])
        self.assertRegex(proposal["proposal_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(batch["receipt"]["proposal_count"], 1)

    def test_cross_evidence_output_is_rejected(self) -> None:
        output = make_provider_output()
        output["facts"][0]["selected_sha256"] = "0" * 64
        with self.assertRaises(ContractViolation):
            validate_provider_result(self.request(), output)

    def test_provider_cannot_set_policy_validity_or_internal_identity_fields(self) -> None:
        forbidden = (
            "domains",
            "intents",
            "surface",
            "requires_explicit",
            "projectable",
            "valid_from",
            "valid_to",
            "subject_entity_key",
            "object_entity_key",
            "proposal_id",
            "operation_id",
            "semantic_key_sha256",
            "proposal_sha256",
        )
        for field in forbidden:
            output = make_provider_output()
            output["facts"][0][field] = "provider-controlled"
            with self.subTest(field=field), self.assertRaises(ContractViolation):
                validate_provider_result(self.request(), output)

    def test_unknown_or_missing_provider_fields_are_rejected(self) -> None:
        extra = make_provider_output()
        extra["untrusted_extra"] = True
        missing = make_provider_output()
        del missing["facts"][0]["predicate"]
        for value in (extra, missing):
            with self.subTest(value=value), self.assertRaises(ContractViolation):
                validate_provider_result(self.request(), value)

    def test_noncanonical_and_nonfinite_values_are_rejected(self) -> None:
        wrong_type = make_provider_output()
        wrong_type["facts"][0]["object"] = {"kind": "literal", "value": 1.5}
        malformed_predicate = make_provider_output()
        malformed_predicate["facts"][0]["predicate"] = "DROP TABLE synthetic"
        for value in (wrong_type, malformed_predicate):
            with self.subTest(value=value), self.assertRaises(ContractViolation):
                validate_provider_result(self.request(), value)

    def test_noncatalog_predicate_and_object_kind_mismatch_are_rejected(self) -> None:
        noncatalog = make_provider_output()
        noncatalog["facts"][0]["predicate"] = "preference.unknown"
        wrong_object_kind = make_provider_output()
        wrong_object_kind["facts"][0]["object"] = {"kind": "integer", "value": 7}
        for value in (noncatalog, wrong_object_kind):
            with self.subTest(value=value), self.assertRaises(ContractViolation):
                validate_provider_result(self.request(), value)

    def test_catalog_denies_subject_sensitivity_and_epistemic_mismatches(self) -> None:
        wrong_subject = make_provider_output()
        wrong_subject["facts"][0]["subject"] = {
            "kind": "person",
            "label": "Synthetic person",
        }
        wrong_sensitivity = make_provider_output()
        wrong_sensitivity["facts"][0]["sensitivity"] = "sensitive_third_party"
        wrong_epistemic = make_provider_output()
        wrong_epistemic["facts"][0]["epistemic_status"] = "unsupported"
        for field, value in (
            ("subject", wrong_subject),
            ("sensitivity", wrong_sensitivity),
            ("epistemic_status", wrong_epistemic),
        ):
            with self.subTest(field=field), self.assertRaises(ContractViolation):
                validate_provider_result(self.request(), value)

    def test_selected_offsets_and_source_message_are_authority_bound(self) -> None:
        shifted = make_selected_evidence()
        selected_bytes = shifted["selected_text"].encode("utf-8")
        shifted["start_utf8"] = 7
        shifted["end_utf8"] = 7 + len(selected_bytes)

        wrong_message = make_selected_evidence()
        wrong_message["source_message_id"] = (
            "00000000-0000-4000-8000-000000000010"
        )

        for field, evidence in (("offsets", shifted), ("message_id", wrong_message)):
            with self.subTest(field=field), self.assertRaises(ContractViolation):
                build_provider_request(
                    make_extraction_job(),
                    evidence,
                    MODEL,
                    SCHEMA,
                    PREDICATE_CATALOG,
                )

    def test_proposal_and_operation_ids_are_deterministic_and_fact_index_bound(self) -> None:
        request = self.request()
        output = make_provider_output()
        second_fact = copy.deepcopy(output["facts"][0])
        second_fact["object"]["value"] = "amber"
        output["facts"].append(second_fact)
        first = validate_provider_result(request, output)["proposals"]
        second = validate_provider_result(request, output)["proposals"]
        self.assertEqual(first, second)
        self.assertEqual([item["fact_index"] for item in first], [0, 1])
        self.assertEqual(len({item["proposal_id"] for item in first}), 2)
        self.assertEqual(len({item["operation_id"] for item in first}), 2)
        for item in first:
            self.assertNotEqual(item["proposal_id"], item["operation_id"])

    def test_duplicate_nonself_facts_are_rejected_before_index_derived_keys(self) -> None:
        output = make_provider_output()
        output["facts"][0].update(
            {
                "subject": {"kind": "person", "label": "Synthetic Alice"},
                "predicate": "entity.attribute",
            }
        )
        output["facts"].append(copy.deepcopy(output["facts"][0]))
        with self.assertRaises(ContractViolation):
            validate_provider_result(self.request(), output)

    def test_nonself_entities_receive_opaque_source_local_keys(self) -> None:
        output = make_provider_output()
        output["facts"][0].update(
            {
                "subject": {"kind": "person", "label": "Synthetic Alice"},
                "predicate": "entity.attribute",
            }
        )
        proposal = validate_provider_result(self.request(), output)["proposals"][0]
        self.assertEqual(proposal["subject_display_name"], "Synthetic Alice")
        self.assertRegex(proposal["subject_entity_key"], r"^local:[0-9a-f]{64}$")
        self.assertNotIn("alice", proposal["subject_entity_key"].lower())

        entity_output = make_provider_output()
        entity_output["facts"][0].update(
            {
                "predicate": "relationship.kind",
                "object": {
                    "kind": "entity",
                    "entity_type": "person",
                    "label": "Synthetic Bob",
                },
                "sensitivity": "sensitive_third_party",
            }
        )
        entity_proposal = validate_provider_result(
            self.request(), entity_output
        )["proposals"][0]
        self.assertEqual(entity_proposal["subject_entity_key"], "self")
        self.assertEqual(entity_proposal["object_display_name"], "Synthetic Bob")
        self.assertRegex(
            entity_proposal["object_entity_key"], r"^local:[0-9a-f]{64}$"
        )
        self.assertNotIn("bob", entity_proposal["object_entity_key"].lower())

    def test_entity_display_names_have_one_exact_256_utf8_byte_bound(self) -> None:
        accepted_label = "é" * 128
        rejected_label = accepted_label + "é"

        subject = make_provider_output()
        subject["facts"][0].update(
            {
                "subject": {"kind": "person", "label": accepted_label},
                "predicate": "entity.attribute",
            }
        )
        accepted = validate_provider_result(self.request(), subject)["proposals"][0]
        self.assertEqual(accepted["subject_display_name"], accepted_label)
        subject["facts"][0]["subject"]["label"] = rejected_label
        with self.assertRaises(ContractViolation):
            validate_provider_result(self.request(), subject)

        object_value = make_provider_output()
        object_value["facts"][0].update(
            {
                "predicate": "relationship.kind",
                "object": {
                    "kind": "entity",
                    "entity_type": "person",
                    "label": accepted_label,
                },
                "sensitivity": "sensitive_third_party",
            }
        )
        accepted = validate_provider_result(
            self.request(), object_value
        )["proposals"][0]
        self.assertEqual(accepted["object_display_name"], accepted_label)
        object_value["facts"][0]["object"]["label"] = rejected_label
        with self.assertRaises(ContractViolation):
            validate_provider_result(self.request(), object_value)

    def test_source_local_entity_key_fixed_vector_excludes_display_text(self) -> None:
        vector = ENTITY_KEY_TEST_VECTOR
        self.assertEqual(vector["domain"], "governed_memory.source_local_entity.v1")
        self.assertEqual(
            vector["ordered_fields"],
            (
                ("owner_user_id", "00000000-0000-0000-0000-000000000001"),
                (
                    "selection_binding_sha256",
                    "20057004136f64e54333d53d4e46c85d85eba494c3140596f197572b0d47b619",
                ),
                ("fact_index", "0"),
                ("role", "object"),
                ("entity_type", "person"),
            ),
        )
        material = bytearray((vector["domain"] + "\n").encode("utf-8"))
        for field, value in vector["ordered_fields"]:
            encoded = value.encode("utf-8")
            material.extend(f"{field}:{len(encoded)}:".encode("utf-8"))
            material.extend(encoded)
            material.extend(b"\n")
        expected_digest = (
            "fd84fb3d7dc64f134e0c7b42df0307be5cc87693a9e58649c94f04cadb253e82"
        )
        self.assertEqual(sha256(material).hexdigest(), expected_digest)
        self.assertEqual(vector["sha256"], expected_digest)
        self.assertEqual(vector["entity_key"], f"local:{expected_digest}")
        self.assertEqual(vector["irrelevant_display_name"], "Zoë:\n猫")
        self.assertNotIn(
            "display",
            tuple(inspect.signature(derive_source_local_entity_key).parameters),
        )
        self.assertEqual(
            derive_source_local_entity_key(
                owner_user_id=UUID(vector["ordered_fields"][0][1]),
                selection_binding=vector["ordered_fields"][1][1],
                fact_index=0,
                role="object",
                entity_type="person",
            ),
            vector["entity_key"],
        )

    def test_self_entity_key_is_exact_and_never_source_local(self) -> None:
        vector = ENTITY_KEY_TEST_VECTOR
        for role in ("subject", "object"):
            with self.subTest(role=role):
                self.assertEqual(
                    derive_source_local_entity_key(
                        owner_user_id=OWNER_A,
                        selection_binding=vector["ordered_fields"][1][1],
                        fact_index=0,
                        role=role,
                        entity_type="self",
                    ),
                    "self",
                )

    def test_self_display_is_exactly_null_and_cannot_be_provider_supplied(self) -> None:
        proposal = validate_provider_result(
            self.request(), make_provider_output()
        )["proposals"][0]
        self.assertEqual(proposal["subject_entity_type"], "self")
        self.assertEqual(proposal["subject_entity_key"], "self")
        self.assertIsNone(proposal["subject_display_name"])

        forged = make_provider_output()
        forged["facts"][0]["subject"] = {
            "kind": "self",
            "label": "self",
        }
        with self.assertRaises(ContractViolation):
            validate_provider_result(self.request(), forged)

    def test_restricted_identifier_cannot_enter_provider_catalog(self) -> None:
        catalog = copy.deepcopy(PREDICATE_CATALOG)
        catalog["predicates"]["preference.personal"]["sensitivities"] = [
            "restricted_identifier"
        ]
        with self.assertRaises(ContractViolation):
            build_provider_request(
                make_extraction_job(),
                make_selected_evidence(),
                MODEL,
                SCHEMA,
                catalog,
            )

    def test_restricted_identifier_policy_and_correction_fail_before_sql(self) -> None:
        with self.assertRaises(ContractViolation):
            policy_for_sensitivity("restricted_identifier")

        correction = PROPOSAL_TEST_VECTORS["correction"]
        item = dict(correction["item"])
        item["sensitivity"] = "restricted_identifier"
        with self.assertRaises(ContractViolation):
            recompute_proposal_sha256(item, dict(correction["binding"]))

    def test_zero_facts_complete_with_a_persistable_content_free_receipt(self) -> None:
        output = {
            "schema": SCHEMA,
            "facts": [],
            "usage": {"input_tokens": 128, "output_tokens": 0},
        }
        batch = validate_provider_result(
            self.request(),
            output,
        )
        self.assertEqual(batch["proposals"], ())
        self.assertEqual(
            tuple(sorted(batch["proposal_hash_binding"])),
            tuple(sorted(PROPOSAL_HASH_BINDING_FIELDS)),
        )
        self.assertEqual(batch["receipt"]["proposal_count"], 0)
        self.assertEqual(batch["receipt"]["proposal_ids"], [])
        self.assertEqual(batch["receipt"]["operation_ids"], [])
        self.assertEqual(batch["receipt"]["proposal_sha256s"], [])
        self.assertEqual(batch["receipt"]["provider_outcome"], "completed")
        self.assertEqual(batch["receipt"]["external_model_calls"], 1)
        self.assertEqual(batch["receipt"]["input_tokens"], 128)
        self.assertEqual(batch["receipt"]["output_tokens"], 0)
        self.assertEqual(
            batch["response_sha256"],
            canonical_sha256("governed_memory.provider_response", output),
        )
        self.assertRegex(batch["receipt"]["receipt_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(
            {"claim", "revision", "projection", "outbox", "vector_point"}.isdisjoint(
                batch
            )
        )

    def test_fact_array_is_bounded_and_validated_all_or_nothing(self) -> None:
        output = make_provider_output()
        output["facts"] = make_provider_output()["facts"] * 9
        with self.assertRaises(ContractViolation):
            validate_provider_result(self.request(), output)

        output = make_provider_output()
        valid_fact = copy.deepcopy(output["facts"][0])
        invalid_fact = copy.deepcopy(valid_fact)
        invalid_fact["predicate"] = "preference.not_in_catalog"
        output["facts"] = [valid_fact, invalid_fact]
        validated_batch = None
        with self.assertRaises(ContractViolation):
            validated_batch = validate_provider_result(self.request(), output)
        self.assertIsNone(
            validated_batch,
            "a partially validated proposal batch escaped the all-or-nothing boundary",
        )

    def test_validation_does_not_mutate_request_or_output(self) -> None:
        request = self.request()
        output = make_provider_output()
        request_before = copy.deepcopy(request)
        output_before = copy.deepcopy(output)
        validate_provider_result(request, output)
        self.assertEqual(request, request_before)
        self.assertEqual(output, output_before)

    def test_content_free_receipt_excludes_selected_text(self) -> None:
        batch = validate_provider_result(self.request(), make_provider_output())
        receipt = batch["receipt"]
        serialized = json.dumps(receipt, sort_keys=True)
        self.assertNotIn(SOURCE_TEXT, serialized)
        self.assertNotIn("selected_text", receipt)
        self.assertRegex(receipt["request_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(receipt["response_sha256"], r"^[0-9a-f]{64}$")

    def test_usage_is_closed_bounded_and_exactly_bound_into_the_receipt(self) -> None:
        output = make_provider_output()
        batch = validate_provider_result(self.request(), output)
        receipt = batch["receipt"]
        self.assertEqual(receipt["input_tokens"], 128)
        self.assertEqual(receipt["output_tokens"], 16)
        receipt_material = dict(receipt)
        observed_receipt_sha256 = receipt_material.pop("receipt_sha256")
        self.assertEqual(
            observed_receipt_sha256,
            canonical_sha256(
                "governed_memory.provider_completion_receipt",
                receipt_material,
            ),
        )

        malformed_values = (
            {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            {"input_tokens": -1, "output_tokens": 0},
            {"input_tokens": 100_001, "output_tokens": 0},
            {"input_tokens": 1, "output_tokens": -1},
            {"input_tokens": 1, "output_tokens": 4_097},
            {"input_tokens": True, "output_tokens": 1},
            {"input_tokens": 1, "output_tokens": False},
        )
        for usage in malformed_values:
            malformed = make_provider_output()
            malformed["usage"] = usage
            with self.subTest(usage=usage), self.assertRaises(ContractViolation):
                validate_provider_result(self.request(), malformed)


if __name__ == "__main__":
    unittest.main()
