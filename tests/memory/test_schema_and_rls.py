"""Independent static SQL checks.

These tests inspect candidate bytes only. They do not connect to PostgreSQL and
must never be described as catalog, RLS-execution, rollback, or migration proof.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import inspect
import json
from pathlib import Path
import re
import runpy
import unittest
from uuid import UUID

from rag_engine.governed_memory import contracts
from rag_engine.governed_memory import worker as worker_core
from rag_engine.governed_memory.admission import (
    CLAIM_HASH_TEST_VECTORS,
    CLAIM_STATE_HASH_FIELDS,
    REVISION_HASH_FIELDS,
    recompute_claim_state_sha256,
    recompute_revision_sha256,
)
from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.extraction import (
    COMPLETE_EXTRACTION_ITEM_FIELDS,
    ENTITY_KEY_TEST_VECTOR,
    PROPOSAL_HASH_BINDING_FIELDS,
    PROPOSAL_TEST_VECTORS,
    derive_source_local_entity_key,
    recompute_proposal_sha256,
)
from rag_engine.governed_memory.retrieval import (
    ANSWER_BINDING_FIELDS,
    ANSWER_INJECTION_MANIFEST_FIELDS,
    ANSWER_MANIFEST_TEST_VECTORS,
    ANSWER_RECORD_LIMIT,
    ANSWER_SELECTION_MANIFEST_FIELDS,
    FINAL_ANSWER_BINDING_OUTCOMES,
    MEMORY_BLOCK_MAX_UTF8_BYTES,
    OUTBOUND_REQUEST_MAX_UTF8_BYTES,
    RETRIEVAL_POLICY_DOMAIN,
    answer_injection_manifest_sha256,
    answer_selection_manifest_sha256,
    retrieval_policy_material_bytes,
    retrieval_policy_sha256,
)
from rag_engine.governed_memory.worker import (
    NOT_EXECUTED_REASON_CODES,
    TERMINAL_PROVIDER_REASON_CODES,
    TERMINAL_REPLAY_CONTRACT,
    UNKNOWN_OUTCOME_REASON_CODES,
)


FIXTURE_PROVENANCE = "synthetic-governed-memory-successor"
ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "governed-memory-migrations"
CONTRACT_PATH = MIGRATIONS / "schema_contract.json"
ROOT_MANIFEST_PATH = MIGRATIONS / "manifest.json"
MANIFEST_VERIFIER_PATH = (
    ROOT / "tools" / "governed_memory_validation" / "verify_migration_manifest.py"
)
STORE_MANIFEST_VERIFIER_PATH = (
    ROOT
    / "tools"
    / "governed_memory_validation"
    / "verify_store_migration_manifest.py"
)
ROLES_PATH = MIGRATIONS / "roles_preflight.pgsql"
PACKAGE_DIRS = (
    MIGRATIONS / "0001_foundation",
    MIGRATIONS / "0002_conversation_bridge",
)
ADDITIVE_PACKAGE_DIRS = (
    MIGRATIONS / "0003_owner_claim_detail",
    MIGRATIONS / "0004_pilot_marker",
    MIGRATIONS / "0005_bounded_auto_admission",
)
ALL_PACKAGE_DIRS = (
    PACKAGE_DIRS[0],
    ADDITIVE_PACKAGE_DIRS[0],
    ADDITIVE_PACKAGE_DIRS[1],
    ADDITIVE_PACKAGE_DIRS[2],
    PACKAGE_DIRS[1],
)
FOUNDATION_TABLES = (
    "predicate_catalog",
    "evidence",
    "extraction_job",
    "provider_call",
    "proposal",
    "entity",
    "claim",
    "claim_revision",
    "claim_evidence",
    "projection_outbox",
    "answer_binding",
    "audit_event",
    "erased_chat_message_tombstone",
    "claim_deletion_receipt",
    "source_erasure_operation",
    "source_erasure_target",
    "source_erasure_claim",
    "source_erasure_receipt",
)

SELECTION_NO_CONTEXT_SHA256 = (
    "20057004136f64e54333d53d4e46c85d85eba494c3140596f197572b0d47b619"
)
SELECTION_WITH_CONTEXT_SHA256 = (
    "88c6ea7d5e4aa020ca5f5acca6586e2b2dfe260c4d0b693ad06f27004ab2d860"
)
SEMANTIC_LITERAL_SHA256 = (
    "3395c6bd0e42b2fc8340a5fcb0ce437b11d97d1811b32c544574ea8b0c360fdc"
)
SEMANTIC_ENTITY_SHA256 = (
    "0717334b30e69d2d6f0aeb5b2d057c9a038d75f137108440083aabfb5e582149"
)
BRIDGE_SOURCE_SHA256 = contracts.BRIDGE_SOURCE_HASH_TEST_VECTOR["sha256"]
BRIDGE_SOURCE_FIELD_NAMES = contracts.BRIDGE_SOURCE_BINDING_FIELDS
BRIDGE_LEASE_FIELDS = (
    "outbox_id",
    "owner_user_id",
    "message_id",
    "thread_id",
    "exchange_id",
    "window_id",
    "window_ordinal",
    "window_sha256",
    "content_sha256",
    "source_binding_sha256",
    "policy_sha256",
    "source_created_at",
    "ingest_after",
    "context_review_count",
    "eligibility_decision",
    "lease_token",
    "lease_expires_at",
)
INGEST_SUCCESSOR_RECEIPT_SHA256 = (
    "89a5a4c63e334181cd637dcadf9b72033c7f1afe7810fcd9d7e82a03ee79a742"
)
INGEST_SUCCESSOR_RECEIPT_FIELDS = (
    "owner_user_id",
    "operation_id",
    "bridge_source_binding_sha256",
    "decision",
    "evidence_id",
    "extraction_job_id",
)

SELECTION_FIELD_NAMES = (
    "owner_user_id",
    "source_kind",
    "source_message_id",
    "source_thread_id",
    "source_window_id",
    "window_sha256",
    "source_sha256",
    "selected_sha256",
    "start_utf8",
    "end_utf8",
    "context_message_id",
    "context_sha256",
)
SELECTION_SQL_PARAMETER_NAMES = (
    "owner_user_id",
    "source_kind",
    "source_message_id",
    "source_thread_id",
    "source_window_id",
    "source_window_sha256",
    "source_sha256",
    "selected_sha256",
    "selected_start_utf8",
    "selected_end_utf8",
    "context_message_id",
    "context_sha256",
)
SEMANTIC_FIELD_NAMES = (
    "subject_entity_key",
    "predicate",
    "object_kind",
    "object_entity_key",
    "object_literal",
)
ENTITY_KEY_SQL_PARAMETER_NAMES = (
    ("owner_user_id", "uuid"),
    ("selection_binding_sha256", "text"),
    ("slot_index", "integer"),
    ("role", "text"),
    ("entity_type", "text"),
)
PROPOSAL_HASH_TRAILING_FIELDS = (
    "fact_index",
    "proposal_id",
    "operation_id",
    "subject_entity_type",
    "subject_entity_key",
    "subject_display_name",
    "predicate",
    "object_kind",
    "object_entity_type",
    "object_entity_key",
    "object_display_name",
    "object_literal",
    "epistemic_state",
    "sensitivity",
    "semantic_key_sha256",
    "projectable",
    "domains",
    "intents",
    "surface",
    "requires_explicit",
    "valid_from",
    "valid_to",
)
EXPECTED_PROPOSAL_HASH_BINDING_FIELDS = (
    "owner_user_id",
    "job_id",
    "evidence_id",
    "provider_call_id",
    "source_kind",
    "source_message_id",
    "source_thread_id",
    "source_window_id",
    "window_sha256",
    "source_sha256",
    "selected_sha256",
    "selection_binding_sha256",
    "context_message_id",
    "context_sha256",
    "predicate_catalog_sha256",
    "request_sha256",
    "response_sha256",
    "purpose",
    "correction_target_claim_id",
    "correction_target_revision_id",
    "correction_target_revision_number",
    "correction_target_revision_sha256",
    "correction_target_state_sha256",
    "correction_target_identity_sha256",
    "correction_target_projection_sequence",
    "correction_pending_state_sha256",
)
EXPECTED_REVISION_HASH_FIELDS = (
    "owner_user_id",
    "claim_id",
    "revision_id",
    "revision_number",
    "proposal_id",
    "proposal_sha256",
    "semantic_key_sha256",
    "claim_identity_sha256",
    "subject_entity_type",
    "subject_entity_key",
    "subject_display_name",
    "predicate",
    "object_kind",
    "object_entity_type",
    "object_entity_key",
    "object_display_name",
    "object_literal",
    "epistemic_state",
    "sensitivity",
    "projectable",
    "domains",
    "intents",
    "surface",
    "requires_explicit",
    "valid_from",
    "valid_to",
    "source_sha256",
    "selected_sha256",
    "selection_binding_sha256",
    "predicate_catalog_sha256",
    "retrieval_text_sha256",
)
EXPECTED_CLAIM_STATE_HASH_FIELDS = (
    "owner_user_id",
    "claim_id",
    "semantic_key_sha256",
    "claim_identity_sha256",
    "lifecycle_state",
    "is_current",
    "revision_id",
    "revision_number",
    "revision_sha256",
    "projection_sequence",
)


def _load_json(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    if len(raw) > 512 * 1024:
        raise AssertionError(f"oversized JSON contract: {path}")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"JSON contract is not an object: {path}")
    return value


def _sql(path: Path) -> str:
    raw = path.read_bytes()
    if b"\r" in raw or b"\x00" in raw:
        raise AssertionError(f"noncanonical SQL bytes: {path}")
    return raw.decode("utf-8")


def _without_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", sql)


def _file_binding(package: dict[str, object], kind: str) -> tuple[str, str]:
    section = package.get(kind)
    if kind == "rollback" and section is None:
        section = package.get("recovery")
    if not isinstance(section, dict):
        raise AssertionError(f"package lacks {kind} binding")
    path = section.get("path")
    digest = section.get("sha256")
    if not isinstance(path, str) or not isinstance(digest, str):
        raise AssertionError(f"package has malformed {kind} binding")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise AssertionError(f"package has malformed {kind} SHA-256")
    return path, digest


def _function_definitions(sql: str) -> dict[str, str]:
    starts = list(
        re.finditer(
            r"\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+"
            r"((?:memory_private|memory_ingest_private)\.[a-z][a-z0-9_]*)\s*\(",
            sql,
            flags=re.IGNORECASE,
        )
    )
    definitions: dict[str, str] = {}
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(sql)
        name = match.group(1).lower()
        if name in definitions:
            raise AssertionError(f"duplicate function definition: {name}")
        definitions[name] = sql[match.start() : end]
    return definitions


def _framed_sha256(
    domain: str,
    fields: tuple[tuple[str, str | None], ...],
) -> str:
    """Independent test oracle for the cross-language UTF-8 framing."""

    material = bytearray(f"{domain}\n".encode("utf-8"))
    for name, value in fields:
        if value is None:
            material.extend(f"{name}:-:\n".encode("utf-8"))
            continue
        encoded = value.encode("utf-8")
        material.extend(f"{name}:{len(encoded)}:".encode("utf-8"))
        material.extend(encoded)
        material.extend(b"\n")
    return sha256(material).hexdigest()


def _hash_scalar(value: object) -> str | None:
    """Independent scalar encoding used only by frozen hash-vector tests."""

    if value is None:
        return None
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(str(item) for item in value) + "]"
    raise AssertionError(f"unsupported fixed-vector scalar: {type(value).__name__}")


def _sql_do_blocks(sql: str) -> list[str]:
    return re.findall(
        r"\bDO\s+\$(?:[a-z_][a-z0-9_]*)?\$(.*?)"
        r"\$(?:[a-z_][a-z0-9_]*)?\$\s*;",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _table_definition(sql: str, qualified_name: str) -> str:
    marker = f"CREATE TABLE {qualified_name} ("
    if marker not in sql:
        raise AssertionError(f"missing table definition: {qualified_name}")
    return sql.split(marker, 1)[1].split(");", 1)[0]


class CrossLanguageHashContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = _sql(PACKAGE_DIRS[0] / "forward.pgsql")
        cls.functions = _function_definitions(_without_comments(cls.sql))

    def test_selection_binding_fixed_vectors_match_python_and_sql(self) -> None:
        common = {
            "owner_user_id": UUID("00000000-0000-0000-0000-000000000001"),
            "source_kind": "conversation_message",
            "source_message_id": UUID("00000000-0000-0000-0000-000000000002"),
            "source_thread_id": UUID("00000000-0000-0000-0000-000000000003"),
            "source_window_id": UUID("00000000-0000-0000-0000-000000000004"),
            "window_sha256": "1" * 64,
            "source_sha256": "2" * 64,
            "selected_sha256": "3" * 64,
            "start_utf8": 7,
            "end_utf8": 19,
        }
        no_context_fields = tuple(
            (name, None if name.startswith("context_") else str(common[name]))
            for name in SELECTION_FIELD_NAMES
        )
        with_context = {
            **common,
            "context_message_id": UUID(
                "00000000-0000-0000-0000-000000000005"
            ),
            "context_sha256": "4" * 64,
        }
        with_context_fields = tuple(
            (name, str(with_context[name])) for name in SELECTION_FIELD_NAMES
        )
        self.assertEqual(
            _framed_sha256(
                "governed_memory.selection_binding.v1", no_context_fields
            ),
            SELECTION_NO_CONTEXT_SHA256,
        )
        self.assertEqual(
            _framed_sha256(
                "governed_memory.selection_binding.v1", with_context_fields
            ),
            SELECTION_WITH_CONTEXT_SHA256,
        )

        signature = inspect.signature(contracts.selection_binding_sha256)
        self.assertEqual(tuple(signature.parameters), SELECTION_FIELD_NAMES)
        self.assertEqual(
            contracts.selection_binding_sha256(
                **common,
                context_message_id=None,
                context_sha256=None,
            ),
            SELECTION_NO_CONTEXT_SHA256,
        )
        self.assertEqual(
            contracts.selection_binding_sha256(**with_context),
            SELECTION_WITH_CONTEXT_SHA256,
        )

        definition = self.functions["memory_private.selection_binding_sha256"]
        sql_signature = definition.split("RETURNS", 1)[0]
        self.assertEqual(
            tuple(
                name.removeprefix("p_")
                for name in re.findall(
                    r"\b(p_[a-z][a-z0-9_]*)\s+(?:uuid|text|integer)\b",
                    sql_signature,
                    flags=re.IGNORECASE,
                )
            ),
            SELECTION_SQL_PARAMETER_NAMES,
        )
        self.assertIn("governed_memory.selection_binding.v1", definition)
        self.assertNotIn("governed_memory.selection_binding.v1|", definition)
        executable_checks = "\n".join(
            block
            for block in _sql_do_blocks(self.sql)
            if "RAISE EXCEPTION" in block.upper()
        )
        for digest in (
            SELECTION_NO_CONTEXT_SHA256,
            SELECTION_WITH_CONTEXT_SHA256,
        ):
            with self.subTest(digest=digest):
                self.assertIn(digest, executable_checks)

    def test_bridge_source_fixed_vector_and_lease_wire_match_python_and_sql(self) -> None:
        helper = contracts.bridge_source_binding_sha256
        self.assertTrue(callable(helper), "bridge_source_binding_sha256 is absent")
        self.assertEqual(
            tuple(inspect.signature(helper).parameters),
            BRIDGE_SOURCE_FIELD_NAMES,
        )
        vector_material = dict(
            contracts.BRIDGE_SOURCE_HASH_TEST_VECTOR["material"]
        )
        values = {
            **vector_material,
            **{
                name: UUID(vector_material[name])
                for name in (
                    "owner_user_id",
                    "message_id",
                    "thread_id",
                    "exchange_id",
                    "window_id",
                )
            },
            "source_created_at": datetime.fromisoformat(
                vector_material["source_created_at"].replace("Z", "+00:00")
            ),
        }
        fields = tuple(
            (name, str(vector_material[name])) for name in BRIDGE_SOURCE_FIELD_NAMES
        )
        self.assertEqual(
            _framed_sha256("governed_memory.bridge_source.v1", fields),
            BRIDGE_SOURCE_SHA256,
        )
        self.assertEqual(helper(**values), BRIDGE_SOURCE_SHA256)

        bridge = _without_comments(_sql(PACKAGE_DIRS[1] / "forward.pgsql"))
        definitions = _function_definitions(bridge)
        definition = definitions["memory_ingest_private.source_binding_sha256"]
        signature = definition.split("RETURNS", 1)[0]
        self.assertEqual(
            tuple(
                name.removeprefix("p_")
                for name in re.findall(
                    r"\b(p_[a-z][a-z0-9_]*)\s+"
                    r"(?:uuid|text|integer|timestamptz)\b",
                    signature,
                    flags=re.IGNORECASE,
                )
            ),
            BRIDGE_SOURCE_FIELD_NAMES,
        )
        self.assertEqual(
            tuple(
                re.findall(
                    r"framed_utf8_field\(\s*'([a-z][a-z0-9_]*)'",
                    definition,
                    flags=re.IGNORECASE,
                )
            ),
            BRIDGE_SOURCE_FIELD_NAMES,
        )
        self.assertIn("governed_memory.bridge_source.v1", definition)
        executable_checks = "\n".join(
            block
            for block in _sql_do_blocks(bridge)
            if "RAISE EXCEPTION" in block.upper()
        )
        self.assertIn(BRIDGE_SOURCE_SHA256, executable_checks)

        for function_name, expected_fields in (
            ("memory_ingest_private.lease_memory_ingest", BRIDGE_LEASE_FIELDS),
            (
                "memory_ingest_private.read_leased_chat_log_message",
                BRIDGE_LEASE_FIELDS + ("role", "content"),
            ),
        ):
            returns = definitions[function_name].split("RETURNS TABLE(", 1)[1].split(
                ")", 1
            )[0]
            observed = tuple(
                re.findall(
                    r"\b([a-z][a-z0-9_]*)\s+"
                    r"(?:uuid|text|integer|timestamptz)\b",
                    returns,
                    flags=re.IGNORECASE,
                )
            )
            with self.subTest(function=function_name):
                self.assertEqual(observed, expected_fields)
                self.assertRegex(
                    definitions[function_name],
                    r"(?:state\s*=|target\.state\s*<>)\s*'claimed'",
                )
                self.assertIn("lease_token", definitions[function_name])
                self.assertIn("lease_expires_at", definitions[function_name])

    def test_ingest_successor_receipt_fixed_vector_and_lookup_are_exact(self) -> None:
        helper = getattr(worker_core, "ingest_successor_receipt_sha256", None)
        self.assertTrue(callable(helper), "ingest successor receipt hash is absent")
        self.assertEqual(
            tuple(inspect.signature(helper).parameters),
            INGEST_SUCCESSOR_RECEIPT_FIELDS,
        )
        values = {
            "owner_user_id": UUID("00000000-0000-0000-0000-000000000001"),
            "operation_id": UUID("00000000-0000-0000-0000-000000000002"),
            "bridge_source_binding_sha256": "1" * 64,
            "decision": "send_external",
            "evidence_id": UUID("00000000-0000-0000-0000-000000000003"),
            "extraction_job_id": UUID("00000000-0000-0000-0000-000000000004"),
        }
        fields = tuple(
            (name, str(values[name])) for name in INGEST_SUCCESSOR_RECEIPT_FIELDS
        )
        self.assertEqual(
            _framed_sha256(
                "governed_memory.ingest_successor_receipt.v1", fields
            ),
            INGEST_SUCCESSOR_RECEIPT_SHA256,
        )
        self.assertEqual(helper(**values), INGEST_SUCCESSOR_RECEIPT_SHA256)

        definitions = _function_definitions(self.sql)
        digest = definitions["memory_private.ingest_successor_receipt_sha256"]
        signature = digest.split("RETURNS", 1)[0]
        self.assertEqual(
            tuple(
                name.removeprefix("p_")
                for name in re.findall(
                    r"\b(p_[a-z][a-z0-9_]*)\s+(?:uuid|text)\b",
                    signature,
                    flags=re.IGNORECASE,
                )
            ),
            INGEST_SUCCESSOR_RECEIPT_FIELDS,
        )
        self.assertEqual(
            tuple(
                re.findall(
                    r"framed_utf8_field\(\s*'([a-z][a-z0-9_]*)'",
                    digest,
                    flags=re.IGNORECASE,
                )
            ),
            INGEST_SUCCESSOR_RECEIPT_FIELDS,
        )
        self.assertIn("governed_memory.ingest_successor_receipt.v1", digest)
        self.assertIn(INGEST_SUCCESSOR_RECEIPT_SHA256, self.sql)

        evidence = _table_definition(self.sql, "memory.evidence")
        self.assertRegex(
            evidence,
            r"bridge_source_binding_sha256\s+text\b",
        )
        self.assertRegex(
            evidence,
            r"ingest_receipt_sha256\s+text\b",
        )
        record = definitions["memory_private.record_selected_evidence"]
        self.assertIn("p_bridge_source_binding_sha256", record)
        self.assertIn("bridge_source_binding_sha256", record)
        self.assertIn("ingest_receipt_sha256", record)
        for required_hash in (
            "p_bridge_source_binding_sha256",
            "p_window_sha256",
            "p_source_sha256",
            "p_policy_sha256",
        ):
            with self.subTest(required_evidence_hash=required_hash):
                self.assertRegex(record, rf"{required_hash}\s+IS\s+NULL")
                self.assertRegex(
                    record,
                    rf"{required_hash}\s*!~\s*'\^\[0-9a-f\]\{{64\}}\$'",
                )
        read = definitions["memory_private.read_ingest_receipt"]
        for required in (
            "p_owner_user_id",
            "p_operation_id",
            "p_bridge_source_binding_sha256",
            "evidence_id",
            "extraction_job_id",
            "ingest_receipt_sha256",
        ):
            with self.subTest(required=required):
                self.assertIn(required, read)
        self.assertRegex(read, r"session_user\s*<>\s*'governed_memory_worker'")
        self.assertRegex(read, r"decision\s*=\s*'send_external'")

    def test_answer_manifest_fixed_vectors_match_python_and_sql(self) -> None:
        cases = (
            (
                "selection",
                answer_selection_manifest_sha256,
                "memory_private.answer_selection_manifest_sha256",
                ANSWER_SELECTION_MANIFEST_FIELDS,
            ),
            (
                "injection",
                answer_injection_manifest_sha256,
                "memory_private.answer_injection_manifest_sha256",
                ANSWER_INJECTION_MANIFEST_FIELDS,
            ),
        )
        executable_checks = "\n".join(
            block
            for block in _sql_do_blocks(self.sql)
            if "RAISE EXCEPTION" in block.upper()
        )
        for vector_name, python_helper, sql_name, expected_fields in cases:
            vector = ANSWER_MANIFEST_TEST_VECTORS[vector_name]
            definition = self.functions[sql_name]
            signature = definition.split("RETURNS", 1)[0]
            with self.subTest(vector=vector_name):
                self.assertEqual(tuple(vector["ordered_fields"]), expected_fields)
                self.assertEqual(
                    python_helper(**vector["arguments"]), vector["sha256"]
                )
                self.assertEqual(
                    tuple(
                        name.removeprefix("p_")
                        for name in re.findall(
                            r"\b(p_[a-z][a-z0-9_]*)\s+"
                            r"(?:uuid\[\]|text\[\]|uuid|text|integer|boolean)\b",
                            signature,
                            flags=re.IGNORECASE,
                        )
                    ),
                    expected_fields,
                )
                self.assertEqual(
                    tuple(
                        re.findall(
                            r"framed_utf8_field\(\s*'([a-z][a-z0-9_]*)'",
                            definition,
                            flags=re.IGNORECASE,
                        )
                    ),
                    expected_fields,
                )
                self.assertIn(vector["domain"], definition)
                self.assertIn(vector["sha256"], executable_checks)

    def test_semantic_key_literal_and_entity_vectors_match_python_and_sql(self) -> None:
        helper = getattr(contracts, "semantic_key_sha256", None)
        self.assertTrue(callable(helper), "semantic_key_sha256 is not exported")
        self.assertEqual(tuple(inspect.signature(helper).parameters), SEMANTIC_FIELD_NAMES)

        literal = {
            "subject_entity_key": "self",
            "predicate": "preference.personal",
            "object_kind": "literal",
            "object_entity_key": None,
            "object_literal": "caf\u00e9:\n\u732b",
        }
        entity = {
            "subject_entity_key": "person:alice",
            "predicate": "relationship.kind",
            "object_kind": "entity",
            "object_entity_key": "person:bob",
            "object_literal": None,
        }
        for value, expected in (
            (literal, SEMANTIC_LITERAL_SHA256),
            (entity, SEMANTIC_ENTITY_SHA256),
        ):
            fields = tuple((name, value[name]) for name in SEMANTIC_FIELD_NAMES)
            with self.subTest(object_kind=value["object_kind"]):
                self.assertEqual(
                    _framed_sha256("governed_memory.semantic_key.v1", fields),
                    expected,
                )
                self.assertEqual(helper(**value), expected)

        definition = self.functions["memory_private.semantic_key_sha256"]
        sql_signature = definition.split("RETURNS", 1)[0]
        self.assertEqual(
            tuple(
                name.removeprefix("p_")
                for name in re.findall(
                    r"\b(p_[a-z][a-z0-9_]*)\s+(?:text|jsonb)\b",
                    sql_signature,
                    flags=re.IGNORECASE,
                )
            ),
            SEMANTIC_FIELD_NAMES,
        )
        self.assertIn("governed_memory.semantic_key.v1", definition)
        self.assertNotIn("governed_memory.semantic_identity", definition)
        self.assertNotIn("p_object_literal::text", definition)
        executable_checks = "\n".join(
            block
            for block in _sql_do_blocks(self.sql)
            if "RAISE EXCEPTION" in block.upper()
        )
        for digest in (SEMANTIC_LITERAL_SHA256, SEMANTIC_ENTITY_SHA256):
            with self.subTest(digest=digest):
                self.assertIn(digest, executable_checks)

    def test_semantic_key_requires_exactly_one_closed_object_value(self) -> None:
        helper = getattr(contracts, "semantic_key_sha256", None)
        self.assertTrue(callable(helper), "semantic_key_sha256 is not exported")
        common = {
            "subject_entity_key": "self",
            "predicate": "preference.personal",
        }
        malformed = (
            {
                **common,
                "object_kind": "literal",
                "object_entity_key": "person:bob",
                "object_literal": "cobalt",
            },
            {
                **common,
                "object_kind": "literal",
                "object_entity_key": None,
                "object_literal": None,
            },
            {
                **common,
                "object_kind": "entity",
                "object_entity_key": "person:bob",
                "object_literal": "cobalt",
            },
            {
                **common,
                "object_kind": "entity",
                "object_entity_key": None,
                "object_literal": None,
            },
        )
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(ContractViolation):
                helper(**value)

    def test_source_local_entity_key_fixed_vector_matches_python_and_sql(self) -> None:
        vector = ENTITY_KEY_TEST_VECTOR
        expected_digest = (
            "fd84fb3d7dc64f134e0c7b42df0307be5cc87693a9e58649c94f04cadb253e82"
        )
        expected_key = f"local:{expected_digest}"
        self.assertEqual(tuple(vector["ordered_fields"]), (
            ("owner_user_id", "00000000-0000-0000-0000-000000000001"),
            (
                "selection_binding_sha256",
                "20057004136f64e54333d53d4e46c85d85eba494c3140596f197572b0d47b619",
            ),
            ("fact_index", "0"),
            ("role", "object"),
            ("entity_type", "person"),
        ))
        self.assertEqual(
            _framed_sha256(vector["domain"], tuple(vector["ordered_fields"])),
            expected_digest,
        )
        self.assertEqual(vector["sha256"], expected_digest)
        self.assertEqual(vector["entity_key"], expected_key)
        self.assertEqual(vector["irrelevant_display_name"], "Zoë:\n猫")
        self.assertEqual(
            derive_source_local_entity_key(
                owner_user_id=UUID(vector["ordered_fields"][0][1]),
                selection_binding=vector["ordered_fields"][1][1],
                fact_index=0,
                role="object",
                entity_type="person",
            ),
            expected_key,
        )

        definition = self.functions["memory_private.source_local_entity_key"]
        signature = definition.split("RETURNS", 1)[0]
        self.assertEqual(
            tuple(
                (name.removeprefix("p_"), sql_type.lower())
                for name, sql_type in re.findall(
                    r"\b(p_[a-z][a-z0-9_]*)\s+(uuid|text|integer)\b",
                    signature,
                    flags=re.IGNORECASE,
                )
            ),
            ENTITY_KEY_SQL_PARAMETER_NAMES,
        )
        self.assertEqual(
            tuple(
                re.findall(
                    r"framed_utf8_field\(\s*'([a-z][a-z0-9_]*)'",
                    definition,
                    flags=re.IGNORECASE,
                )
            ),
            tuple(name for name, _ in vector["ordered_fields"]),
        )
        self.assertIn(vector["domain"], definition)
        self.assertRegex(definition, r"'local:'\s*\|\|")
        self.assertRegex(definition, r"p_slot_index\s+NOT\s+BETWEEN\s+0\s+AND\s+7")
        for closed_value in ("subject", "object", "organization", "other", "person", "pet", "place"):
            with self.subTest(closed_value=closed_value):
                self.assertIn(f"'{closed_value}'", definition)
        self.assertRegex(
            definition,
            r"WHEN\s+p_entity_type\s*=\s*'self'\s+THEN\s+'self'",
        )
        self.assertNotIn("display", definition.lower())

        executable_checks = "\n".join(
            block
            for block in _sql_do_blocks(self.sql)
            if "RAISE EXCEPTION" in block.upper()
        )
        self.assertIn(expected_digest, executable_checks)
        self.assertIn(expected_key, executable_checks)

        complete = self.functions["memory_private.complete_extraction"]
        self.assertGreaterEqual(
            complete.count("memory_private.source_local_entity_key("), 2
        )
        for field, role in (
            ("subject_entity_key", "subject"),
            ("object_entity_key", "object"),
        ):
            with self.subTest(complete_field=field):
                self.assertRegex(
                    complete,
                    rf"(?s)item->>'{field}'.{{0,150}}IS\s+DISTINCT\s+FROM"
                    r"\s+memory_private\.source_local_entity_key\(.{0,300}"
                    rf"'{role}'",
                )

        correct = self.functions["memory_private.correct_claim"]
        replacement_key_list = re.search(
            r"jsonb_object_keys\(p_replacement\).*?<>\s*ARRAY\[(.*?)\]::text\[\]",
            correct,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(replacement_key_list)
        self.assertNotIn("object_entity_key", replacement_key_list.group(1))
        self.assertRegex(
            correct,
            r"(?s)replacement_object_entity_key\s*:=\s*CASE.{0,400}"
            r"memory_private\.source_local_entity_key\(.{0,250}'object'",
        )
        self.assertRegex(
            correct,
            r"existing_proposal\.object_entity_key\s+IS\s+DISTINCT\s+FROM"
            r"\s+replacement_object_entity_key",
        )
        self.assertGreaterEqual(correct.count("replacement_object_entity_key"), 4)

    def test_proposal_revision_and_claim_state_hash_vectors_are_exact(self) -> None:
        self.assertEqual(
            PROPOSAL_HASH_BINDING_FIELDS,
            EXPECTED_PROPOSAL_HASH_BINDING_FIELDS,
        )
        self.assertEqual(REVISION_HASH_FIELDS, EXPECTED_REVISION_HASH_FIELDS)
        self.assertEqual(CLAIM_STATE_HASH_FIELDS, EXPECTED_CLAIM_STATE_HASH_FIELDS)
        self.assertIn("correction_pending_state_sha256", PROPOSAL_HASH_BINDING_FIELDS)
        self.assertIn("projectable", REVISION_HASH_FIELDS)

        expected_proposal_digests = {
            "new_claim": (
                "a8df61933716bb2809595922b6e24800ebfff064c9d667874ff511300ab4973c"
            ),
            "correction": (
                "f793cbc8773cfc39b11cd4867d277f3b5957037dc9ccd94b8aff431198d5960f"
            ),
        }
        for name, expected in expected_proposal_digests.items():
            vector = PROPOSAL_TEST_VECTORS[name]
            binding = vector["binding"]
            item = vector["item"]
            with self.subTest(vector=name):
                self.assertEqual(tuple(binding), PROPOSAL_HASH_BINDING_FIELDS)
                self.assertEqual(
                    tuple(sorted(item)),
                    COMPLETE_EXTRACTION_ITEM_FIELDS,
                )
                self.assertEqual(vector["sha256"], expected)
                self.assertEqual(
                    recompute_proposal_sha256(item, binding),
                    expected,
                )
                derived_policy = {
                    "projectable": True,
                    "domains": (),
                    "intents": (),
                    "surface": "normal",
                    "requires_explicit": False,
                    "valid_from": None,
                    "valid_to": None,
                }
                framed_fields = tuple(
                    (field, _hash_scalar(binding[field]))
                    for field in PROPOSAL_HASH_BINDING_FIELDS
                ) + tuple(
                    (
                        field,
                        _hash_scalar(
                            item[field] if field in item else derived_policy[field]
                        ),
                    )
                    for field in PROPOSAL_HASH_TRAILING_FIELDS
                )
                self.assertEqual(
                    _framed_sha256("governed_memory.proposal.v1", framed_fields),
                    expected,
                )

        new_binding = PROPOSAL_TEST_VECTORS["new_claim"]["binding"]
        correction_binding = PROPOSAL_TEST_VECTORS["correction"]["binding"]
        for field in PROPOSAL_HASH_BINDING_FIELDS:
            if field.startswith("correction_"):
                with self.subTest(new_claim_null_field=field):
                    self.assertIsNone(new_binding[field])
        self.assertEqual(correction_binding["correction_target_projection_sequence"], 4)
        self.assertRegex(
            correction_binding["correction_pending_state_sha256"],
            r"^[0-9a-f]{64}$",
        )

        vector_cases = (
            (
                "revision",
                REVISION_HASH_FIELDS,
                recompute_revision_sha256,
                "61f011a52dceb6beec78f788b3638047bfa576d534ecc233bf8b059965ddef54",
            ),
            (
                "claim_state",
                CLAIM_STATE_HASH_FIELDS,
                recompute_claim_state_sha256,
                "22444cef2c2c45950246c2d5e209386c0df41fe26793565e2967df85b898af68",
            ),
        )
        for name, fields, recompute, expected in vector_cases:
            vector = CLAIM_HASH_TEST_VECTORS[name]
            material = vector["material"]
            with self.subTest(vector=name):
                self.assertEqual(tuple(material), fields)
                self.assertEqual(vector["sha256"], expected)
                self.assertEqual(recompute(material), expected)
                self.assertEqual(
                    _framed_sha256(
                        vector["domain"],
                        tuple(
                            (field, _hash_scalar(material[field]))
                            for field in fields
                        ),
                    ),
                    expected,
                )

        sql_hash_functions = (
            (
                "memory_private.proposal_sha256",
                PROPOSAL_HASH_BINDING_FIELDS + PROPOSAL_HASH_TRAILING_FIELDS,
            ),
            ("memory_private.claim_revision_sha256", REVISION_HASH_FIELDS),
            ("memory_private.claim_state_sha256", CLAIM_STATE_HASH_FIELDS),
        )
        for function_name, expected_fields in sql_hash_functions:
            definition = self.functions[function_name]
            observed_fields = tuple(
                re.findall(
                    r"framed_utf8_field\(\s*'([a-z][a-z0-9_]*)'",
                    definition,
                    flags=re.IGNORECASE,
                )
            )
            with self.subTest(function=function_name):
                self.assertEqual(observed_fields, expected_fields)

        executable_checks = "\n".join(
            block
            for block in _sql_do_blocks(self.sql)
            if "RAISE EXCEPTION" in block.upper()
        )
        for digest in (
            *expected_proposal_digests.values(),
            vector_cases[0][3],
            vector_cases[1][3],
        ):
            with self.subTest(sql_vector=digest):
                self.assertIn(digest, executable_checks)


class SchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = _load_json(CONTRACT_PATH)

    def test_contract_declares_a_new_empty_authority(self) -> None:
        self.assertEqual(
            self.contract["schema_version"],
            "governed-memory-successor-schema-contract-v1",
        )
        self.assertEqual(
            self.contract["status"],
            "isolated_candidate_disposable_validated_not_production_applied",
        )
        self.assertEqual(self.contract["database"], "governed_memory")
        self.assertEqual(self.contract["schemas"], ["memory", "memory_private"])
        self.assertEqual(
            self.contract["conversation_database"],
            {
                "database": "memory",
                "schemas": ["memory_ingest_private", "chat_integrity"],
                "authority": "separate_existing_conversation_database_only",
            },
        )
        self.assertTrue(
            all(
                name.startswith("memory_private.")
                for name in self.contract["internal_functions"]
            )
        )
        self.assertEqual(
            self.contract["authority"],
            {
                "account_identity": "verified_supabase_user_and_session_uuid_supplied_by_backend",
                "session_authority": "staged_auth_sessions_rpc_not_installed_or_live_verified",
                "claim_truth": "postgresql",
                "vector_index": "derived_qdrant_only",
                "source_sha256_authority": "evidence_only_not_projection",
                "historical_backfill": False,
                "legacy_imports": False,
            },
        )
        self.assertEqual(
            self.contract["initialization"],
            {
                "postgresql": "new_empty_database",
                "qdrant": "new_empty_versioned_collection_with_stable_alias",
                "legacy_compatibility_state": False,
                "old_claims_cards_vectors_jobs_reviews": "disposable_not_imported",
            },
        )

    def test_contract_has_unique_closed_object_surfaces(self) -> None:
        for key in (
            "tables",
            "owner_bearing_tables",
            "function_surface",
            "internal_functions",
        ):
            values = self.contract[key]
            self.assertIsInstance(values, list)
            self.assertTrue(values)
            self.assertEqual(len(values), len(set(values)), key)
        self.assertEqual(
            set(self.contract["owner_bearing_tables"]),
            set(self.contract["tables"]) - {"predicate_catalog", "pilot_marker"},
        )
        marker = self.contract["pilot_marker"]
        self.assertEqual(marker["table"], "memory.pilot_marker")
        self.assertEqual(marker["owner_scope"], "global_content_free_not_owner_bearing")
        self.assertFalse(marker["owner_bearing"])
        self.assertFalse(marker["contains_owner_user_id"])
        self.assertTrue(marker["content_free"])
        self.assertTrue(marker["append_only"])
        bridge = self.contract["bridge"]
        self.assertEqual(bridge["table"], "memory_ingest_private.memory_ingest_outbox")
        self.assertEqual(bridge["private_schema"], "memory_ingest_private")
        self.assertFalse(bridge["contains_message_text"])
        self.assertFalse(bridge["contains_attachment_text"])
        self.assertTrue(bridge["assumes_conversation_table_shape"])
        self.assertEqual(bridge["database"], "memory")
        self.assertEqual(
            bridge["capture_application_login_role"], "brains_app"
        )
        self.assertEqual(
            bridge["source_erasure_application_login_role"],
            "governed_memory_api",
        )
        self.assertEqual(bridge["source_table"], "public.chat_log")
        self.assertEqual(
            bridge["source_row_capture"],
            "same_transaction_xmin_and_timestamp_exact_match",
        )
        self.assertEqual(bridge["source_reader"], "exact_claimed_unexpired_lease_token")
        self.assertEqual(bridge["source_value"], "frontend/chat:user")
        self.assertEqual(
            bridge["application_capture_authority"],
            "supabase_access_token_v1",
        )
        self.assertEqual(bridge["worker_base_table_privileges"], [])
        self.assertEqual(bridge["writer_base_table_privileges"], [])
        self.assertEqual(
            bridge["attachment_reads"],
            "existence_only_owner_thread_message_no_columns_returned",
        )
        self.assertFalse(bridge["attachment_content_reads"])
        self.assertTrue(bridge["attachment_presence_required_absent"])
        self.assertFalse(bridge["attachment_message_capture"])
        self.assertFalse(bridge["historical_scan_or_backfill"])
        self.assertEqual(bridge["pilot_capture_limit_per_owner_rolling_24h"], 20)
        self.assertEqual(
            bridge["pilot_capture_limit_count_scope"],
            "all_outbox_states_by_source_created_at",
        )
        self.assertEqual(
            bridge["pilot_capture_limit_lock"],
            "owner_scoped_transaction_advisory_lock",
        )
        self.assertEqual(
            bridge["pilot_capture_limit_result"],
            "pilot_limit_reached_null_outbox_id",
        )
        self.assertFalse(bridge["pilot_capture_replay_consumes_new_slot"])
        self.assertEqual(len(bridge["functions"]), len(set(bridge["functions"])))
        self.assertEqual(
            bridge["source_erasure_selectors"],
            ["thread", "message_tail", "recent", "all_conversations"],
        )
        self.assertTrue(bridge["source_erasure_exact_chat_targets_only"])
        self.assertFalse(bridge["source_erasure_memory_only_selector_allowed"])
        self.assertFalse(
            bridge["source_erasure_account_wide_memory_selector_allowed"]
        )
        self.assertEqual(
            bridge["source_erasure_chat_tables"],
            ["public.chat_log", "public.chat_attachments", "public.threads"],
        )
        self.assertEqual(
            bridge["source_erasure_transient_target_tables"],
            [
                "memory_ingest_private.source_erasure_target",
                "memory_ingest_private.source_erasure_thread_target",
            ],
        )
        self.assertEqual(
            bridge["source_erasure_permanent_tombstone_tables"],
            [
                "memory_ingest_private.source_erasure_message_tombstone",
                "memory_ingest_private.source_erasure_thread_tombstone",
            ],
        )
        self.assertEqual(
            bridge["source_erasure_tombstone_identity_scope"],
            "global_message_and_thread_uuid",
        )
        self.assertEqual(
            bridge["source_erasure_targets_retained_until"],
            "conversation_deletion_final_receipt_acknowledged",
        )
        self.assertTrue(bridge["source_erasure_tombstones_immutable"])
        self.assertEqual(
            bridge["source_erasure_runtime_catalog_attestation"],
            "exact_mutated_relation_schema_foreign_key_trigger_rule_"
            "and_inheritance_inventory",
        )
        self.assertFalse(
            bridge["source_erasure_unclassified_side_effects_allowed"]
        )
        self.assertFalse(
            bridge["source_erasure_legacy_capture_trigger_required"]
        )
        self.assertEqual(
            bridge["source_erasure_legacy_capture_trigger_if_present"],
            "forbidden_after_forward_exact_disabled_identity_restored_by_"
            "empty_only_rollback",
        )
        self.assertFalse(
            bridge["source_erasure_structured_lifeswitch_tables_allowed"]
        )
        self.assertTrue(
            bridge["source_erasure_lifeswitch_usage_and_audit_records_retained"]
        )
        self.assertEqual(
            bridge["source_erasure_vantage_answer_trace_disposition"],
            "undecided_activation_blocker",
        )
        self.assertEqual(
            bridge["source_erasure_telemetry_payload_disposition"],
            "undecided_activation_blocker",
        )
        self.assertFalse(bridge["source_erasure_accounts_deleted"])
        self.assertTrue(bridge["source_erasure_content_free_receipts_retained"])

    def test_lifecycle_and_epistemic_states_are_not_collapsed(self) -> None:
        states = self.contract["states"]
        self.assertEqual(
            states["claim_lifecycle"],
            ["active", "correction_pending", "retracted", "deletion_pending"],
        )
        self.assertEqual(
            states["claim_epistemic"],
            ["supported", "uncertain", "disputed"],
        )
        self.assertTrue(set(states["claim_lifecycle"]).isdisjoint(states["claim_epistemic"]))

    def test_terminal_proposal_replay_horizon_is_explicitly_bounded(self) -> None:
        replay_contract = self.contract["interface_contracts"][
            "terminal_proposal_replay"
        ]
        self.assertIsInstance(replay_contract, str)
        for required in (
            "30 days",
            "exact replay",
            "unavailable",
            "proposal_retention_purged",
            "content-free",
        ):
            with self.subTest(contract_phrase=required):
                self.assertIn(required, replay_contract)

    def test_answer_binding_retention_contract_is_explicit(self) -> None:
        retention = self.contract["interface_contracts"][
            "answer_binding_retention"
        ]
        for concept in (
            r"(?:90 days|90-day)",
            r"(?:server-owned[^.]*timestamp|server-clock)",
            r"(?:bounded worker|worker-only[^.]*bounded)",
            r"content-free[^.]*receipt",
        ):
            with self.subTest(retention_concept=concept):
                self.assertRegex(retention, concept)

    def test_hard_requirements_are_fail_closed(self) -> None:
        requirements = self.contract["hard_requirements"]
        for key in (
            "forced_rls_on_every_owner_table",
            "owner_composite_foreign_keys",
            "leases_use_skip_locked",
            "operation_id_idempotency",
            "qdrant_contains_only_admitted_current_projectable_claims",
        ):
            self.assertTrue(requirements[key], key)
        for key in (
            "runtime_direct_table_writes",
            "public_execute",
            "anon_access",
            "authenticated_role_access",
            "security_definer_schema_exposed",
            "legacy_names_allowed",
            "cascade_ddl_allowed",
        ):
            self.assertFalse(requirements[key], key)
        self.assertEqual(requirements["security_definer_search_path"], "pg_catalog")


class PackageIntegrityTests(unittest.TestCase):
    def test_bridge_package_inventories_deletion_state_and_scope(self) -> None:
        package = json.loads(
            (MIGRATIONS / "0002_conversation_bridge" / "package.json")
            .read_text(encoding="utf-8")
        )
        contract = package["object_contract"]
        self.assertEqual(contract["table_count"], 7)
        self.assertEqual(
            contract["source_erasure_transient_target_tables"],
            [
                "memory_ingest_private.source_erasure_target",
                "memory_ingest_private.source_erasure_thread_target",
            ],
        )
        self.assertEqual(
            contract["source_erasure_permanent_tombstone_tables"],
            [
                "memory_ingest_private.source_erasure_message_tombstone",
                "memory_ingest_private.source_erasure_thread_tombstone",
            ],
        )
        self.assertEqual(
            contract["source_erasure_tombstone_identity_scope"],
            "global_message_and_thread_uuid",
        )
        self.assertEqual(
            contract["source_erasure_targets_retained_until"],
            "conversation_deletion_final_receipt_acknowledged",
        )
        self.assertTrue(contract["source_erasure_tombstones_immutable"])
        self.assertFalse(
            contract["source_erasure_unclassified_side_effects_allowed"]
        )
        self.assertFalse(contract["source_erasure_legacy_capture_trigger_required"])
        self.assertFalse(contract["accounts_deleted"])
        self.assertFalse(contract["structured_lifeswitch_data_deleted"])
        self.assertTrue(contract["lifeswitch_usage_audit_records_retained"])
        self.assertEqual(
            contract["vantage_answer_trace_disposition"],
            "undecided_activation_blocker",
        )
        self.assertEqual(
            contract["telemetry_payload_disposition"],
            "undecided_activation_blocker",
        )
        self.assertFalse(package["hash_bindings"]["current_hash_rebind_required"])
        self.assertFalse(
            package["hash_bindings"]["root_migration_manifest_rebind_required"]
        )
        self.assertTrue(package["activation"]["disposable_database_validated"])
        self.assertFalse(contract["legacy_project_rows_deleted"])

    def test_exact_migration_artifact_set_exists(self) -> None:
        expected = {
            "manifest.json",
            "predicate_catalog.json",
            "roles_preflight.pgsql",
            "schema_contract.json",
            "0001_foundation/forward.pgsql",
            "0001_foundation/package.json",
            "0001_foundation/rollback.pgsql",
            "0002_conversation_bridge/forward.pgsql",
            "0002_conversation_bridge/package.json",
            "0002_conversation_bridge/rollback.pgsql",
            "0003_owner_claim_detail/forward.pgsql",
            "0003_owner_claim_detail/package.json",
            "0003_owner_claim_detail/rollback.pgsql",
            "0004_pilot_marker/forward.pgsql",
            "0004_pilot_marker/package.json",
            "0004_pilot_marker/rollback.pgsql",
            "0005_bounded_auto_admission/forward.pgsql",
            "0005_bounded_auto_admission/package.json",
            "0005_bounded_auto_admission/rollback.pgsql",
        }
        observed = {
            path.relative_to(MIGRATIONS).as_posix()
            for path in MIGRATIONS.rglob("*")
            if path.is_file()
        }
        self.assertEqual(observed, expected)

    def test_package_hashes_bind_exact_forward_and_rollback_bytes(self) -> None:
        for directory in ALL_PACKAGE_DIRS:
            package = _load_json(directory / "package.json")
            self.assertEqual(
                package["schema_version"],
                "governed-memory-migration-package-v2",
            )
            for kind in ("forward", "rollback"):
                relative, expected = _file_binding(package, kind)
                self.assertEqual(relative, f"{kind}.pgsql")
                observed = sha256((directory / relative).read_bytes()).hexdigest()
                self.assertEqual(observed, expected, f"{directory.name} {kind}")

    def test_root_manifest_binds_every_non_manifest_artifact(self) -> None:
        manifest = _load_json(ROOT_MANIFEST_PATH)
        self.assertEqual(
            manifest["schema_version"],
            "governed-memory-migration-manifest-v2",
        )
        files = manifest.get("files")
        self.assertIsInstance(files, list)
        bound: dict[str, str] = {}
        for item in files:
            self.assertIsInstance(item, dict)
            path = item.get("path")
            digest = item.get("sha256")
            self.assertIsInstance(path, str)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertNotIn(path, bound)
            bound[path] = digest
        observed_paths = {
            path.relative_to(MIGRATIONS).as_posix()
            for path in MIGRATIONS.rglob("*")
            if path.is_file() and path != ROOT_MANIFEST_PATH
        }
        self.assertEqual(set(bound), observed_paths)
        for relative, expected in bound.items():
            self.assertEqual(
                sha256((MIGRATIONS / relative).read_bytes()).hexdigest(),
                expected,
                relative,
            )

    def test_fail_closed_manifest_verifier_accepts_only_exact_phase8g_state(self) -> None:
        verifier = runpy.run_path(str(MANIFEST_VERIFIER_PATH))
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            verifier["reject_duplicate_keys"]([("scope", 1), ("scope", 2)])
        manifest = _load_json(ROOT_MANIFEST_PATH)
        self.assertEqual(
            manifest["status"],
            "isolated_candidate_disposable_validated_not_production_applied",
        )
        self.assertEqual(
            sha256(ROOT_MANIFEST_PATH.read_bytes()).hexdigest(),
            "d8a954d048cf1df775cc614da01964348620df86eb3ed88c078812f44a655dc0",
        )
        receipt = verifier["verify"](MIGRATIONS)
        self.assertEqual(receipt["result"], "artifact_integrity_verified")
        self.assertEqual(
            receipt["schema_version"],
            "governed-memory-migration-verification-v6",
        )
        self.assertEqual(
            receipt["validation_state"],
            "phase8g_current_candidate_disposable_validated",
        )
        self.assertTrue(receipt["current_disposable_validation_complete"])
        self.assertFalse(receipt["disposable_revalidation_required"])
        self.assertFalse(
            receipt["historical_phase7c_proof_reusable_for_current_candidate"]
        )
        self.assertFalse(receipt["production_state_changed"])

    def test_current_stores_only_manifest_is_exact_and_excludes_bridge(self) -> None:
        verifier = runpy.run_path(str(STORE_MANIFEST_VERIFIER_PATH))
        receipt = verifier["verify"]()
        self.assertEqual(
            receipt["schema_version"],
            "governed-memory-dormant-store-install-store-migration-verification-v3",
        )
        self.assertEqual(receipt["file_count"], 9)
        self.assertEqual(receipt["source_bridge_artifact_count"], 0)
        self.assertEqual(receipt["historical_package_descriptor_count"], 0)
        self.assertFalse(receipt["production_state_changed"])
        self.assertNotIn(
            "0002_conversation_bridge",
            "\n".join(receipt["artifact_sha256"]),
        )


class StaticSQLPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = _load_json(CONTRACT_PATH)
        cls.foundation = _without_comments(_sql(PACKAGE_DIRS[0] / "forward.pgsql"))
        cls.foundation_rollback = _without_comments(_sql(PACKAGE_DIRS[0] / "rollback.pgsql"))
        cls.bridge = _without_comments(_sql(PACKAGE_DIRS[1] / "forward.pgsql"))
        cls.bridge_rollback = _without_comments(_sql(PACKAGE_DIRS[1] / "rollback.pgsql"))
        cls.claim_detail = _without_comments(
            _sql(ADDITIVE_PACKAGE_DIRS[0] / "forward.pgsql")
        )
        cls.pilot_marker = _without_comments(
            _sql(ADDITIVE_PACKAGE_DIRS[1] / "forward.pgsql")
        )
        cls.roles = _without_comments(_sql(ROLES_PATH))

    def test_foundation_creates_exact_declared_tables(self) -> None:
        observed = set(
            re.findall(
                r"\bCREATE\s+TABLE\s+memory\.([a-z][a-z0-9_]*)\s*\(",
                self.foundation,
                flags=re.IGNORECASE,
            )
        )
        self.assertEqual(
            set(self.contract["tables"]),
            set(FOUNDATION_TABLES) | {"pilot_marker"},
        )
        self.assertEqual(observed, set(FOUNDATION_TABLES))

    def test_governed_source_erasure_replay_tombstones_are_exact(self) -> None:
        table = _table_definition(
            self.foundation, "memory.erased_chat_message_tombstone"
        )
        observed_columns = tuple(
            re.findall(
                r"^\s{2}([a-z][a-z0-9_]*)\s+"
                r"(?:uuid|timestamptz)\b",
                table,
                flags=re.IGNORECASE | re.MULTILINE,
            )
        )
        self.assertEqual(
            observed_columns,
            (
                "message_id",
                "owner_user_id",
                "erasure_operation_id",
                "erased_at",
            ),
        )
        self.assertRegex(table, r"message_id\s+uuid\s+PRIMARY\s+KEY")
        self.assertRegex(
            table,
            r"REFERENCES\s+memory\.source_erasure_operation\(\s*"
            r"owner_user_id,\s*operation_id\s*\)\s+ON\s+DELETE\s+RESTRICT",
        )
        definitions = _function_definitions(self.foundation)
        for required_function in (
            "memory_private.assert_chat_messages_not_erased",
            "memory_private.guard_erased_chat_message_replay",
            "memory_private.guard_erased_chat_message_tombstone_immutable",
            "memory_private.assert_source_erasure_tombstones",
            "memory_private.assert_source_erasure_deletion_catalog",
        ):
            self.assertIn(required_function, definitions)
        replay_guard = definitions[
            "memory_private.guard_erased_chat_message_replay"
        ]
        self.assertRegex(
            replay_guard,
            r"(?s)IF\s+TG_TABLE_SCHEMA\s*=\s*'memory'\s+AND\s+"
            r"TG_TABLE_NAME\s*=\s*'evidence'\s+THEN\s+IF\s+"
            r"NEW\.source_kind\s*=\s*'conversation_message'\s+THEN\s+"
            r"PERFORM\s+memory_private\.assert_chat_messages_not_erased\(\s*"
            r"NEW\.source_message_id,\s*NEW\.context_message_id\s*\);\s*"
            r"END\s+IF;\s+ELSIF\s+TG_TABLE_SCHEMA\s*=\s*'memory'\s+AND\s+"
            r"TG_TABLE_NAME\s*=\s*'answer_binding'\s+THEN\s+PERFORM\s+"
            r"memory_private\.assert_chat_messages_not_erased\(\s*"
            r"NEW\.response_id,\s*NULL::uuid\s*\);\s+ELSE\s+RAISE\s+"
            r"EXCEPTION\s+'erased chat replay guard attached to wrong table'",
        )
        self.assertNotRegex(
            replay_guard,
            r"TG_TABLE_NAME\s*=\s*'evidence'\s+AND\s+NEW\.source_kind",
        )
        tombstone_assertion = definitions[
            "memory_private.assert_source_erasure_tombstones"
        ]
        for exact_binding in (
            "tombstone.message_id = target.message_id",
            "tombstone.owner_user_id <> p_owner_user_id",
            "tombstone.erasure_operation_id <> p_operation_id",
            "tombstone.erased_at IS DISTINCT FROM p_sealed_at",
            "cross-owner chat message lineage appeared after seal",
        ):
            self.assertIn(exact_binding, tombstone_assertion)
        for relation in ("memory.evidence", "memory.answer_binding"):
            self.assertRegex(
                self.foundation,
                rf"(?s)CREATE\s+TRIGGER\s+erased_chat_message_replay\s+"
                rf"BEFORE\s+INSERT\s+ON\s+{re.escape(relation)}\s+"
                r"FOR\s+EACH\s+ROW\s+EXECUTE\s+FUNCTION\s+"
                r"memory_private\.guard_erased_chat_message_replay\(\)",
            )
        self.assertRegex(
            self.foundation,
            r"(?s)CREATE\s+TRIGGER\s+"
            r"erased_chat_message_tombstone_immutable\s+"
            r"BEFORE\s+UPDATE\s+OR\s+DELETE\s+ON\s+"
            r"memory\.erased_chat_message_tombstone\s+"
            r"FOR\s+EACH\s+ROW\s+EXECUTE\s+FUNCTION\s+"
            r"memory_private\.guard_erased_chat_message_tombstone_immutable\(\)",
        )
        for finalizer in (
            "memory_private.finalize_source_erasure_memory",
            "memory_private.ack_source_erasure_conversation_deleted",
        ):
            self.assertIn(
                "assert_source_erasure_tombstones",
                definitions[finalizer],
            )

    def test_worker_lane_scheduler_is_private_persistent_and_closed(self) -> None:
        scheduler = self.contract["worker_scheduler"]
        self.assertEqual(
            scheduler["sequence"],
            "memory_private.worker_lane_sequence",
        )
        self.assertEqual(
            scheduler["function"],
            "memory_private.next_worker_lane()",
        )
        self.assertEqual(
            scheduler["lane_order"],
            ["bridge", "extraction", "projection"],
        )
        self.assertTrue(scheduler["content_free"])
        self.assertFalse(scheduler["direct_runtime_sequence_privileges"])
        self.assertTrue(scheduler["advance_once_per_locked_invocation"])
        self.assertRegex(
            self.foundation,
            r"CREATE\s+SEQUENCE\s+memory_private\.worker_lane_sequence\s+AS\s+bigint",
        )
        definition = _function_definitions(self.foundation)[
            "memory_private.next_worker_lane"
        ]
        self.assertRegex(definition, r"SECURITY\s+DEFINER")
        self.assertRegex(definition, r"SET\s+search_path\s+TO\s+pg_catalog")
        self.assertEqual(definition.count("pg_catalog.nextval("), 1)
        self.assertIn("'bridge', 'extraction', 'projection'", definition)
        self.assertRegex(
            self.foundation,
            r"REVOKE\s+ALL\s+ON\s+SEQUENCE\s+memory_private\.worker_lane_sequence\s+FROM\s+PUBLIC,\s*governed_memory_api,\s*governed_memory_worker",
        )
        self.assertIn(
            "DROP FUNCTION memory_private.next_worker_lane();",
            self.foundation_rollback,
        )
        self.assertIn(
            "DROP SEQUENCE memory_private.worker_lane_sequence;",
            self.foundation_rollback,
        )
    def test_durable_error_and_reason_fields_accept_only_ascii_codes(self) -> None:
        code_pattern = r"'\^\[a-z\]\[a-z0-9_\]\{0,127\}\$'"
        scalar_fields = (
            (self.foundation, "memory.extraction_job", "last_error_code"),
            (self.foundation, "memory.provider_call", "error_code"),
            (self.foundation, "memory.projection_outbox", "last_error_code"),
            (self.foundation, "memory.audit_event", "reason_code"),
            (self.bridge, "memory_ingest_private.memory_ingest_outbox", "last_error_code"),
        )
        for sql, table, field in scalar_fields:
            block = _table_definition(sql, table)
            with self.subTest(table=table, field=field):
                self.assertRegex(
                    block,
                    rf"{field}\s*~\s*{code_pattern}",
                )

        closed_arrays = {
            "memory.proposal": (
                "review_reason_codes",
                {
                    "duplicate_existing",
                    "explicit_owner_review",
                    "lifecycle_override",
                    "not_durable",
                    "proposal_expired",
                    "proposal_incorrect",
                },
            ),
            "memory.claim_evidence": (
                "reason_codes",
                {
                    "duplicate_existing",
                    "explicit_owner_review",
                    "not_durable",
                    "proposal_incorrect",
                },
            ),
        }
        for table, (field, expected_codes) in closed_arrays.items():
            block = _table_definition(self.foundation, table)
            matches = re.findall(
                rf"is_sorted_unique_allowlist\(\s*{field},\s*"
                rf"ARRAY\[(.*?)\]::text\[\]",
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
            with self.subTest(table=table, field=field):
                self.assertRegex(
                    block,
                    rf"is_ascii_key_array\(\s*{field}\s*\)",
                )
                observed_allowlists = [
                    set(re.findall(r"'([a-z][a-z0-9_]*)'", raw))
                    for raw in matches
                ]
                self.assertIn(expected_codes, observed_allowlists)
                self.assertTrue(
                    all(codes <= expected_codes for codes in observed_allowlists)
                )

        canonical_code = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
        self.assertRegex("provider_timeout_after_dispatch", canonical_code)
        for contaminated in (
            "provider timeout",
            "https://provider.invalid/error",
            "User said synthetic secret text",
            "429: rate limited",
            "line1\nline2",
        ):
            with self.subTest(contaminated=contaminated):
                self.assertIsNone(canonical_code.fullmatch(contaminated))

    def test_complete_extraction_wire_schema_matches_validated_python_exactly(self) -> None:
        definitions = _function_definitions(self.foundation)
        complete = definitions["memory_private.complete_extraction"]
        key_match = re.search(
            r"jsonb_object_keys\(item\).*?<>\s*ARRAY\[(.*?)\]::text\[\]",
            complete,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(key_match)
        sql_keys = tuple(re.findall(r"'([a-z][a-z0-9_]*)'", key_match.group(1)))
        self.assertEqual(sql_keys, COMPLETE_EXTRACTION_ITEM_FIELDS)

        caller_policy_fields = (
            "domains",
            "intents",
            "surface",
            "requires_explicit",
            "projectable",
            "valid_from",
            "valid_to",
            "selected_sha256",
            "selection_binding_sha256",
            "predicate_catalog_sha256",
        )
        for field in caller_policy_fields:
            with self.subTest(field=field):
                self.assertNotRegex(
                    complete,
                    rf"item\s*->>?\s*'{re.escape(field)}'",
                )
        self.assertRegex(
            complete,
            r"jsonb_typeof\(item->'fact_index'\)\s*<>\s*'number'",
        )
        self.assertRegex(
            complete,
            r"\(item->>'fact_index'\)::smallint",
        )
        self.assertRegex(complete, r"item_fact_index\s+NOT\s+BETWEEN\s+0\s+AND\s+7")
        self.assertRegex(
            complete,
            r"jsonb_typeof\(item->'object_literal'\)\s*<>\s*'string'",
        )
        self.assertIn("computed_semantic_sha := memory_private.semantic_key_sha256(", complete)
        self.assertIn("computed_proposal_sha := memory_private.proposal_sha256(", complete)
        self.assertRegex(
            complete,
            r"item->>'proposal_sha256'\s*<>\s*computed_proposal_sha",
        )
        for derived in (
            "true, ARRAY[]::text[]",
            "ARRAY[]::text[]",
            "derived_surface",
            "derived_requires_explicit",
            "NULL::timestamptz",
        ):
            with self.subTest(derived=derived):
                self.assertIn(derived, complete)

    def test_self_display_name_is_null_across_wire_proposal_revision_and_renderer(self) -> None:
        complete = _function_definitions(self.foundation)[
            "memory_private.complete_extraction"
        ]
        self.assertRegex(
            complete,
            r"(?s)item->>'subject_entity_type'\s*=\s*'self'.*?"
            r"item->'subject_display_name'\s*<>\s*'null'::jsonb",
        )
        self.assertRegex(
            complete,
            r"(?s)item->>'subject_entity_type'\s*<>\s*'self'.*?"
            r"jsonb_typeof\(item->'subject_display_name'\)\s*<>\s*'string'",
        )

        for table in ("memory.proposal", "memory.claim_revision"):
            block = _table_definition(self.foundation, table)
            with self.subTest(table=table):
                self.assertRegex(block, r"subject_display_name\s+text(?:\s|,)")
                self.assertNotRegex(
                    block,
                    r"subject_display_name\s+text\s+NOT\s+NULL",
                )
                self.assertRegex(
                    block,
                    r"(?s)subject_entity_type\s*=\s*'self'.*?"
                    r"subject_display_name\s+IS\s+NULL",
                )
                self.assertRegex(
                    block,
                    r"(?s)subject_entity_type\s*<>\s*'self'.*?"
                    r"subject_display_name\s+IS\s+NOT\s+NULL.*?"
                    r"octet_length\(subject_display_name\)\s+BETWEEN\s+1\s+AND\s+256"
                    r".*?subject_display_name\s*=\s*normalize\(subject_display_name,\s*NFC\)",
                )

        review = _function_definitions(self.foundation)[
            "memory_private.review_proposal"
        ]
        self.assertRegex(
            review,
            r"(?s)INSERT\s+INTO\s+memory\.claim_revision\(.*?"
            r"subject_display_name.*?VALUES\s*\(.*?proposal\.subject_display_name",
        )
        correct = _function_definitions(self.foundation)[
            "memory_private.correct_claim"
        ]
        self.assertRegex(
            correct,
            r"CASE\s+WHEN\s+current_subject\.entity_type\s*=\s*'self'\s+"
            r"THEN\s+NULL::text\s+ELSE\s+current_subject\.display_name\s+END",
        )
        renderer = _function_definitions(self.foundation).get(
            "memory_private.render_relational_fact", ""
        )
        self.assertRegex(
            renderer,
            r"(?s)p_subject_entity_type\s*=\s*'self'.*?NULL",
        )

    def test_display_name_utf8_bound_is_256_on_every_sql_admission_surface(self) -> None:
        entity = _table_definition(self.foundation, "memory.entity")
        proposal = _table_definition(self.foundation, "memory.proposal")
        revision = _table_definition(self.foundation, "memory.claim_revision")
        definitions = _function_definitions(self.foundation)
        complete = definitions["memory_private.complete_extraction"]
        correct = definitions["memory_private.correct_claim"]

        self.assertRegex(
            entity,
            r"octet_length\(display_name\)\s+BETWEEN\s+1\s+AND\s+256",
        )
        for table in (proposal, revision):
            for field in ("subject_display_name", "object_display_name"):
                with self.subTest(field=field):
                    self.assertRegex(
                        table,
                        rf"octet_length\({field}\)\s+BETWEEN\s+1\s+AND\s+256",
                    )
        for field in ("subject_display_name", "object_display_name"):
            with self.subTest(complete_field=field):
                self.assertRegex(
                    complete,
                    rf"octet_length\(item->>'{field}'\)\s+"
                    r"NOT\s+BETWEEN\s+1\s+AND\s+256",
                )
        self.assertRegex(
            correct,
            r"octet_length\(p_replacement->>'object_display_name'\)\s+"
            r"NOT\s+BETWEEN\s+1\s+AND\s+256",
        )
        self.assertNotRegex(
            "\n".join((entity, proposal, revision, complete, correct)),
            r"(?:subject|object)?_?display_name[^;]{0,160}\b300\b",
        )

    def test_zero_fact_completion_persists_receipt_without_downstream_rows(self) -> None:
        complete = _function_definitions(self.foundation)[
            "memory_private.complete_extraction"
        ]
        self.assertRegex(
            complete,
            r"jsonb_array_length\(p_proposals\)\s+NOT\s+BETWEEN\s+0\s+AND\s+8",
        )
        self.assertNotRegex(
            complete,
            r"jsonb_array_length\(p_proposals\)\s+NOT\s+BETWEEN\s+1\s+AND\s+8",
        )
        loop_end = complete.find("END LOOP")
        completed_write = complete.find("UPDATE memory.provider_call", loop_end)
        self.assertGreaterEqual(loop_end, 0)
        self.assertGreater(completed_write, loop_end)
        completed_tail = complete[completed_write:]
        self.assertRegex(
            completed_tail,
            r"(?s)SET\s+state\s*=\s*'completed',\s*"
            r"response_sha256\s*=\s*p_response_sha256,\s*"
            r".*?completed_at\s*=",
        )
        self.assertRegex(complete, r"RETURN\s+QUERY\s+SELECT\s+p_outcome,\s*item_count")
        for table in ("claim", "claim_revision", "projection_outbox"):
            with self.subTest(table=table):
                self.assertNotRegex(
                    complete,
                    rf"INSERT\s+INTO\s+memory\.{table}\b",
                )

    def test_completion_usage_is_closed_budget_bound_persisted_and_receipted(self) -> None:
        complete = _function_definitions(self.foundation)[
            "memory_private.complete_extraction"
        ]
        signature = complete.split("RETURNS", 1)[0]
        self.assertRegex(
            signature,
            r"p_response_sha256\s+text,\s*p_input_tokens\s+integer,\s*"
            r"p_output_tokens\s+integer,\s*p_error_code\s+text",
        )
        self.assertRegex(
            complete,
            r"p_input_tokens\s+NOT\s+BETWEEN\s+0\s+AND\s+100000",
        )
        self.assertRegex(
            complete,
            r"p_output_tokens\s+NOT\s+BETWEEN\s+0\s+AND\s+"
            r"call\.max_output_tokens",
        )
        self.assertNotRegex(complete, r"p_output_tokens\s*>\s*8192")
        self.assertRegex(
            complete,
            r"(?s)p_outcome\s*=\s*'completed'.*?p_input_tokens\s+IS\s+NULL"
            r".*?p_output_tokens\s+IS\s+NULL",
        )
        self.assertRegex(
            complete,
            r"(?s)SET\s+state\s*=\s*'completed'.*?"
            r"input_tokens\s*=\s*p_input_tokens.*?"
            r"output_tokens\s*=\s*p_output_tokens",
        )
        completion_receipt = complete.split("completion_event_hash :=", 1)[1]
        for field in (
            "p_outcome",
            "p_request_sha256",
            "p_response_sha256",
            "p_input_tokens",
            "p_output_tokens",
            "p_error_code",
            "call.selection_binding_sha256",
        ):
            with self.subTest(field=field):
                self.assertIn(field, completion_receipt)

        provider_table = _table_definition(self.foundation, "memory.provider_call")
        self.assertRegex(
            provider_table,
            r"(?s)state\s*=\s*'completed'.*?input_tokens\s+IS\s+NOT\s+NULL"
            r".*?output_tokens\s+IS\s+NOT\s+NULL",
        )

    def test_complete_extraction_failure_states_match_the_python_reducer_exactly(self) -> None:
        definitions = _function_definitions(self.foundation)
        complete = definitions["memory_private.complete_extraction"]
        provider_table = _table_definition(self.foundation, "memory.provider_call")

        def guarded_codes(outcome: str) -> set[str]:
            match = re.search(
                rf"p_outcome\s*=\s*'{outcome}'.*?"
                rf"p_error_code\s+NOT\s+IN\s*\((.*?)\)",
                complete,
                flags=re.IGNORECASE | re.DOTALL,
            )
            self.assertIsNotNone(match, f"missing closed {outcome} reason guard")
            return set(re.findall(r"'([a-z][a-z0-9_]*)'", match.group(1)))

        expected_by_outcome = {
            "retryable_failure": set(NOT_EXECUTED_REASON_CODES),
            "terminal_failure": set(TERMINAL_PROVIDER_REASON_CODES),
            "outcome_unknown": set(UNKNOWN_OUTCOME_REASON_CODES),
        }
        for outcome, expected in expected_by_outcome.items():
            with self.subTest(outcome=outcome):
                self.assertEqual(guarded_codes(outcome), expected)

        constraint_names = {
            "retryable_failure": "provider_call_retry_semantics",
            "terminal_failure": "provider_call_terminal_semantics",
            "outcome_unknown": "provider_call_unknown_semantics",
        }
        for outcome, expected in expected_by_outcome.items():
            match = re.search(
                rf"CONSTRAINT\s+{constraint_names[outcome]}\s+CHECK\s*\("
                rf".*?state\s*<>\s*'{outcome}'.*?error_code\s+IN\s*\((.*?)\)"
                rf".*?\n\s*\)",
                provider_table,
                flags=re.IGNORECASE | re.DOTALL,
            )
            with self.subTest(outcome=outcome, surface="table"):
                self.assertIsNotNone(match)
                self.assertEqual(
                    set(re.findall(r"'([a-z][a-z0-9_]*)'", match.group(1))),
                    expected,
                )

        self.assertRegex(
            complete,
            r"(?s)p_outcome\s*=\s*'retryable_failure'.*?"
            r"call\.state\s*<>\s*'reserved'",
        )
        self.assertNotRegex(
            complete,
            r"(?s)call\.state\s*=\s*'dispatched'.{0,400}"
            r"p_outcome\s*=\s*'retryable_failure'",
        )
        self.assertRegex(
            complete,
            r"(?s)p_outcome\s*=\s*'retryable_failure'.*?"
            r"p_request_sha256\s+IS\s+NOT\s+NULL.*?"
            r"p_response_sha256\s+IS\s+NOT\s+NULL.*?"
            r"p_input_tokens\s+IS\s+NOT\s+NULL.*?"
            r"p_output_tokens\s+IS\s+NOT\s+NULL",
        )
        self.assertRegex(
            provider_table,
            r"(?s)state\s*=\s*'retryable_failure'.*?"
            r"request_sha256\s+IS\s+NULL.*?"
            r"response_sha256\s+IS\s+NULL.*?"
            r"input_tokens\s+IS\s+NULL.*?"
            r"output_tokens\s+IS\s+NULL.*?"
            r"dispatched_at\s+IS\s+NULL.*?"
            r"completed_at\s+IS\s+NOT\s+NULL",
        )
        self.assertRegex(
            complete,
            r"(?s)p_outcome\s+IN\s*\(\s*'completed',\s*'outcome_unknown'\s*\)"
            r".*?call\.state\s*<>\s*'dispatched'",
        )
        self.assertRegex(
            complete,
            r"(?s)p_outcome\s*=\s*'terminal_failure'.*?"
            r"call\.state\s*<>\s*'dispatched'.*?"
            r"call\.request_sha256\s*<>\s*p_request_sha256",
        )
        self.assertRegex(
            provider_table,
            r"(?s)state\s*=\s*'terminal_failure'.*?"
            r"request_sha256\s+IS\s+NOT\s+NULL.*?"
            r"response_sha256\s+IS\s+NULL.*?"
            r"dispatched_at\s+IS\s+NOT\s+NULL.*?"
            r"completed_at\s+IS\s+NOT\s+NULL",
        )

    def test_extraction_excerpt_lifetime_is_review_bound_and_fail_closed(self) -> None:
        definitions = _function_definitions(self.foundation)
        complete = definitions["memory_private.complete_extraction"]
        redaction = [
            definition
            for definition in definitions.values()
            if "SET review_excerpt = NULL" in definition
            and "memory.proposal" in definition
            and "pending_review" in definition
        ]

        self.assertRegex(
            complete,
            r"(?s)p_outcome\s*=\s*'completed'\s+AND\s+item_count\s*=\s*0"
            r".*?SET\s+review_excerpt\s*=\s*NULL",
        )
        self.assertRegex(
            complete,
            r"(?s)p_outcome\s+IN\s*\(\s*'terminal_failure',"
            r"\s*'outcome_unknown'\s*\).*?"
            r"SET\s+review_excerpt\s*=\s*NULL",
        )
        self.assertRegex(
            complete,
            r"(?s)p_outcome\s*=\s*'retryable_failure'\s+"
            r"AND\s+job\.attempt_count\s*>=\s*job\.max_attempts"
            r".*?SET\s+review_excerpt\s*=\s*NULL",
        )
        self.assertNotRegex(
            complete,
            r"(?s)p_outcome\s*=\s*'retryable_failure'\s*\)\s*THEN"
            r".{0,300}SET\s+review_excerpt\s*=\s*NULL",
        )
        self.assertNotRegex(
            complete,
            r"(?s)p_outcome\s*=\s*'completed'\s*(?:\)|OR|THEN)"
            r".{0,500}SET\s+review_excerpt\s*=\s*NULL",
        )
        self.assertTrue(
            redaction,
            "a bounded path must redact the excerpt once no proposal remains pending",
        )
        combined = "\n".join(redaction)
        self.assertRegex(
            combined,
            r"(?s)NOT\s+EXISTS\s*\(.*?FROM\s+memory\.proposal.*?"
            r"review_state\s*=\s*'pending_review'",
        )
        self.assertRegex(
            combined,
            r"(?s)NOT\s+EXISTS\s*\(.*?FROM\s+memory\.extraction_job.*?"
            r"state\s+IN\s*\(\s*'pending',\s*'claimed',\s*'retryable'\s*\)",
        )
        self.assertIn("evidence_excerpt_expired_before_dispatch", combined)
        self.assertRegex(
            combined,
            r"excerpt_expires_at\s*<=\s*pg_catalog\.clock_timestamp\(\)",
        )
        self.assertRegex(
            combined,
            r"(?s)(?:NOT\s+EXISTS\s*\(.*?review_state\s*=\s*'pending_review'.*?\)"
            r".*?OR.*?excerpt_expires_at\s*<=\s*pg_catalog\.clock_timestamp\(\)"
            r"|excerpt_expires_at\s*<=\s*pg_catalog\.clock_timestamp\(\)"
            r".*?OR.*?NOT\s+EXISTS\s*\(.*?review_state\s*=\s*'pending_review')",
        )
        self.assertRegex(combined, r"FOR\s+UPDATE\s+SKIP\s+LOCKED")
        self.assertRegex(combined, r"LIMIT\s+p_limit")

    def test_review_surface_and_rebuild_reader_are_cold_start_complete(self) -> None:
        definitions = _function_definitions(self.foundation)
        proposals = definitions["memory_private.list_proposals"]
        for field in (
            "operation_id",
            "source_excerpt",
            "subject_entity_type",
            "subject_entity_key",
            "predicate",
            "object_kind",
            "object_literal",
            "epistemic_state",
            "sensitivity",
        ):
            with self.subTest(review_field=field):
                self.assertIn(field, proposals)

        rebuild = definitions["memory_private.read_projection_rebuild_batch"]
        self.assertIn("session_user <> 'governed_memory_worker'", rebuild)
        self.assertIn("outbox.state = 'applied'", rebuild)
        self.assertIn("outbox.vector_sha256 IS NOT NULL", rebuild)
        self.assertIn("memory_private.claim_state_sha256", rebuild)
        self.assertIn("memory_private.projection_manifest_sha256", rebuild)
        self.assertRegex(
            self.foundation,
            r"GRANT\s+EXECUTE\s+ON\s+FUNCTION(?s:.*?)"
            r"memory_private\.read_projection_rebuild_batch\(uuid,uuid,integer\)"
            r"(?s:.*?)TO\s+governed_memory_worker",
        )

    def test_complete_extraction_exact_replay_precedes_mutation_and_cannot_duplicate(self) -> None:
        complete = _function_definitions(self.foundation)[
            "memory_private.complete_extraction"
        ]
        replay_start = complete.find("IF call.state IN (")
        replay_end_marker = "RETURN;\n  END IF;"
        replay_end = complete.find(replay_end_marker, replay_start)
        self.assertGreaterEqual(replay_start, 0, TERMINAL_REPLAY_CONTRACT)
        self.assertGreater(replay_end, replay_start, TERMINAL_REPLAY_CONTRACT)
        replay = complete[replay_start : replay_end + len(replay_end_marker)]
        self.assertRegex(
            replay,
            r"IF\s+call\.state\s+IN\s*\(\s*'completed',\s*"
            r"'retryable_failure',\s*'terminal_failure',\s*"
            r"'outcome_unknown'\s*\)",
        )
        for field in (
            "p_outcome",
            "p_request_sha256",
            "p_response_sha256",
            "p_input_tokens",
            "p_output_tokens",
            "p_error_code",
        ):
            with self.subTest(field=field):
                self.assertIn(field, replay)
        self.assertRegex(replay, r"RAISE\s+EXCEPTION\s+'[^']*replay[^']*(?:drift|conflict)[^']*'")
        self.assertRegex(
            replay,
            r"RETURN\s+QUERY\s+SELECT\s+(?:p_outcome|'replayed'::text)",
        )
        self.assertLess(replay_start, complete.find("INSERT INTO memory.proposal"))
        self.assertLess(replay_start, complete.find("UPDATE memory.provider_call"))
        self.assertEqual(complete.count("INSERT INTO memory.proposal("), 1)

    def test_complete_and_review_recompute_the_same_framed_proposal_hash(self) -> None:
        definitions = _function_definitions(self.foundation)
        proposal_hash = definitions["memory_private.proposal_sha256"]
        complete = definitions["memory_private.complete_extraction"]
        review = definitions["memory_private.review_proposal"]
        expected_order = PROPOSAL_HASH_BINDING_FIELDS + (
            "fact_index",
            "proposal_id",
            "operation_id",
            "subject_entity_type",
            "subject_entity_key",
            "subject_display_name",
            "predicate",
            "object_kind",
            "object_entity_type",
            "object_entity_key",
            "object_display_name",
            "object_literal",
            "epistemic_state",
            "sensitivity",
            "semantic_key_sha256",
            "projectable",
            "domains",
            "intents",
            "surface",
            "requires_explicit",
            "valid_from",
            "valid_to",
        )
        framed_names = tuple(
            re.findall(
                r"framed_utf8_field\(\s*'([a-z][a-z0-9_]*)'",
                proposal_hash,
                flags=re.IGNORECASE,
            )
        )
        self.assertEqual(framed_names, expected_order)
        self.assertIn("governed_memory.proposal.v1", proposal_hash)
        self.assertNotIn("governed_memory.proposal.v1|", proposal_hash)
        self.assertEqual(
            complete.count("memory_private.proposal_sha256("),
            2,
            "completed replay and first admission must each recompute the proposal hash",
        )
        self.assertEqual(review.count("memory_private.proposal_sha256("), 1)
        self.assertRegex(
            review,
            r"proposal\.proposal_sha256\s*<>\s*recomputed_proposal_hash",
        )

    def test_review_replay_returns_the_original_claim_revision_and_outbox(self) -> None:
        review = _function_definitions(self.foundation)[
            "memory_private.review_proposal"
        ]
        replay_start = review.find("IF proposal.review_state <> 'pending_review'")
        replay_end = review.find("RETURN;", replay_start)
        self.assertGreaterEqual(replay_start, 0)
        self.assertGreater(replay_end, replay_start)
        replay = review[replay_start : replay_end + len("RETURN;")]
        self.assertRegex(
            replay,
            r"FROM\s+memory\.claim_revision\s+AS\s+revision",
        )
        self.assertRegex(
            replay,
            r"FROM\s+memory\.projection_outbox\s+AS\s+value",
        )
        self.assertIn("existing_projection.outbox_id", replay)
        self.assertIn("'admission-projection'", replay)
        self.assertIn("'correction-rejection-projection'", replay)
        self.assertRegex(
            replay,
            r"(?s)RETURN\s+QUERY\s+SELECT\s+'replayed'::text,\s*"
            r"resulting_claim_id,\s*resulting_revision_id,\s*"
            r"resulting_outbox_id",
        )

    def test_claim_head_and_full_revision_fact_policy_have_closed_hash_surfaces(self) -> None:
        definitions = _function_definitions(self.foundation)
        fact_policy = definitions["memory_private.revision_fact_policy_sha256"]
        claim_state = definitions["memory_private.claim_state_sha256"]
        self.assertEqual(
            set(
                re.findall(
                    r"framed_utf8_field\(\s*'([a-z][a-z0-9_]*)'",
                    fact_policy,
                    flags=re.IGNORECASE,
                )
            ),
            {
                "subject_entity_key",
                "subject_entity_type",
                "subject_display_name",
                "predicate",
                "object_kind",
                "object_entity_key",
                "object_entity_type",
                "object_display_name",
                "object_literal",
                "epistemic_state",
                "sensitivity",
                "projectable",
                "domains",
                "intents",
                "surface",
                "requires_explicit",
                "valid_from",
                "valid_to",
                "selected_sha256",
                "selection_binding_sha256",
                "predicate_catalog_sha256",
                "retrieval_text_sha256",
            },
        )
        self.assertEqual(
            set(
                re.findall(
                    r"framed_utf8_field\(\s*'([a-z][a-z0-9_]*)'",
                    claim_state,
                    flags=re.IGNORECASE,
                )
            ),
            set(EXPECTED_CLAIM_STATE_HASH_FIELDS),
        )
        claim_table = self.foundation.split("CREATE TABLE memory.claim (", 1)[1].split(
            ");", 1
        )[0]
        self.assertRegex(
            claim_table,
            r"current_state_sha256\s+text\s+NOT\s+NULL",
        )
        self.assertRegex(
            claim_table,
            r"current_state_sha256\s*~\s*'\^\[0-9a-f\]\{64\}\$'",
        )

    def test_every_claim_transition_recomputes_and_cas_checks_current_state(self) -> None:
        definitions = _function_definitions(self.foundation)
        minimum_state_hash_calls = {
            "memory_private.review_proposal": 4,
            "memory_private.expire_proposals": 2,
            "memory_private.correct_claim": 2,
            "memory_private.retract_claim": 2,
            "memory_private.request_claim_deletion": 2,
            "memory_private.finalize_claim_deletion": 1,
        }
        for name, minimum in minimum_state_hash_calls.items():
            definition = definitions[name]
            with self.subTest(function=name):
                self.assertGreaterEqual(
                    definition.count("memory_private.claim_state_sha256("),
                    minimum,
                )
                self.assertIn("current_state_sha256", definition)

        review = definitions["memory_private.review_proposal"]
        expiry = definitions["memory_private.expire_proposals"]
        for transition, definition in (
            ("correction rejection", review),
            ("correction expiry", expiry),
        ):
            with self.subTest(transition=transition):
                self.assertRegex(
                    definition,
                    r"SET\s+lifecycle_state\s*=\s*'active',"
                    r"\s*current_state_sha256\s*=\s*new_state_hash",
                )
                self.assertRegex(
                    definition,
                    r"prior_state_hash\s*<>\s*target_claim\.current_state_sha256",
                )

    def test_lifecycle_replays_preserve_cas_and_delete_survives_final_purge(self) -> None:
        definitions = _function_definitions(self.foundation)
        for function_name, receipt_aliases in (
            ("memory_private.retract_claim", ("existing_outbox",)),
            (
                "memory_private.request_claim_deletion",
                ("existing_deletion_receipt", "existing_outbox"),
            ),
        ):
            definition = definitions[function_name]
            replay_end = definition.find("SELECT value.* INTO STRICT target")
            self.assertGreaterEqual(replay_end, 0, function_name)
            replay = definition[:replay_end]
            for receipt_alias in receipt_aliases:
                with self.subTest(
                    function=function_name,
                    binding="revision",
                    receipt_alias=receipt_alias,
                ):
                    self.assertRegex(
                        replay,
                        rf"{receipt_alias}\.revision_sha256\s*"
                        rf"(?:IS\s+DISTINCT\s+FROM|<>|=)\s*"
                        rf"p_expected_revision_sha256",
                    )
            with self.subTest(function=function_name, binding="state"):
                self.assertRegex(
                    replay,
                    r"existing_transition\.prior_state_sha256\s*"
                    r"(?:IS\s+DISTINCT\s+FROM|<>|=)\s*"
                    r"p_expected_state_sha256",
                )

        deletion = definitions["memory_private.request_claim_deletion"]
        self.assertIn("memory.claim_deletion_receipt", deletion)
        retained_replay = deletion.split("memory.claim_deletion_receipt", 1)[1]
        for binding in (
            "operation_id",
            "claim_id",
            "prior_state_sha256",
            "revision_sha256",
            "delete_outbox_id",
        ):
            with self.subTest(retained_deletion_binding=binding):
                self.assertIn(binding, retained_replay)

    def test_correct_claim_declares_every_new_retrieval_local(self) -> None:
        correct = _function_definitions(self.foundation)[
            "memory_private.correct_claim"
        ]
        declarations = correct.split("DECLARE", 1)[1].split("BEGIN", 1)[0]
        self.assertRegex(
            declarations,
            r"\breplacement_retrieval_text\s+text\s*;",
        )
        self.assertGreaterEqual(correct.count("replacement_retrieval_text"), 3)

    def test_owner_correction_evidence_has_full_deterministic_selection_lineage(self) -> None:
        evidence = _table_definition(self.foundation, "memory.evidence")
        correction_shape = evidence.split("source_kind = 'owner_correction'", 1)[1]
        for field in (
            "source_message_id",
            "source_thread_id",
            "source_window_id",
            "source_window_sha256",
        ):
            with self.subTest(field=field):
                self.assertRegex(correction_shape, rf"{field}\s+IS\s+NOT\s+NULL")
        self.assertRegex(correction_shape, r"selected_start_utf8\s*=\s*0")
        self.assertRegex(correction_shape, r"selected_end_utf8\s*>\s*0")
        self.assertRegex(correction_shape, r"context_message_id\s+IS\s+NULL")
        self.assertRegex(correction_shape, r"context_sha256\s+IS\s+NULL")
        self.assertRegex(correction_shape, r"selected_sha256\s*=\s*source_sha256")

        correct = _function_definitions(self.foundation)[
            "memory_private.correct_claim"
        ]
        self.assertRegex(
            correct,
            r"new_evidence_id\s*:=\s*memory_private\.derived_uuid\(\s*"
            r"p_operation_id,\s*'correction-evidence'\s*\)",
        )
        derived_ids = {
            field: label
            for field, label in (
                ("correction_source_message_id", "source-message"),
                ("correction_source_thread_id", "source-thread"),
                ("correction_source_window_id", "source-window"),
            )
        }
        for field, label in derived_ids.items():
            with self.subTest(field=field):
                self.assertRegex(
                    correct,
                    rf"{field}\s*:=\s*memory_private\.uuid5\(\s*"
                    rf"new_evidence_id,\s*'{label}'\s*\)",
                )
        self.assertRegex(
            correct,
            r"correction_end_utf8\s*:=\s*pg_catalog\.octet_length\(\s*"
            r"pg_catalog\.convert_to\(correction_material,\s*'UTF8'\)\s*\)",
        )
        self.assertRegex(
            correct,
            r"new_selection_binding_sha\s*:=\s*"
            r"memory_private\.selection_binding_sha256\(\s*"
            r"actor,\s*'owner_correction',\s*correction_source_message_id,\s*"
            r"correction_source_thread_id,\s*correction_source_window_id,\s*"
            r"correction_window_sha,\s*new_source_sha,\s*new_source_sha,\s*"
            r"0,\s*correction_end_utf8,\s*NULL::uuid,\s*NULL::text\s*\)",
        )
        self.assertRegex(
            correct,
            r"source_message_id,\s*source_thread_id,\s*source_window_id,\s*"
            r"source_window_sha256,\s*source_sha256,\s*selected_sha256,\s*"
            r"selection_binding_sha256,\s*selected_start_utf8,\s*"
            r"selected_end_utf8,\s*source_created_at",
        )
        self.assertRegex(
            correct,
            r"correction_source_message_id,\s*correction_source_thread_id,\s*"
            r"correction_source_window_id,\s*correction_window_sha,\s*"
            r"new_source_sha,\s*new_source_sha,\s*new_selection_binding_sha,\s*"
            r"0,\s*correction_end_utf8,",
        )

    def test_rejected_and_expired_proposals_are_structurally_purged_after_retention(self) -> None:
        definitions = _function_definitions(self.foundation)
        purgers = {
            name: definition
            for name, definition in definitions.items()
            if re.search(
                r"DELETE\s+FROM\s+memory\.proposal\b",
                definition,
                flags=re.IGNORECASE,
            )
            and "purge_after" in definition
        }
        self.assertEqual(
            len(purgers),
            1,
            "one bounded retention function must purge terminal proposal content",
        )
        _, purge = next(iter(purgers.items()))
        self.assertRegex(
            purge,
            r"review_state\s+IN\s*\(\s*'rejected',\s*'expired'\s*\)",
        )
        self.assertRegex(
            purge,
            r"purge_after\s*<=\s*pg_catalog\.transaction_timestamp\(\)",
        )
        self.assertRegex(purge, r"FOR\s+UPDATE\s+SKIP\s+LOCKED")
        self.assertRegex(purge, r"LIMIT\s+p_limit")
        audit_position = purge.find("INSERT INTO memory.audit_event")
        delete_position = purge.find("DELETE FROM memory.proposal")
        self.assertGreaterEqual(audit_position, 0)
        self.assertGreater(delete_position, audit_position)
        self.assertIn("proposal_sha256", purge)
        self.assertIn("event_sha256", purge)
        self.assertNotRegex(purge, r"review_state\s*=\s*'admitted'")
        self.assertNotRegex(purge, r"review_state\s*=\s*'pending_review'")
        for personal_field in (
            "subject_display_name",
            "object_display_name",
            "object_literal",
            "review_excerpt",
        ):
            with self.subTest(personal_field=personal_field):
                self.assertNotIn(personal_field, purge)

    def test_sql_relational_renderer_is_exact_compact_json_with_fixed_unicode_vector(self) -> None:
        self.assertNotIn("%s | %s | %s", self.foundation)
        definitions = _function_definitions(self.foundation)
        self.assertIn("memory_private.render_relational_fact", definitions)
        renderer = definitions["memory_private.render_relational_fact"]
        for prohibited in (
            "jsonb_build_object",
            "json_build_object",
            "::jsonb::text",
            "::jsonb)::text",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, renderer.lower())
        self.assertIn("pg_catalog.to_json", renderer)
        self.assertIn("p_object_kind = 'literal'", renderer)
        self.assertIn("p_object_kind = 'entity'", renderer)

        top_level_positions = [
            renderer.find(fragment)
            for fragment in ('{"object":', ',"predicate":', ',"subject":')
        ]
        self.assertTrue(all(position >= 0 for position in top_level_positions))
        self.assertEqual(top_level_positions, sorted(top_level_positions))

        literal_branch = renderer.split(
            "WHEN p_object_kind = 'literal' THEN", 1
        )[1].split("WHEN p_object_kind = 'entity' THEN", 1)[0]
        literal_positions = [
            literal_branch.find(fragment)
            for fragment in ('"kind":"literal"', '"literal":')
        ]
        self.assertTrue(all(position >= 0 for position in literal_positions))
        self.assertEqual(literal_positions, sorted(literal_positions))

        entity_branch = renderer.split(
            "WHEN p_object_kind = 'entity' THEN", 1
        )[1].split("ELSE NULL::text", 1)[0]
        entity_positions = [
            entity_branch.find(fragment)
            for fragment in (
                '"display_name":',
                '"entity_key":',
                '"entity_type":',
                '"kind":"entity"',
            )
        ]
        self.assertTrue(all(position >= 0 for position in entity_positions))
        self.assertEqual(entity_positions, sorted(entity_positions))

        subject_branch = renderer.split(',"subject":', 1)[1]
        subject_positions = [
            subject_branch.find(fragment)
            for fragment in ('"display_name":', '"entity_key":', '"entity_type":')
        ]
        self.assertTrue(all(position >= 0 for position in subject_positions))
        self.assertEqual(subject_positions, sorted(subject_positions))

        self.assertIn(
            "memory_private.render_relational_fact(",
            definitions["memory_private.review_proposal"],
        )
        self.assertNotIn(
            "INSERT INTO memory.claim_revision",
            definitions["memory_private.correct_claim"],
        )

        expected_sha = (
            "404e70a184945366051bd89614ea852e8530f46141f552ae2ff9a897e654004e"
        )
        self.assertIn(expected_sha, self.foundation)
        unicode_checks = [
            block
            for block in _sql_do_blocks(self.foundation)
            if expected_sha in block
        ]
        self.assertEqual(len(unicode_checks), 1)
        self.assertIn("café", unicode_checks[0])
        self.assertIn("猫", unicode_checks[0])
        self.assertRegex(
            unicode_checks[0],
            r"(?s)sha256\(\s*pg_catalog\.convert_to\(.*?'UTF8'\s*\)\s*\)",
        )

    def test_pending_correction_concurrency_cannot_resurrect_terminal_claims(self) -> None:
        definitions = _function_definitions(self.foundation)
        correct = definitions["memory_private.correct_claim"]
        review = definitions["memory_private.review_proposal"]
        expire = definitions["memory_private.expire_proposals"]
        reject_for_lifecycle = definitions[
            "memory_private.reject_pending_correction_for_lifecycle"
        ]

        lock_position = correct.find("FROM memory.claim AS value")
        active_check = correct.find("target.lifecycle_state <> 'active'")
        pending_update = correct.find("lifecycle_state = 'correction_pending'")
        self.assertGreaterEqual(lock_position, 0)
        self.assertIn("FOR UPDATE", correct[lock_position:active_check])
        self.assertGreater(active_check, lock_position)
        self.assertGreater(pending_update, active_check)
        self.assertIn("correction_pending_state_sha256", correct)

        self.assertRegex(
            reject_for_lifecycle,
            r"(?s)FROM\s+memory\.claim\s+AS\s+value.*?FOR\s+UPDATE",
        )
        self.assertRegex(
            reject_for_lifecycle,
            r"target\.lifecycle_state\s*<>\s*'correction_pending'",
        )
        self.assertRegex(
            reject_for_lifecycle,
            r"(?s)FROM\s+memory\.proposal\s+AS\s+value.*?"
            r"review_state\s*=\s*'pending_review'.*?FOR\s+UPDATE",
        )
        for left, right in (
            ("correction_target_revision_id", "revision.revision_id"),
            ("correction_target_revision_number", "revision.revision_number"),
            ("expected_revision_sha256", "revision.revision_sha256"),
            ("correction_target_identity_sha256", "target.claim_identity_sha256"),
            ("correction_pending_state_sha256", "target.current_state_sha256"),
        ):
            with self.subTest(lifecycle_binding=left):
                self.assertRegex(
                    reject_for_lifecycle,
                    rf"{left}\s*=\s*{re.escape(right)}",
                )
        self.assertRegex(
            reject_for_lifecycle,
            r"review_reason_codes\s*=\s*"
            r"ARRAY\['lifecycle_override'\]::text\[\]",
        )
        self.assertIn(
            "correction_rejected_lifecycle_override", reject_for_lifecycle
        )

        for name, definition in (
            ("retract", definitions["memory_private.retract_claim"]),
            ("delete", definitions["memory_private.request_claim_deletion"]),
        ):
            with self.subTest(operation=name):
                self.assertRegex(
                    definition,
                    r"target\.lifecycle_state\s+NOT\s+IN\s*\(\s*'active',\s*"
                    r"'correction_pending'",
                )
                helper_call = re.search(
                    r"IF\s+target\.lifecycle_state\s*=\s*"
                    r"'correction_pending'\s+THEN\s+PERFORM\s+"
                    r"memory_private\.reject_pending_correction_for_lifecycle\(\s*"
                    r"actor,\s*target\.claim_id,\s*p_operation_id\s*\)",
                    definition,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                self.assertIsNotNone(
                    helper_call,
                    f"{name} must terminalize the pending correction atomically",
                )
                terminalize_position = helper_call.start()
                claim_update_position = definition.find("UPDATE memory.claim")
                self.assertGreater(claim_update_position, terminalize_position)

        self.assertRegex(
            review,
            r"target_claim\.lifecycle_state\s*<>\s*'correction_pending'",
        )
        self.assertRegex(
            review,
            r"target_claim\.current_state_sha256\s*<>\s*"
            r"proposal\.correction_pending_state_sha256",
        )
        self.assertRegex(review, r"SET\s+lifecycle_state\s*=\s*'active'")

        self.assertRegex(
            expire,
            r"target_claim\.lifecycle_state\s*=\s*'correction_pending'",
        )
        self.assertRegex(
            expire,
            r"target_claim\.current_state_sha256\s*"
            r"<>\s*candidate\.correction_pending_state_sha256",
        )
        self.assertRegex(expire, r"SET\s+lifecycle_state\s*=\s*'active'")
        for definition in (review, expire):
            self.assertIn("correction_pending_state_sha256", definition)

        self.assertRegex(
            expire,
            r"WHERE\s+proposal\.review_state\s*=\s*'pending_review'",
        )
        self.assertRegex(
            review,
            r"proposal\.review_state\s*<>\s*'pending_review'",
        )

    def test_provider_call_updates_are_monotonic_and_binding_hashes_are_write_once(self) -> None:
        trigger = re.search(
            r"CREATE\s+TRIGGER\s+[a-z][a-z0-9_]*\s+"
            r"BEFORE\s+UPDATE\s+OR\s+DELETE\s+ON\s+memory\.provider_call\s+"
            r"FOR\s+EACH\s+ROW\s+EXECUTE\s+FUNCTION\s+"
            r"(memory_private\.[a-z][a-z0-9_]*)\(\)\s*;",
            self.foundation,
            flags=re.IGNORECASE,
        )
        self.assertIsNotNone(trigger, "provider_call lacks its update/delete guard")
        definitions = _function_definitions(self.foundation)
        guard = definitions[trigger.group(1).lower()]

        immutable_fields = (
            "provider_call_id",
            "owner_user_id",
            "job_id",
            "attempt_number",
            "operation_id",
            "idempotency_sha256",
            "provider",
            "model",
            "schema_sha256",
            "selected_sha256",
            "selection_binding_sha256",
            "predicate_catalog_sha256",
            "operation",
            "privacy_manifest_sha256",
            "max_output_tokens",
            "timeout_ms",
            "reserved_at",
        )
        for field in immutable_fields:
            with self.subTest(immutable_field=field):
                self.assertRegex(
                    guard,
                    rf"OLD\.{field}\s+IS\s+DISTINCT\s+FROM\s+NEW\.{field}",
                )

        transition_sets = {
            "reserved": {"dispatched", "retryable_failure"},
            "dispatched": {"completed", "terminal_failure", "outcome_unknown"},
        }
        for old_state, expected_new_states in transition_sets.items():
            match = re.search(
                rf"OLD\.state\s*=\s*'{old_state}'\s+AND\s+NEW\.state\s+IN\s*"
                rf"\((.*?)\)",
                guard,
                flags=re.IGNORECASE | re.DOTALL,
            )
            with self.subTest(old_state=old_state):
                self.assertIsNotNone(match)
                self.assertEqual(
                    set(re.findall(r"'([a-z_]+)'", match.group(1))),
                    expected_new_states,
                )
        self.assertNotRegex(guard, r"OLD\.state\s*=\s*'retryable_failure'")
        self.assertNotRegex(guard, r"OLD\.state\s*=\s*'terminal_failure'")
        self.assertNotRegex(guard, r"OLD\.state\s*=\s*'outcome_unknown'")

        for field in (
            "request_sha256",
            "response_sha256",
            "input_tokens",
            "output_tokens",
            "dispatched_at",
            "completed_at",
        ):
            with self.subTest(field=field):
                self.assertRegex(
                    guard,
                    rf"OLD\.{field}\s+IS\s+NOT\s+NULL\s+"
                    rf"AND\s+OLD\.{field}\s+IS\s+DISTINCT\s+FROM\s+NEW\.{field}",
                )

        self.assertRegex(guard, r"TG_OP\s*=\s*'DELETE'")
        self.assertRegex(guard, r"session_user\s*<>\s*'governed_memory_worker'")
        self.assertIn("app.memory_hard_delete_operation_id", guard)

    def test_provider_dispatch_replay_never_reauthorizes_an_external_call(self) -> None:
        dispatch = _function_definitions(self.foundation)[
            "memory_private.mark_provider_call_dispatched"
        ]
        for required_hash in (
            "p_request_sha256",
            "p_expected_selected_sha256",
            "p_expected_selection_binding_sha256",
            "p_expected_predicate_catalog_sha256",
        ):
            with self.subTest(required_dispatch_hash=required_hash):
                self.assertRegex(dispatch, rf"{required_hash}\s+IS\s+NULL")
                self.assertRegex(
                    dispatch,
                    rf"{required_hash}\s*!~\s*'\^\[0-9a-f\]\{{64\}}\$'",
                )

        replay_start = dispatch.find("IF call.state = 'dispatched' THEN")
        reserved_gate = dispatch.find("IF call.state <> 'reserved' THEN")
        update = dispatch.find("UPDATE memory.provider_call")
        returned = dispatch.find("RETURN QUERY SELECT 'dispatched'::text")
        self.assertGreaterEqual(replay_start, 0)
        self.assertGreater(reserved_gate, replay_start)
        self.assertGreater(update, reserved_gate)
        self.assertGreater(returned, update)
        replay = dispatch[replay_start:reserved_gate]
        self.assertRegex(
            replay,
            r"call\.request_sha256\s*<>\s*p_request_sha256",
        )
        self.assertRegex(replay, r"external retry is forbidden")
        self.assertNotIn("RETURN QUERY", replay)
        self.assertNotIn("call_allowed", dispatch)

    def test_idempotent_receipt_reads_are_serialized_before_replay_or_insert(self) -> None:
        definitions = _function_definitions(self.foundation)
        answer = definitions["memory_private.record_answer_binding"]
        answer_count = answer.find("FROM memory.answer_binding AS value")
        answer_insert = answer.find("INSERT INTO memory.answer_binding")
        self.assertGreater(answer_count, 0)
        self.assertGreater(answer_insert, answer_count)
        answer_prefix = answer[:answer_count]
        self.assertGreaterEqual(
            answer_prefix.count("pg_catalog.pg_advisory_xact_lock("), 2
        )
        self.assertIn("p_operation_id", answer_prefix)
        self.assertIn("p_response_id", answer_prefix)

        for function_name, receipt_marker in (
            ("memory_private.retract_claim", "FROM memory.projection_outbox"),
            (
                "memory_private.request_claim_deletion",
                "FROM memory.claim_deletion_receipt",
            ),
            (
                "memory_private.finalize_claim_deletion",
                "FROM memory.claim_deletion_receipt",
            ),
        ):
            definition = definitions[function_name]
            lock = definition.find("pg_catalog.pg_advisory_xact_lock(")
            receipt = definition.find(receipt_marker)
            with self.subTest(function=function_name):
                self.assertGreaterEqual(lock, 0)
                self.assertGreater(receipt, lock)

    def test_correction_paths_share_claim_then_proposal_lock_order(self) -> None:
        definitions = _function_definitions(self.foundation)
        review = definitions["memory_private.review_proposal"]
        expiry = definitions["memory_private.expire_proposals"]
        reject = definitions[
            "memory_private.reject_pending_correction_for_lifecycle"
        ]

        review_operation_lock = review.find("'|operation|'")
        review_claim_lock = review.find("'|claim|'")
        review_proposal_lock = review.find("'|proposal|'")
        review_proposal_row_lock = review.find("FOR UPDATE")
        self.assertGreaterEqual(review_operation_lock, 0)
        self.assertGreater(review_claim_lock, review_operation_lock)
        self.assertGreaterEqual(review_claim_lock, 0)
        self.assertGreater(review_proposal_lock, review_claim_lock)
        self.assertGreater(review_proposal_row_lock, review_proposal_lock)
        self.assertRegex(
            review[:review_claim_lock],
            r"(?s)SELECT\s+(?:value\.correction_of_claim_id|value\.\*)\s+"
            r"INTO(?:\s+STRICT)?\s+[a-z][a-z0-9_]*\s+"
            r"FROM\s+memory\.proposal.*?proposal_id\s*=\s*p_proposal_id",
        )
        self.assertNotIn("FOR UPDATE", review[:review_claim_lock])
        locked_review = review[review_proposal_row_lock:]
        self.assertRegex(
            locked_review,
            r"proposal\.correction_of_claim_id\s+IS\s+DISTINCT\s+FROM\s+"
            r"(?:proposal_preview\.correction_of_claim_id|"
            r"preview_correction_claim_id)",
        )

        expiry_loop_match = re.search(r"FOR\s+candidate(?:_key)?\s+IN", expiry)
        self.assertIsNotNone(expiry_loop_match)
        expiry_loop = expiry_loop_match.start()
        expiry_loop_body = expiry.index("LOOP", expiry_loop)
        self.assertNotIn("FOR UPDATE", expiry[expiry_loop:expiry_loop_body])
        expiry_claim_lock = expiry.find("'|claim|'", expiry_loop_body)
        expiry_proposal_lock = expiry.find("'|proposal|'", expiry_loop_body)
        expiry_proposal_row_lock = expiry.find("FOR UPDATE", expiry_loop_body)
        self.assertGreater(expiry_claim_lock, expiry_loop_body)
        self.assertGreater(expiry_proposal_lock, expiry_claim_lock)
        self.assertGreater(expiry_proposal_row_lock, expiry_proposal_lock)
        locked_expiry = expiry[expiry_proposal_row_lock:]
        self.assertRegex(
            locked_expiry,
            r"candidate\.review_state\s*(?:<>|IS\s+DISTINCT\s+FROM)\s*"
            r"'pending_review'",
        )
        self.assertRegex(
            locked_expiry,
            r"candidate\.expires_at\s*>\s*"
            r"pg_catalog\.transaction_timestamp\(\)",
        )
        self.assertIn("CONTINUE", locked_expiry)

        reject_claim_lock = reject.find("'|claim|'")
        reject_proposal_lock = reject.find("'|proposal|'")
        reject_claim_row_lock = reject.find("FOR UPDATE")
        self.assertGreaterEqual(reject_claim_lock, 0)
        self.assertGreater(reject_proposal_lock, reject_claim_lock)
        self.assertGreater(reject_claim_row_lock, reject_proposal_lock)

    def test_review_operation_id_is_bound_to_one_proposal_and_decision(self) -> None:
        review = _function_definitions(self.foundation)[
            "memory_private.review_proposal"
        ]
        operation_lock = review.find("'|operation|'")
        claim_lock = review.find("'|claim|'")
        proposal_lock = review.find("'|proposal|'")
        self.assertGreaterEqual(operation_lock, 0)
        self.assertGreater(claim_lock, operation_lock)
        self.assertGreater(proposal_lock, claim_lock)
        proposal_row_lock = review.find("FOR UPDATE", proposal_lock)
        self.assertGreater(proposal_row_lock, proposal_lock)
        locked_review = review[proposal_row_lock:]
        self.assertRegex(
            locked_review,
            r"proposal\.operation_id\s+(?:<>|IS\s+DISTINCT\s+FROM)\s+"
            r"p_operation_id",
        )
        self.assertIn("proposal.review_state <> p_decision", locked_review)

    def test_owner_mutation_operation_ids_are_globally_reserved_after_exact_replay(self) -> None:
        signature = (
            "memory_private.operation_id_conflicts("
            "uuid,uuid,uuid,uuid,uuid,text[])"
        )
        self.assertIn(signature, self.contract["internal_functions"])
        definitions = _function_definitions(self.foundation)
        conflicts = definitions["memory_private.operation_id_conflicts"]
        for table in (
            "evidence",
            "extraction_job",
            "provider_call",
            "proposal",
            "projection_outbox",
            "answer_binding",
            "claim_deletion_receipt",
            "audit_event",
        ):
            with self.subTest(operation_surface=table):
                self.assertRegex(conflicts, rf"FROM\s+memory\.{table}\s+AS\s+value")
        self.assertGreaterEqual(conflicts.count("owner_user_id = p_owner_user_id"), 8)
        self.assertGreaterEqual(conflicts.count("operation_id = p_operation_id"), 8)
        for allowed in (
            "p_allowed_proposal_id",
            "p_allowed_outbox_id",
            "p_allowed_claim_id",
            "p_allowed_transition_codes",
        ):
            self.assertIn(allowed, conflicts)

        gates = (
            ("memory_private.review_proposal", "UPDATE memory.proposal"),
            ("memory_private.correct_claim", "INSERT INTO memory.evidence"),
            ("memory_private.retract_claim", "UPDATE memory.claim"),
            ("memory_private.request_claim_deletion", "UPDATE memory.claim"),
            ("memory_private.finalize_claim_deletion", "DELETE FROM memory.claim"),
            ("memory_private.record_answer_binding", "INSERT INTO memory.answer_binding"),
        )
        for function_name, first_mutation in gates:
            definition = definitions[function_name]
            operation_lock = definition.find("'|operation|'")
            conflict = definition.find("memory_private.operation_id_conflicts(")
            mutation = definition.find(first_mutation)
            with self.subTest(operation_gate=function_name):
                self.assertGreaterEqual(operation_lock, 0)
                self.assertGreater(conflict, operation_lock)
                self.assertIn("RETURN;", definition[operation_lock:conflict])
                self.assertGreater(mutation, conflict)

    def test_completed_extraction_replay_revalidates_full_proposal_content(self) -> None:
        complete = _function_definitions(self.foundation)[
            "memory_private.complete_extraction"
        ]
        replay_return = complete.find("RETURN QUERY SELECT 'replayed'::text")
        self.assertGreaterEqual(replay_return, 0)
        replay_prefix = complete[:replay_return]
        for required in (
            "stored_item := pg_catalog.jsonb_build_object(",
            "item IS DISTINCT FROM stored_item",
            "memory_private.validated_fact_sha256(",
            "memory_private.semantic_key_sha256(",
            "memory_private.proposal_sha256(",
            "memory_private.uuid5(",
            "item->>'proposal_sha256'",
            "stored_proposal.proposal_sha256",
        ):
            with self.subTest(replay_validation=required):
                self.assertIn(required, replay_prefix)
        stored_item_block = replay_prefix.split(
            "stored_item := pg_catalog.jsonb_build_object(", 1
        )[1].split(");", 1)[0]
        for field in COMPLETE_EXTRACTION_ITEM_FIELDS:
            with self.subTest(stored_replay_field=field):
                self.assertIn(f"'{field}'", stored_item_block)
        self.assertTrue(
            re.search(
                r"computed_proposal_sha\s+(?:<>|IS\s+DISTINCT\s+FROM)\s+"
                r"stored_proposal\.proposal_sha256",
                replay_prefix,
            )
            or re.search(
                r"stored_proposal\.proposal_sha256\s+"
                r"(?:<>|IS\s+DISTINCT\s+FROM)\s+computed_proposal_sha",
                replay_prefix,
            )
        )
        self.assertRegex(
            replay_prefix,
            r"stored_item_count\s*(?:<>|IS\s+DISTINCT\s+FROM)\s*item_count",
        )

    def test_proposal_updates_can_only_resolve_review_metadata_once(self) -> None:
        trigger = re.search(
            r"CREATE\s+TRIGGER\s+[a-z][a-z0-9_]*\s+"
            r"BEFORE\s+UPDATE\s+OR\s+DELETE\s+ON\s+memory\.proposal\s+"
            r"FOR\s+EACH\s+ROW\s+EXECUTE\s+FUNCTION\s+"
            r"(memory_private\.[a-z][a-z0-9_]*)\(\)\s*;",
            self.foundation,
            flags=re.IGNORECASE,
        )
        self.assertIsNotNone(trigger, "proposal lacks its update/delete guard")
        definitions = _function_definitions(self.foundation)
        guard = definitions[trigger.group(1).lower()]

        review_fields = (
            "review_state",
            "reviewer_kind",
            "reviewer_user_id",
            "review_reason_codes",
            "reviewed_at",
        )
        proposal_columns = set(
            re.findall(
                r"^\s{2}([a-z][a-z0-9_]*)\s+"
                r"(?:uuid|text|smallint|integer|boolean|jsonb|text\[\]|timestamptz)\b",
                _table_definition(self.foundation, "memory.proposal"),
                flags=re.IGNORECASE | re.MULTILINE,
            )
        )
        immutable_fields = set(
            re.findall(
                r"OLD\.([a-z][a-z0-9_]*)\s+IS\s+DISTINCT\s+FROM\s+"
                r"NEW\.\1",
                guard,
                flags=re.IGNORECASE,
            )
        )
        self.assertEqual(
            immutable_fields,
            proposal_columns - set(review_fields),
        )
        self.assertRegex(guard, r"OLD\.review_state\s*<>\s*'pending_review'")
        transition_match = re.search(
            r"NEW\.review_state\s+NOT\s+IN\s*\((.*?)\)",
            guard,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(transition_match)
        self.assertEqual(
            set(re.findall(r"'([a-z_]+)'", transition_match.group(1))),
            {"admitted", "rejected", "expired"},
        )
        self.assertRegex(guard, r"TG_OP\s*=\s*'DELETE'")
        self.assertRegex(guard, r"session_user\s*<>\s*'governed_memory_worker'")
        self.assertIn("app.memory_hard_delete_operation_id", guard)

    def test_every_owner_table_enables_and_forces_rls(self) -> None:
        for table in self.contract["owner_bearing_tables"]:
            escaped = re.escape(f"memory.{table}")
            with self.subTest(table=table):
                self.assertRegex(
                    self.foundation,
                    rf"ALTER\s+TABLE\s+{escaped}\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY\s*;",
                )
                self.assertRegex(
                    self.foundation,
                    rf"ALTER\s+TABLE\s+{escaped}\s+FORCE\s+ROW\s+LEVEL\s+SECURITY\s*;",
                )
                self.assertRegex(
                    self.foundation,
                    rf"CREATE\s+POLICY\s+[a-z0-9_]+\s+ON\s+{escaped}\b",
                )

    def test_declared_function_names_are_exactly_created(self) -> None:
        combined = "\n".join(
            (self.foundation, self.claim_detail, self.pilot_marker, self.bridge)
        )
        declared = {
            signature.split("(", 1)[0]
            for signature in (
                list(self.contract["function_surface"])
                + list(self.contract["internal_functions"])
                + list(self.contract["bridge"]["functions"])
                + list(self.contract["bridge"]["internal_functions"])
            )
        }
        observed = set(_function_definitions(combined))
        self.assertEqual(observed, declared)

    def test_function_security_modes_and_search_paths_are_explicit(self) -> None:
        combined = "\n".join(
            (self.foundation, self.claim_detail, self.pilot_marker, self.bridge)
        )
        definitions = _function_definitions(combined)
        self.assertTrue(definitions)
        for name, definition in definitions.items():
            with self.subTest(name=name):
                self.assertRegex(
                    definition,
                    r"\bSET\s+search_path\s*(?:=|TO)\s*pg_catalog\b",
                )

        current_owner = definitions["memory_private.current_owner_id"]
        self.assertRegex(current_owner, r"\bSECURITY\s+INVOKER\b")
        self.assertNotRegex(current_owner, r"\bSECURITY\s+DEFINER\b")

        privileged_surface = {
            signature.split("(", 1)[0]
            for signature in (
                list(self.contract["function_surface"])
                + list(self.contract["bridge"]["functions"])
            )
            if not signature.startswith("memory_private.current_owner_id(")
        }
        for name in privileged_surface:
            with self.subTest(privileged_function=name):
                self.assertRegex(
                    definitions[name],
                    r"\bSECURITY\s+DEFINER\b",
                )

    def test_roles_are_non_superuser_non_bypass_and_receive_no_table_dml(self) -> None:
        for role_contract in self.contract["roles"].values():
            role = role_contract["name"]
            with self.subTest(role=role):
                self.assertIn(role, self.roles + self.foundation)
                self.assertFalse(role_contract["inherit"])
                self.assertFalse(role_contract["superuser"])
                self.assertFalse(role_contract["bypassrls"])
        combined = "\n".join(
            (
                self.roles,
                self.foundation,
                self.claim_detail,
                self.pilot_marker,
                self.bridge,
            )
        )
        self.assertRegex(
            self.roles,
            r"role\.rolcanlogin\s*=\s*role_record\.must_login",
        )
        for attribute in (
            "rolsuper",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
            "rolinherit",
        ):
            with self.subTest(attribute=attribute):
                self.assertRegex(self.roles, rf"NOT\s+role\.{attribute}\b")
        for role in (
            self.contract["roles"]["api"]["name"],
            self.contract["roles"]["worker"]["name"],
            self.contract["roles"]["ingest_writer"]["name"],
        ):
            self.assertNotRegex(
                combined,
                rf"GRANT\s+(?:SELECT|INSERT|UPDATE|DELETE|ALL)\b[^;]*\bTO\s+{re.escape(role)}\b",
            )

    def test_bridge_contains_hashes_and_lineage_but_no_raw_content_columns(self) -> None:
        self.assertRegex(
            self.bridge,
            r"\bCREATE\s+TABLE\s+memory_ingest_private\.memory_ingest_outbox\b",
        )
        for required in (
            "owner_user_id",
            "message_id",
            "thread_id",
            "content_sha256",
            "source_binding_sha256",
            "exchange_id",
            "window_id",
            "window_sha256",
        ):
            self.assertIn(required, self.bridge)
        for prohibited in ("message_text", "attachment_text", "raw_content", "selected_text"):
            self.assertNotIn(prohibited, self.bridge.lower())

    def test_bridge_enqueue_binds_exact_window_lineage_and_auth_context(self) -> None:
        expected_signature = "memory_ingest_private.enqueue_chat_log_message(uuid,text)"
        self.assertIn(expected_signature, self.contract["bridge"]["functions"])
        enqueue = _function_definitions(self.bridge)[
            "memory_ingest_private.enqueue_chat_log_message"
        ]
        signature = enqueue.split("RETURNS", 1)[0]
        for prohibited in (
            "p_owner_user_id",
            "p_thread_id",
            "p_content_sha256",
            "p_source_created_at",
            "p_exchange_id",
            "p_window_id",
            "p_window_ordinal",
            "p_window_sha256",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, signature)
        self.assertIn("session_user <> 'brains_app'", enqueue)
        self.assertRegex(
            enqueue,
            r"FROM\s+public\.chat_log\s+AS\s+source",
        )
        self.assertRegex(enqueue, r"source\.owner_user_id\s*=\s*actor")
        self.assertIn("normalize(source_row.text, NFC)", enqueue)
        self.assertIn("memory_ingest_private.ingest_window_sha256(", enqueue)
        self.assertRegex(enqueue, r"p_message_id,\s*p_message_id,\s*0")
        self.assertRegex(
            enqueue,
            r"COALESCE\(\s*pg_catalog\.current_setting\("
            r"'app\.auth_context_sha256',\s*true\),\s*''\s*\)\s*!~",
        )
        self.assertRegex(
            enqueue,
            r"existing\.window_id\s*<>\s*p_message_id",
        )
        self.assertRegex(
            enqueue,
            r"existing\.window_sha256\s*<>\s*window_hash",
        )

    def test_bridge_cutover_is_transaction_owned_and_cannot_be_backfilled(self) -> None:
        enqueue = _function_definitions(self.bridge)[
            "memory_ingest_private.enqueue_chat_log_message"
        ]
        signature = enqueue.split("RETURNS", 1)[0]
        self.assertNotIn("p_ingest_after", signature)
        self.assertNotIn("p_source_created_at", signature)
        self.assertIn("pg_catalog.transaction_timestamp()", enqueue)
        self.assertRegex(
            enqueue,
            r"source_row\.created_at\s*<>\s*captured_at",
        )
        self.assertIn("source_row.created_at, p_policy_sha256", enqueue)
        self.assertRegex(
            enqueue,
            r"ingest_after,\s*source_created_at",
        )
        self.assertRegex(
            self.bridge,
            r"source_created_at\s*>=\s*ingest_after",
        )

    def test_bridge_context_review_is_one_leased_mark_then_final_ack(self) -> None:
        mark_signatures = [
            signature
            for signature in self.contract["bridge"]["functions"]
            if signature.startswith(
                "memory_ingest_private.mark_memory_ingest_context_review("
            )
        ]
        self.assertEqual(
            mark_signatures,
            ["memory_ingest_private.mark_memory_ingest_context_review(uuid,uuid)"],
        )
        definitions = _function_definitions(self.bridge)
        mark = definitions[
            "memory_ingest_private.mark_memory_ingest_context_review"
        ]
        self.assertRegex(mark, r"target\.state\s*<>\s*'claimed'")
        self.assertRegex(mark, r"target\.lease_token\s*<>\s*p_lease_token")
        self.assertRegex(
            mark,
            r"target\.lease_expires_at\s*<=\s*pg_catalog\.clock_timestamp\(\)",
        )
        self.assertRegex(mark, r"target\.context_review_count\s*<>\s*0")
        self.assertRegex(mark, r"SET\s+context_review_count\s*=\s*1")
        self.assertRegex(
            mark,
            r"eligibility_decision\s*=\s*'review_context'",
        )

        ack = definitions["memory_ingest_private.ack_memory_ingest"]
        self.assertRegex(ack, r"p_decision\s*=\s*'review_context'")
        self.assertRegex(
            ack,
            r"p_decision\s+NOT\s+IN\s*\(\s*'send_external',"
            r"\s*'skip_zero_call',\s*'route_internal',\s*'block_local'\s*\)",
        )
        self.assertIn("content_sha256 = NULL", ack)

    def test_same_transaction_replay_and_leased_reader_revalidate_source(self) -> None:
        definitions = _function_definitions(self.bridge)
        binding_functions = {
            name: definition
            for name, definition in definitions.items()
            if "governed_memory.bridge_source.v1" in definition
        }
        self.assertEqual(len(binding_functions), 1)
        binding_name, binding_definition = next(iter(binding_functions.items()))
        binding_fields = (
            "owner_user_id",
            "message_id",
            "thread_id",
            "exchange_id",
            "window_id",
            "window_ordinal",
            "window_sha256",
            "content_sha256",
            "policy_sha256",
            "source_created_at",
        )
        positions = [
            binding_definition.find(f"'{field}'") for field in binding_fields
        ]
        self.assertTrue(all(position >= 0 for position in positions))
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("governed_memory.bridge_source.v1|", binding_definition)
        framing_definition = definitions[
            "memory_ingest_private.framed_utf8_field"
        ]
        self.assertRegex(
            framing_definition,
            r"(?s)octet_length\s*\(\s*pg_catalog\.convert_to\(\s*"
            r"normalize\(p_value,\s*NFC\),\s*'UTF8'\s*\)\s*\)",
        )

        enqueue = _function_definitions(self.bridge)[
            "memory_ingest_private.enqueue_chat_log_message"
        ]
        self.assertIn(binding_name + "(", enqueue)
        self.assertRegex(
            enqueue,
            r"existing\.source_binding_sha256\s*"
            r"(?:<>|IS\s+DISTINCT\s+FROM)\s*[a-z][a-z0-9_]*",
        )
        self.assertNotRegex(
            enqueue,
            r"existing\.state\s+IN\s*\([^)]*\)\s*AND\s+"
            r"existing\.source_binding_sha256",
        )
        self.assertIn("existing.content_sha256 <> content_hash", enqueue)
        self.assertIn("RETURN QUERY SELECT 'replayed'::text", enqueue)
        self.assertNotIn("terminal_replayed", enqueue)
        self.assertRegex(
            enqueue,
            r"sha256\(\s*pg_catalog\.convert_to\(source_row\.text,\s*'UTF8'\)",
        )
        reader = definitions[
            "memory_ingest_private.read_leased_chat_log_message"
        ]
        self.assertIn("session_user <> 'governed_memory_worker'", reader)
        self.assertRegex(reader, r"target\.lease_token\s*<>\s*p_lease_token")
        self.assertIn("FROM public.chat_log AS source", reader)
        self.assertIn("observed_source_binding_sha256", reader)
        self.assertIn("FROM public.chat_attachments AS attachment", reader)
        self.assertIn("attachment.message_id = target.message_id", reader)
        self.assertNotRegex(
            reader,
            r"attachment\.(?:content|filename|media_type|content_sha256)",
        )
        self.assertRegex(
            self.bridge,
            r"state\s+IN\s*\(\s*'completed',\s*'skipped',\s*'expired',"
            r"\s*'failed_terminal',\s*'erasure_cancelled'\s*\)\s+AND\s+"
            r"completed_at\s+IS\s+NOT\s+NULL"
            r"\s+AND\s+content_sha256\s+IS\s+NULL",
        )
        table_definition = self.bridge.split(
            "CREATE TABLE memory_ingest_private.memory_ingest_outbox", 1
        )[1].split(");", 1)[0]
        self.assertRegex(
            table_definition,
            r"source_binding_sha256\s+text\s+NOT\s+NULL",
        )
        self.assertRegex(
            table_definition,
            r"source_binding_sha256\s*~\s*'\^\[0-9a-f\]\{64\}\$'",
        )
        self.assertNotRegex(
            self.bridge,
            r"source_binding_sha256\s*=\s*NULL",
        )

    def test_bridge_denies_anon_and_authenticated_per_privilege(self) -> None:
        postflight = self.bridge.split("DO $postflight$", 1)[1]
        self.assertRegex(
            postflight,
            r"ARRAY\[\s*'anon',\s*'authenticated'\s*\]",
        )
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            with self.subTest(privilege=privilege):
                self.assertRegex(
                    postflight,
                    r"pg_catalog\.has_table_privilege\(\s*forbidden_role,"
                    r"\s*'memory_ingest_private\.memory_ingest_outbox',\s*'"
                    + privilege
                    + r"'\s*\)",
                )

    def test_source_erasure_is_exact_chat_only_and_preserves_structured_data(
        self,
    ) -> None:
        bridge_contract = self.contract["bridge"]
        self.assertEqual(
            bridge_contract["source_erasure_selectors"],
            ["thread", "message_tail", "recent", "all_conversations"],
        )
        combined = f"{self.foundation}\n{self.bridge}".lower()
        for forbidden in (
            "all_memory",
            "all_governed_memory",
            "public.accounts",
            "public.libraries",
            "public.workouts",
            "public.weightlifting_sessions",
            "public.daily_food_logs",
            "public.food_logs",
            "public.measurements",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined)

        definitions = _function_definitions(self.bridge)
        begin = definitions["memory_ingest_private.begin_source_erasure"]
        self.assertIn("session_user <> 'governed_memory_api'", begin)
        for selector in bridge_contract["source_erasure_selectors"]:
            self.assertIn(f"'{selector}'", begin)
        finalize = definitions[
            "memory_ingest_private.finalize_source_erasure"
        ]
        for exact_target in (
            "memory_ingest_private.memory_ingest_outbox",
            "public.chat_attachments",
            "public.chat_log",
            "public.threads",
        ):
            self.assertRegex(
                finalize,
                rf"DELETE\s+FROM\s+{re.escape(exact_target)}\b",
            )
        delete_target_pattern = re.compile(
            r"\bDELETE\s+FROM\s+([a-z][a-z0-9_.]*)",
            flags=re.IGNORECASE,
        )
        self.assertEqual(
            {
                target.lower()
                for target in delete_target_pattern.findall(finalize)
            },
            {
                "memory_ingest_private.memory_ingest_outbox",
                "public.chat_attachments",
                "public.chat_log",
                "public.threads",
            },
            "conversation erasure executable SQL must remain a closed "
            "chat-only allowlist",
        )
        ack = definitions[
            "memory_ingest_private.ack_source_erasure_completion"
        ]
        self.assertEqual(
            {
                target.lower()
                for target in delete_target_pattern.findall(ack)
            },
            {
                "memory_ingest_private.source_erasure_target",
                "memory_ingest_private.source_erasure_thread_target",
            },
            "completion acknowledgement may purge only transient target rows",
        )

        foundation_definitions = _function_definitions(self.foundation)
        governed_delete_allowlists = {
            "memory_private.finalize_claim_deletion": {
                "memory.answer_binding",
                "memory.claim",
                "memory.claim_evidence",
                "memory.claim_revision",
                "memory.entity",
                "memory.evidence",
                "memory.extraction_job",
                "memory.projection_outbox",
                "memory.proposal",
                "memory.provider_call",
            },
            "memory_private.finalize_source_erasure_memory": {
                "memory.answer_binding",
                "memory.evidence",
                "memory.extraction_job",
                "memory.proposal",
                "memory.provider_call",
            },
            "memory_private.ack_source_erasure_conversation_deleted": {
                "memory.source_erasure_claim",
                "memory.source_erasure_target",
            },
        }
        for function_name, allowed_targets in governed_delete_allowlists.items():
            with self.subTest(function=function_name):
                self.assertEqual(
                    {
                        target.lower()
                        for target in delete_target_pattern.findall(
                            foundation_definitions[function_name]
                        )
                    },
                    allowed_targets,
                    "source erasure may delete only chat-derived "
                    "governed-memory rows and transient target rows",
                )
        self.assertIn("memory_erasure_requester", begin)
        self.assertIn("memory_private.current_owner_id()", self.foundation)
        self.assertTrue(
            bridge_contract["source_erasure_content_free_receipts_retained"]
        )
        for role in ("anon", "authenticated"):
            with self.subTest(role=role):
                self.assertNotRegex(
                    self.bridge,
                    rf"GRANT\s+(?:USAGE|EXECUTE|SELECT|INSERT|UPDATE|DELETE|ALL)"
                    rf"\b[^;]*\bTO\s+{role}\b",
                )

    def test_source_erasure_catalog_and_final_delete_are_fail_closed(
        self,
    ) -> None:
        definitions = _function_definitions(self.bridge)
        assertion_name = (
            "memory_ingest_private.assert_chat_deletion_catalog"
        )
        self.assertIn(assertion_name, definitions)
        self.assertIn(
            f"{assertion_name}()",
            self.contract["bridge"]["internal_functions"],
        )
        catalog = definitions[assertion_name]
        self.assertIn(
            "pg_catalog.has_table_privilege(\n"
            "            'brains_app', expected.relation_oid, 'DELETE'",
            catalog,
        )
        self.assertIn(
            "'INSERT:false', 'SELECT:false', 'UPDATE:false'", catalog
        )
        for relation in (
            "public.chat_log",
            "public.threads",
            "public.chat_attachments",
            "public.active_thread_selection",
            "trusted_web.response_transcript_v1",
            "memory_ingest_private.memory_ingest_outbox",
            "memory_ingest_private.source_erasure_operation",
            "memory_ingest_private.source_erasure_target",
            "memory_ingest_private.source_erasure_thread_target",
            "memory_ingest_private.source_erasure_message_tombstone",
            "memory_ingest_private.source_erasure_thread_tombstone",
            "memory_ingest_private.source_erasure_receipt",
        ):
            with self.subTest(relation=relation):
                self.assertIn(relation, catalog)
        for constraint_name in (
            "chat_log_owner_thread_fk",
            "chat_attachments_thread_owner_fk",
            "chat_attachments_message_owner_thread_fk",
            "active_thread_selection_owner_thread_fk",
            "response_transcript_v1_user_chat_log_id_fkey",
            "response_transcript_v1_assistant_chat_log_id_fkey",
            "source_erasure_target_operation_fk",
            "source_erasure_thread_target_operation_fk",
            "source_erasure_message_tombstone_operation_fk",
            "source_erasure_thread_tombstone_operation_fk",
        ):
            with self.subTest(constraint=constraint_name):
                self.assertIn(constraint_name, catalog)
        for closed_catalog_surface in (
            "pg_catalog.pg_constraint",
            "constraint_row.confdeltype",
            "constraint_row.convalidated",
            "pg_catalog.pg_trigger",
            "trigger_row.tgtype",
            "pg_catalog.pg_rewrite",
            "rewrite_row.ev_class = ANY(deletion_roots)",
            "pg_catalog.pg_inherits",
            "unclassified inbound chat deletion dependency",
            "chat deletion mutation trigger inventory differs",
            "chat deletion private constraint inventory differs",
        ):
            with self.subTest(surface=closed_catalog_surface):
                self.assertIn(closed_catalog_surface, catalog)
        self.assertIn("trigger_row.tgenabled = expected.enabled_state", catalog)
        self.assertIn("trigger_row.tgfoid = expected.function_oid", catalog)
        self.assertIn(
            "response_transcript_serialize_source_erasure", catalog
        )
        self.assertIn("source_erasure_message_tombstone_immutable", catalog)
        self.assertIn("source_erasure_thread_tombstone_immutable", catalog)
        self.assertIn(
            "'chat_log_enqueue_memory_v1_consolidation'",
            catalog,
        )
        self.assertIn(
            "legacy chat capture trigger remains after migration",
            self.bridge,
        )
        for composite_lineage in (
            r"ARRAY\[\s*'user_chat_log_id',\s*'owner_user_id',"
            r"\s*'thread_id'\s*\]::text\[\]",
            r"ARRAY\[\s*'assistant_chat_log_id',\s*'owner_user_id',"
            r"\s*'thread_id'\s*\]::text\[\]",
            r"ARRAY\[\s*'id',\s*'owner_user_id',\s*'thread_id'"
            r"\s*\]::text\[\]",
        ):
            with self.subTest(composite_lineage=composite_lineage):
                self.assertRegex(catalog, composite_lineage)
        self.assertNotIn("project_thread", catalog)
        self.assertNotRegex(catalog, r"<>\s+CASE\s+WHEN")
        self.assertNotRegex(
            self.bridge,
            r"(?m)^\s*IF[^\n]*[+<>=]\s+CASE\s+WHEN",
        )
        self.assertRegex(
            self.bridge,
            r"observed_count\s*<>\s*6\s*\+\s*\(\s*CASE\s+WHEN\s+"
            r"pg_catalog\.to_regclass\(\s*"
            r"'trusted_web\.response_transcript_v1'\s*\)\s+IS\s+NULL\s+"
            r"THEN\s+0\s+ELSE\s+1\s+END\s*\)",
        )
        for counter, relation in (
            ("active_edge_count", "public.active_thread_selection"),
            ("trusted_user_edge_count", "trusted_web.response_transcript_v1"),
            (
                "trusted_assistant_edge_count",
                "trusted_web.response_transcript_v1",
            ),
        ):
            with self.subTest(parenthesized_case=counter):
                self.assertRegex(
                    catalog,
                    rf"{counter}\s*<>\s*\(\s*CASE\s+WHEN\s+"
                    rf"pg_catalog\.to_regclass\('{relation}'\)\s+IS\s+NULL\s+"
                    r"THEN\s+0\s+ELSE\s+1\s+END\s*\)",
                )

        begin = definitions["memory_ingest_private.begin_source_erasure"]
        self.assertLess(
            begin.index("assert_chat_deletion_catalog"),
            begin.index(
                "INSERT INTO memory_ingest_private.source_erasure_operation"
            ),
        )
        self.assertIn("source erasure selector has future-dated chat rows", begin)
        self.assertIn("source erasure attachment target limit exceeded", begin)
        self.assertIn("observed_attachment_count > 1000000", begin)
        attachment_limit = begin.index(
            "IF observed_attachment_count > 1000000"
        )
        selective_attachment_bound = begin[
            begin.rfind("  ELSE", 0, attachment_limit):attachment_limit
        ]
        for selective_bound_lineage in (
            "attachment.owner_user_id = actor",
            "target.owner_user_id = actor",
            "target.operation_id = p_operation_id",
            "target.thread_id = attachment.thread_id",
            "target.message_id = attachment.message_id",
            "target_thread.owner_user_id = actor",
            "target_thread.operation_id = p_operation_id",
            "target_thread.thread_id = attachment.thread_id",
            "remaining.owner_user_id = actor",
            "remaining.thread_id = target_thread.thread_id",
            "remaining_target.owner_user_id = actor",
            "remaining_target.operation_id = p_operation_id",
            "remaining_target.thread_id = remaining.thread_id",
            "remaining_target.message_id = remaining.id",
        ):
            with self.subTest(
                selective_bound_lineage=selective_bound_lineage
            ):
                self.assertIn(
                    selective_bound_lineage, selective_attachment_bound
                )
        self.assertGreaterEqual(
            selective_attachment_bound.count("EXISTS ("), 4
        )
        self.assertRegex(
            begin,
            r"(?s)p_selector_kind\s*=\s*'message_tail'.*?"
            r"source\.created_at\s+IS\s+NULL.*?"
            r"message-tail erasure has invalid chat time",
        )
        self.assertRegex(
            begin,
            r"(?s)p_selector_kind\s*=\s*'recent'.*?"
            r"source\.created_at\s+IS\s+NULL.*?"
            r"recent erasure has invalid chat time",
        )

        finalize = definitions[
            "memory_ingest_private.finalize_source_erasure"
        ]
        first_lock = finalize.index("LOCK TABLE public.threads")
        catalog_assertion = finalize.index("assert_chat_deletion_catalog")
        first_delete = finalize.index("DELETE FROM")
        self.assertLess(first_lock, catalog_assertion)
        self.assertLess(catalog_assertion, first_delete)
        for locked_root in (
            "LOCK TABLE public.threads IN ROW EXCLUSIVE MODE",
            "LOCK TABLE public.chat_log IN ROW EXCLUSIVE MODE",
            "LOCK TABLE public.chat_attachments IN ROW EXCLUSIVE MODE",
            "LOCK TABLE public.active_thread_selection IN ROW EXCLUSIVE MODE",
            "LOCK TABLE trusted_web.response_transcript_v1 IN ROW EXCLUSIVE MODE",
            "LOCK TABLE memory_ingest_private.memory_ingest_outbox",
            "LOCK TABLE memory_ingest_private.source_erasure_operation",
            "LOCK TABLE memory_ingest_private.source_erasure_target",
            "LOCK TABLE memory_ingest_private.source_erasure_thread_target",
            "LOCK TABLE memory_ingest_private.source_erasure_message_tombstone",
            "LOCK TABLE memory_ingest_private.source_erasure_thread_tombstone",
            "LOCK TABLE memory_ingest_private.source_erasure_receipt",
        ):
            with self.subTest(lock=locked_root):
                self.assertIn(locked_root, finalize)
        self.assertIn("remaining_transcript", finalize)
        self.assertIn(
            "target.message_id = transcript.user_chat_log_id", finalize
        )
        self.assertIn(
            "target.message_id = transcript.assistant_chat_log_id", finalize
        )
        message_tombstone_insert = finalize.index(
            "INSERT INTO memory_ingest_private."
            "source_erasure_message_tombstone"
        )
        target_attachment_delete = finalize.index(
            "DELETE FROM public.chat_attachments AS attachment\n"
            "  USING memory_ingest_private.source_erasure_target AS target"
        )
        target_chat_delete = finalize.index(
            "DELETE FROM public.chat_log AS source"
        )
        thread_tombstone_insert = finalize.index(
            "INSERT INTO memory_ingest_private."
            "source_erasure_thread_tombstone"
        )
        empty_thread_attachment_delete = finalize.index(
            "DELETE FROM public.chat_attachments AS attachment",
            thread_tombstone_insert,
        )
        empty_thread_delete = finalize.index(
            "DELETE FROM public.threads AS thread",
            empty_thread_attachment_delete,
        )
        bridge_delete = finalize.index(
            "DELETE FROM memory_ingest_private.memory_ingest_outbox AS bridge"
        )
        self.assertLess(message_tombstone_insert, target_attachment_delete)
        self.assertLess(target_attachment_delete, target_chat_delete)
        self.assertLess(target_chat_delete, thread_tombstone_insert)
        self.assertLess(thread_tombstone_insert, empty_thread_attachment_delete)
        self.assertLess(empty_thread_attachment_delete, empty_thread_delete)
        self.assertLess(empty_thread_delete, bridge_delete)
        thread_tombstone_block = finalize[
            thread_tombstone_insert:empty_thread_attachment_delete
        ]
        self.assertIn(
            "FROM memory_ingest_private.source_erasure_thread_target AS target",
            thread_tombstone_block,
        )
        self.assertIn(
            "remaining.thread_id = target.thread_id",
            thread_tombstone_block,
        )
        self.assertNotIn(
            "remaining.owner_user_id", thread_tombstone_block
        )
        absence_proof = finalize[
            finalize.index("IF remaining_transcript"):finalize.index(
                "RAISE EXCEPTION 'conversation source absence verification failed'"
            )
        ]
        self.assertIn(
            "source.id = target.message_id", absence_proof
        )
        self.assertIn(
            "attachment.message_id = target.message_id", absence_proof
        )
        self.assertIn("bridge.message_id = target.message_id", absence_proof)
        self.assertIn("JOIN public.threads AS thread", absence_proof)
        self.assertIn(
            "JOIN public.chat_attachments AS attachment", absence_proof
        )
        self.assertIn("source_erasure_thread_tombstone AS tombstone", absence_proof)
        self.assertIn("governed_memory.conversation_source_erasure_receipt.v2", finalize)
        for receipt_binding in (
            "thread_target_manifest_sha256",
            "thread_target_count",
            "message_tombstone_count",
            "thread_tombstone_count",
            "tombstone_manifest_sha256",
        ):
            self.assertIn(receipt_binding, finalize)
        self.assertIn("removed_messages <> operation.target_count", finalize)
        self.assertIn(
            "removed_threads <> thread_tombstone_count", finalize
        )
        self.assertNotRegex(
            finalize,
            r"DELETE\s+FROM\s+memory_ingest_private\.source_erasure_(?:thread_)?target",
        )

        chat_fence = definitions[
            "memory_ingest_private.serialize_chat_source_erasure"
        ]
        self.assertGreaterEqual(chat_fence.count("NEW.created_at IS NULL"), 3)

        thread_fence = definitions[
            "memory_ingest_private.serialize_thread_source_erasure"
        ]
        for lineage in (
            "NEW.owner_user_id",
            "NEW.id",
            "NEW.created_at",
            "OLD.owner_user_id",
            "OLD.id",
            "OLD.created_at",
        ):
            with self.subTest(thread_lineage=lineage):
                self.assertIn(lineage, thread_fence)

        transcript_fence = definitions[
            "memory_ingest_private.serialize_response_transcript_source_erasure"
        ]
        for lineage in (
            "NEW.owner_user_id",
            "NEW.user_chat_log_id",
            "NEW.assistant_chat_log_id",
            "OLD.owner_user_id",
            "OLD.user_chat_log_id",
            "OLD.assistant_chat_log_id",
            "target.message_id",
            "operation.state <> 'completed'",
        ):
            with self.subTest(transcript_lineage=lineage):
                self.assertIn(lineage, transcript_fence)

        attachment_fence = definitions[
            "memory_ingest_private.serialize_attachment_source_erasure"
        ]
        for lineage in (
            "OLD.owner_user_id",
            "OLD.thread_id",
            "OLD.message_id",
            "NEW.owner_user_id",
            "NEW.thread_id",
            "NEW.message_id",
        ):
            with self.subTest(lineage=lineage):
                self.assertIn(lineage, attachment_fence)
        for suppression_fence in (
            "source_erasure_message_tombstone",
            "source_erasure_thread_tombstone",
            "source_erasure_target",
            "source_erasure_thread_target",
            "target.message_id = NEW.message_id",
            "target.message_id = OLD.message_id",
            "target.thread_id = NEW.thread_id",
            "target.thread_id = OLD.thread_id",
        ):
            with self.subTest(suppression_fence=suppression_fence):
                self.assertIn(suppression_fence, attachment_fence)

        for thread_suppression_fence in (
            "source_erasure_thread_tombstone",
            "source_erasure_thread_target",
            "target.thread_id = NEW.id",
            "target.thread_id = OLD.id",
            "tombstone.thread_id = NEW.id",
            "tombstone.thread_id = OLD.id",
        ):
            with self.subTest(thread_fence=thread_suppression_fence):
                self.assertIn(thread_suppression_fence, thread_fence)

        lease = definitions[
            "memory_ingest_private.lease_source_erasure"
        ]
        self.assertIn("coordinator_attempts_exhausted", lease)
        self.assertRegex(
            lease,
            r"value\.attempt_count\s*<\s*1000\s*"
            r"OR\s+value\.state\s*=\s*'conversation_deleted_pending_ack'",
        )

        self.assertRegex(
            lease,
            r"WHEN\s+value\.state\s*=\s*'conversation_deleted_pending_ack'"
            r"\s+AND\s+value\.attempt_count\s*>=\s*1000\s+THEN\s+1000",
        )

        rollback_assertion = self.bridge_rollback.index(
            "assert_chat_deletion_catalog"
        )
        rollback_drop = self.bridge_rollback.index(
            "DROP FUNCTION memory_ingest_private.assert_chat_deletion_catalog"
        )
        self.assertLess(rollback_assertion, rollback_drop)
        self.assertIn(
            "DROP TRIGGER response_transcript_serialize_source_erasure",
            self.bridge_rollback,
        )
        self.assertIn(
            "DROP FUNCTION\n"
            "  memory_ingest_private."
            "serialize_response_transcript_source_erasure()",
            self.bridge_rollback,
        )
        for rollback_object in (
            "source_erasure_thread_target",
            "source_erasure_message_tombstone",
            "source_erasure_thread_tombstone",
            "source_erasure_message_tombstone_immutable",
            "source_erasure_thread_tombstone_immutable",
        ):
            self.assertIn(rollback_object, self.bridge_rollback)

    def test_source_erasure_delete_reinsert_identity_is_fail_closed(
        self,
    ) -> None:
        definitions = _function_definitions(self.bridge)
        chat_fence = definitions[
            "memory_ingest_private.serialize_chat_source_erasure"
        ]
        self.assertRegex(
            chat_fence,
            r"(?s)FROM memory_ingest_private\.source_erasure_target AS target"
            r"\s+WHERE target\.owner_user_id = operation\.owner_user_id"
            r"\s+AND target\.operation_id = operation\.operation_id"
            r"\s+AND target\.message_id = NEW\.id",
        )

        finalize = definitions[
            "memory_ingest_private.finalize_source_erasure"
        ]
        drift_error = finalize.index(
            "conversation source target lineage drifted"
        )
        target_attachment_delete = finalize.index(
            "DELETE FROM public.chat_attachments AS attachment"
        )
        self.assertLess(drift_error, target_attachment_delete)
        drift_block = finalize[
            finalize.rfind("IF EXISTS (", 0, drift_error):drift_error
        ]
        for drift_binding in (
            "target.owner_user_id = operation.owner_user_id",
            "target.operation_id = operation.operation_id",
            "ON source.id = target.message_id",
            "source.owner_user_id IS DISTINCT FROM target.owner_user_id",
            "source.thread_id IS DISTINCT FROM target.thread_id",
            "source.created_at IS DISTINCT FROM target.source_created_at",
        ):
            with self.subTest(drift_binding=drift_binding):
                self.assertIn(drift_binding, drift_block)

        target_chat_delete = finalize.index(
            "DELETE FROM public.chat_log AS source"
        )
        target_chat_delete_block = finalize[
            target_chat_delete:finalize.index(
                "GET DIAGNOSTICS removed_messages = ROW_COUNT",
                target_chat_delete,
            )
        ]
        self.assertIn(
            "target.owner_user_id = operation.owner_user_id",
            target_chat_delete_block,
        )
        self.assertIn(
            "target.operation_id = operation.operation_id",
            target_chat_delete_block,
        )
        self.assertIn(
            "source.owner_user_id = target.owner_user_id",
            target_chat_delete_block,
        )
        self.assertIn("source.id = target.message_id", target_chat_delete_block)
        self.assertNotIn("source.thread_id", target_chat_delete_block)

        absence_start = finalize.index("IF remaining_transcript OR EXISTS (")
        absence_proof = finalize[
            absence_start:finalize.index(
                "conversation source absence verification failed",
                absence_start,
            )
        ]
        self.assertRegex(
            absence_proof,
            r"JOIN public\.chat_log AS source\s+ON source\.id = target\.message_id",
        )
        self.assertRegex(
            absence_proof,
            r"JOIN public\.chat_attachments AS attachment\s+"
            r"ON attachment\.message_id = target\.message_id",
        )
        for table_name, identity_column in (
            ("source_erasure_message_tombstone", "message_id"),
            ("source_erasure_thread_tombstone", "thread_id"),
        ):
            table = _table_definition(
                self.bridge,
                f"memory_ingest_private.{table_name}",
            )
            self.assertRegex(
                table,
                rf"^\s*{identity_column}\s+uuid\s+PRIMARY KEY,",
            )
            for required_column in (
                "owner_user_id uuid NOT NULL",
                "operation_id uuid NOT NULL",
                "erased_at timestamptz NOT NULL",
            ):
                self.assertIn(required_column, table)
            for forbidden_metadata in (
                "text",
                "request",
                "selector",
                "attachment",
                "content",
                "created_at",
            ):
                self.assertNotIn(forbidden_metadata, table.lower())

        ack = definitions[
            "memory_ingest_private.ack_source_erasure_completion"
        ]
        thread_target_delete = ack.index(
            "DELETE FROM memory_ingest_private.source_erasure_thread_target"
        )
        message_target_delete = ack.index(
            "DELETE FROM memory_ingest_private.source_erasure_target"
        )
        completed_update = ack.index("SET state = 'completed'")
        self.assertLess(thread_target_delete, message_target_delete)
        self.assertLess(message_target_delete, completed_update)
        self.assertIn("pending-ack source erasure target inventory drifted", ack)
        self.assertIn("pending-ack suppression tombstone receipt drifted", ack)

    def test_source_erasure_registration_drains_pre_fence_writers(self) -> None:
        register = _function_definitions(self.foundation)[
            "memory_private.register_source_erasure"
        ]
        expected_locks = (
            "LOCK TABLE memory.answer_binding IN SHARE ROW EXCLUSIVE MODE",
            "LOCK TABLE memory.claim_evidence IN SHARE ROW EXCLUSIVE MODE",
            "LOCK TABLE memory.projection_outbox IN SHARE ROW EXCLUSIVE MODE",
            "LOCK TABLE memory.proposal IN SHARE ROW EXCLUSIVE MODE",
            "LOCK TABLE memory.claim_revision IN SHARE ROW EXCLUSIVE MODE",
            "LOCK TABLE memory.claim IN SHARE ROW EXCLUSIVE MODE",
            "LOCK TABLE memory.provider_call IN SHARE ROW EXCLUSIVE MODE",
            "LOCK TABLE memory.extraction_job IN SHARE ROW EXCLUSIVE MODE",
            "LOCK TABLE memory.evidence IN SHARE ROW EXCLUSIVE MODE",
            "LOCK TABLE memory.entity IN SHARE ROW EXCLUSIVE MODE",
            "LOCK TABLE memory.source_erasure_claim IN SHARE ROW EXCLUSIVE MODE",
            "LOCK TABLE memory.source_erasure_target IN SHARE ROW EXCLUSIVE MODE",
        )
        lock_positions = tuple(register.index(lock) for lock in expected_locks)
        self.assertEqual(lock_positions, tuple(sorted(lock_positions)))

        replay_return = register.index("RETURN QUERY SELECT 'replayed'::text")
        first_lock = lock_positions[0]
        last_lock = lock_positions[-1]
        catalog_assertion = register.index(
            "assert_source_erasure_deletion_catalog", last_lock
        )
        recheck = register.index("IF EXISTS (", last_lock)
        operation_insert = register.index(
            "INSERT INTO memory.source_erasure_operation"
        )
        self.assertLess(replay_return, first_lock)
        self.assertLess(last_lock, catalog_assertion)
        self.assertLess(catalog_assertion, recheck)
        self.assertLess(last_lock, recheck)
        self.assertLess(recheck, operation_insert)
        self.assertEqual(
            register.count("owner memory source erasure already active"),
            2,
        )

    def test_terminal_bridge_purge_is_worker_only_and_bounded(self) -> None:
        expected_signature = (
            "memory_ingest_private.purge_terminal_memory_ingest(integer)"
        )
        self.assertIn(expected_signature, self.contract["bridge"]["functions"])
        purge = _function_definitions(self.bridge)[
            "memory_ingest_private.purge_terminal_memory_ingest"
        ]
        self.assertRegex(
            purge,
            r"session_user\s*<>\s*'governed_memory_worker'",
        )
        self.assertRegex(
            purge,
            r"p_limit\s+IS\s+NULL\s+OR\s+p_limit\s+NOT\s+BETWEEN\s+1\s+AND\s+1000",
        )
        self.assertRegex(
            purge,
            r"state\s+IN\s*\(\s*'completed',\s*'skipped',\s*'expired',"
            r"\s*'failed_terminal'\s*\)",
        )
        self.assertRegex(
            purge,
            r"purge_after\s*<=\s*pg_catalog\.clock_timestamp\(\)",
        )
        self.assertIn("FOR UPDATE SKIP LOCKED", purge)
        self.assertRegex(purge, r"LIMIT\s+p_limit")
        self.assertRegex(
            purge,
            r"DELETE\s+FROM\s+memory_ingest_private\.memory_ingest_outbox",
        )

    def test_legacy_chat_capture_is_retired_and_empty_rollback_restores_it(
        self,
    ) -> None:
        self.assertIn(
            "DROP TRIGGER chat_log_enqueue_memory_v1_consolidation "
            "ON public.chat_log;",
            self.bridge,
        )
        self.assertIn(
            "legacy chat capture trigger remains after migration",
            self.bridge,
        )
        self.assertIn(
            "CREATE TRIGGER chat_log_enqueue_memory_v1_consolidation",
            self.bridge_rollback,
        )
        self.assertIn(
            "DISABLE TRIGGER chat_log_enqueue_memory_v1_consolidation",
            self.bridge_rollback,
        )
        self.assertIn(
            "conversation bridge rollback did not restore enqueue trigger",
            self.bridge_rollback,
        )

    def test_answer_binding_sql_has_exact_outcomes_limits_and_manifests(self) -> None:
        definitions = _function_definitions(self.foundation)
        record = definitions["memory_private.record_answer_binding"]
        table = _table_definition(self.foundation, "memory.answer_binding")

        outcome_match = re.search(
            r"p_outcome\s+NOT\s+IN\s*\((.*?)\)",
            record,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(outcome_match)
        self.assertEqual(
            set(re.findall(r"'([a-z_]+)'", outcome_match.group(1))),
            set(FINAL_ANSWER_BINDING_OUTCOMES),
        )
        self.assertRegex(
            record,
            r"cardinality\(p_selected_claim_ids\)\s+NOT\s+BETWEEN\s+0\s+AND\s+8",
        )
        self.assertRegex(
            record,
            r"cardinality\(p_selected_claim_ids\)\s*>\s*p_max_records",
        )
        self.assertRegex(
            record,
            r"cardinality\(p_injected_claim_ids\)\s+NOT\s+BETWEEN\s+0\s+AND\s+8",
        )
        self.assertIn(
            f"NOT BETWEEN 1 AND {MEMORY_BLOCK_MAX_UTF8_BYTES}", record
        )
        self.assertIn(
            f"NOT BETWEEN 1 AND {OUTBOUND_REQUEST_MAX_UTF8_BYTES}", record
        )
        self.assertRegex(
            record,
            r"p_injected_claim_ids\s+IS\s+DISTINCT\s+FROM\s+"
            r"p_selected_claim_ids\[",
        )
        self.assertIn("answer_selection_manifest_sha256(", record)
        self.assertIn("answer_injection_manifest_sha256(", record)
        self.assertRegex(
            record,
            r"selection_hash\s*<>\s*p_expected_selection_manifest_sha256",
        )
        self.assertRegex(
            record,
            r"injection_hash\s*<>\s*p_expected_injection_manifest_sha256",
        )
        for required_hash in (
            "p_query_sha256",
            "p_policy_sha256",
            "p_renderer_sha256",
            "p_prompt_sha256",
            "p_expected_selection_manifest_sha256",
        ):
            with self.subTest(required_answer_hash=required_hash):
                self.assertRegex(record, rf"{required_hash}\s+IS\s+NULL")
                self.assertRegex(
                    record,
                    rf"{required_hash}\s*!~\s*'\^\[0-9a-f\]\{{64\}}\$'",
                )
        self.assertRegex(record, r"escaped_segment_occurrences\s*<>\s*1")

        table_outcomes = re.search(
            r"CONSTRAINT\s+answer_binding_outcome\s+CHECK\s*\(\s*"
            r"outcome\s+IN\s*\((.*?)\)",
            table,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(table_outcomes)
        self.assertEqual(
            set(re.findall(r"'([a-z_]+)'", table_outcomes.group(1))),
            set(FINAL_ANSWER_BINDING_OUTCOMES),
        )
        self.assertRegex(
            table,
            rf"memory_block_utf8_bytes\s+BETWEEN\s+1\s+AND\s+"
            rf"{MEMORY_BLOCK_MAX_UTF8_BYTES}",
        )
        self.assertRegex(table, r"escaped_segment_occurrence_count\s*=\s*1")
        for redundant in (
            "dispatch_state",
            "exposed_revision_ids",
            "exposed_revision_sha256s",
        ):
            with self.subTest(redundant_terminal_column=redundant):
                self.assertNotRegex(table, rf"\b{redundant}\b")
                self.assertNotRegex(
                    record,
                    rf"(?s)INSERT\s+INTO\s+memory\.answer_binding\s*\([^)]*"
                    rf"\b{redundant}\b",
                )

    def test_answer_explicit_recall_is_persisted_replayed_and_fail_closed(self) -> None:
        self.assertEqual(
            ANSWER_SELECTION_MANIFEST_FIELDS[6], "explicit_recall"
        )
        self.assertIs(
            ANSWER_MANIFEST_TEST_VECTORS["selection"]["arguments"][
                "explicit_recall"
            ],
            False,
        )
        self.assertIn(
            "memory_private.record_answer_binding("
            "uuid,uuid,uuid,text,text,text[],text[],text[],integer,integer,"
            "text,text,boolean,uuid[],uuid[],text,text,text,text,text)",
            self.contract["function_surface"],
        )

        definitions = _function_definitions(self.foundation)
        digest = definitions[
            "memory_private.answer_selection_manifest_sha256"
        ]
        record = definitions["memory_private.record_answer_binding"]
        table = _table_definition(self.foundation, "memory.answer_binding")

        self.assertRegex(digest, r"\bp_explicit_recall\s+boolean\b")
        self.assertRegex(
            digest,
            r"(?s)framed_utf8_field\(\s*'explicit_recall',.*?"
            r"p_explicit_recall.*?\)",
        )
        self.assertRegex(table, r"\bexplicit_recall\s+boolean\s+NOT\s+NULL\b")
        self.assertRegex(record, r"\bp_explicit_recall\s+boolean\b")
        self.assertRegex(
            record,
            r"existing\.explicit_recall\s+IS\s+DISTINCT\s+FROM\s+"
            r"p_explicit_recall",
        )
        self.assertRegex(
            record,
            r"(?s)INSERT\s+INTO\s+memory\.answer_binding\s*\(.*?"
            r"explicit_recall.*?\)\s*VALUES\s*\(.*?p_explicit_recall",
        )
        self.assertGreaterEqual(record.count("revision.requires_explicit"), 1)
        self.assertRegex(
            record,
            r"NOT\s+revision\.requires_explicit\s+OR\s+p_explicit_recall",
        )

    def test_answer_failure_code_is_absent_from_every_closed_surface(self) -> None:
        self.assertNotIn("failure_code", ANSWER_SELECTION_MANIFEST_FIELDS)
        self.assertNotIn("failure_code", ANSWER_BINDING_FIELDS)
        definitions = _function_definitions(self.foundation)
        digest = definitions["memory_private.answer_selection_manifest_sha256"]
        record = definitions["memory_private.record_answer_binding"]
        table = _table_definition(self.foundation, "memory.answer_binding")
        for surface in (digest, record, table):
            self.assertNotRegex(surface, r"\bfailure_code\b")
        expected_signature = (
            "memory_private.record_answer_binding("
            "uuid,uuid,uuid,text,text,text[],text[],text[],integer,integer,"
            "text,text,boolean,uuid[],uuid[],text,text,text,text,text)"
        )
        self.assertIn(expected_signature, self.contract["function_surface"])

    def test_answer_policy_envelope_is_cross_language_and_sql_authoritative(self) -> None:
        expected_material = (
            b"governed_memory.retrieval_policy.v1\n"
            b"explicit_recall:5:false\n"
            b"allowed_predicates_count:1:1\n"
            b"allowed_predicates_0:19:preference.personal\n"
            b"domains_count:1:0\n"
            b"intents_count:1:0\n"
            b"max_records:1:8\n"
            b"policy_revision:1:1\n"
        )
        expected_sha256 = (
            "293dfb7d7c4e6e7d6497028a3be3e36e9f5c0b50ce35a2fce9363ac29512400b"
        )
        arguments = {
            "explicit_recall": False,
            "allowed_predicates": ("preference.personal",),
            "domains": (),
            "intents": (),
            "max_records": 8,
            "policy_revision": 1,
        }
        self.assertEqual(RETRIEVAL_POLICY_DOMAIN, "governed_memory.retrieval_policy.v1")
        self.assertEqual(retrieval_policy_material_bytes(**arguments), expected_material)
        self.assertEqual(retrieval_policy_sha256(**arguments), expected_sha256)
        self.assertIn(expected_sha256, self.foundation)

        definitions = _function_definitions(self.foundation)
        self.assertIn(
            "memory_private.framed_text_array(text,text[])",
            self.contract["internal_functions"],
        )
        self.assertIn(
            "memory_private.retrieval_policy_sha256("
            "boolean,text[],text[],text[],integer,integer)",
            self.contract["internal_functions"],
        )
        policy_digest = definitions["memory_private.retrieval_policy_sha256"]
        record = definitions["memory_private.record_answer_binding"]
        table = _table_definition(self.foundation, "memory.answer_binding")
        self.assertIn("'governed_memory.retrieval_policy.v1'", policy_digest)
        self.assertRegex(
            policy_digest,
            r"'allowed_predicates_'\s*\|\|\s*"
            r"\(item\.ordinality\s*-\s*1\)::text",
        )
        self.assertNotIn("'allowed_predicates['", policy_digest)
        for parameter in (
            "p_explicit_recall",
            "p_allowed_predicates",
            "p_domains",
            "p_intents",
            "p_max_records",
            "p_policy_revision",
        ):
            with self.subTest(policy_parameter=parameter):
                self.assertIn(parameter, policy_digest)
                self.assertIn(parameter, record)
        self.assertRegex(
            record,
            r"p_policy_sha256\s*(?:<>|IS\s+DISTINCT\s+FROM)\s*"
            r"memory_private\.retrieval_policy_sha256\(\s*"
            r"p_explicit_recall,\s*p_allowed_predicates,\s*p_domains,\s*"
            r"p_intents,\s*p_max_records,\s*p_policy_revision\s*\)",
        )
        self.assertRegex(
            record,
            r"cardinality\(p_allowed_predicates\)\s+NOT\s+BETWEEN\s+1\s+AND\s+8",
        )
        self.assertRegex(record, r"p_max_records\s+NOT\s+BETWEEN\s+1\s+AND\s+8")
        self.assertRegex(record, r"p_policy_revision\s*<>\s*1")
        self.assertRegex(
            record,
            r"cardinality\(p_selected_claim_ids\)\s+NOT\s+BETWEEN\s+0\s+AND\s+8",
        )
        self.assertRegex(
            record,
            r"cardinality\(p_selected_claim_ids\)\s*>\s*p_max_records",
        )
        self.assertRegex(
            record,
            r"item\.value\s*!~\s*'\^\[a-z\]\[a-z0-9_\.:-\]\{0,127\}\$'",
        )
        for column, sql_type in (
            ("allowed_predicates", r"text\[\]"),
            ("policy_domains", r"text\[\]"),
            ("policy_intents", r"text\[\]"),
            ("policy_max_records", "integer"),
            ("policy_revision", "integer"),
        ):
            with self.subTest(persisted_policy_column=column):
                self.assertRegex(
                    table,
                    rf"\b{column}\s+{sql_type}\s+NOT\s+NULL\b",
                )
                self.assertIn(f"existing.{column}", record)
        for existing_column, parameter in (
            ("allowed_predicates", "p_allowed_predicates"),
            ("policy_domains", "p_domains"),
            ("policy_intents", "p_intents"),
            ("policy_max_records", "p_max_records"),
            ("policy_revision", "p_policy_revision"),
        ):
            with self.subTest(replay_policy_column=existing_column):
                self.assertRegex(
                    record,
                    rf"existing\.{existing_column}\s+"
                    rf"(?:<>|IS\s+DISTINCT\s+FROM)\s+{parameter}",
                )
        self.assertRegex(
            record,
            r"(?s)INSERT\s+INTO\s+memory\.answer_binding\s*\(.*?"
            r"allowed_predicates.*?policy_domains.*?policy_intents.*?"
            r"policy_max_records.*?policy_revision.*?\)\s*VALUES\s*\(.*?"
            r"p_allowed_predicates.*?p_domains.*?p_intents.*?p_max_records.*?"
            r"p_policy_revision",
        )

    def test_answer_sql_revalidates_full_current_row_and_policy_membership(self) -> None:
        record = _function_definitions(self.foundation)[
            "memory_private.record_answer_binding"
        ]
        for predicate in (
            r"(?:claim\.current_revision_id\s*=\s*revision\.revision_id|"
            r"revision\.revision_id\s*=\s*claim\.current_revision_id)",
            r"(?:claim\.current_revision_number\s*=\s*revision\.revision_number|"
            r"revision\.revision_number\s*=\s*claim\.current_revision_number)",
            r"revision\.predicate\s*=\s*ANY\s*\(p_allowed_predicates\)",
            r"predicate_catalog\.active",
            r"predicate_catalog\.predicate\s*=\s*revision\.predicate",
            r"predicate_catalog\.catalog_sha256\s*=\s*revision\.predicate_catalog_sha256",
            r"revision\.subject_entity_type\s*=\s*ANY\s*\(predicate_catalog\.subject_kinds\)",
            r"revision\.object_kind\s*=\s*ANY\s*\(predicate_catalog\.object_kinds\)",
            r"revision\.sensitivity\s*=\s*ANY\s*\(predicate_catalog\.sensitivities\)",
            r"revision\.epistemic_state\s*=\s*ANY\s*\(predicate_catalog\.epistemic_statuses\)",
        ):
            with self.subTest(authority_predicate=predicate):
                self.assertRegex(record, predicate)
        self.assertRegex(
            record,
            r"JOIN\s+memory\.predicate_catalog\s+AS\s+predicate_catalog\s+ON",
        )
        self.assertRegex(
            record,
            r"claim\.current_state_sha256\s*=\s*"
            r"memory_private\.claim_state_sha256\(\s*"
            r"claim\.owner_user_id,\s*claim\.claim_id,\s*"
            r"claim\.semantic_key_sha256,\s*claim\.claim_identity_sha256,\s*"
            r"claim\.lifecycle_state,\s*true,\s*"
            r"claim\.current_revision_id,\s*claim\.current_revision_number,\s*"
            r"revision\.revision_sha256,\s*claim\.projection_sequence\s*\)",
        )
        for policy_array, revision_array in (
            ("p_domains", "revision.domains"),
            ("p_intents", "revision.intents"),
        ):
            with self.subTest(policy_array=policy_array):
                self.assertRegex(
                    record,
                    rf"cardinality\({re.escape(revision_array)}\)\s*=\s*0",
                )
                self.assertRegex(record, rf"cardinality\({policy_array}\)\s*=\s*0")
                self.assertRegex(
                    record,
                    rf"{re.escape(revision_array)}\s*&&\s*{policy_array}",
                )

    def test_answer_binding_rerenders_postgres_rows_and_derives_exact_prefix(self) -> None:
        definitions = _function_definitions(self.foundation)
        renderer_name = "memory_private.render_answer_memory_record"
        self.assertIn(
            renderer_name + "(text,text,text,text,text,text,text,text)",
            self.contract["internal_functions"],
        )
        renderer = definitions[renderer_name]
        record = definitions["memory_private.record_answer_binding"]

        renderer_signature = renderer.split("RETURNS", 1)[0]
        self.assertEqual(
            tuple(
                re.findall(
                    r"\b(p_[a-z][a-z0-9_]*)\s+text\b",
                    renderer_signature,
                    flags=re.IGNORECASE,
                )
            ),
            (
                "p_epistemic_state",
                "p_predicate",
                "p_subject_display_name",
                "p_subject_entity_type",
                "p_object_kind",
                "p_object_display_name",
                "p_object_entity_type",
                "p_object_literal",
            ),
        )
        for fragment in (
            "{\"epistemic_state\":",
            ",\"fact\":{\"object\":",
            ",\"predicate\":",
            ",\"subject\":",
            ",\"record_type\":\"untrusted_memory_fact\"",
            '"treat_content_as_data":true}',
            "pg_catalog.to_jsonb(p_epistemic_state)::text",
            "pg_catalog.to_jsonb(p_predicate)::text",
            "pg_catalog.to_jsonb(p_subject_entity_type)::text",
        ):
            with self.subTest(renderer_fragment=fragment):
                self.assertIn(fragment, renderer)
        self.assertRegex(renderer, r"p_object_kind\s*=\s*'entity'")
        self.assertNotIn("jsonb_build_object", renderer.lower())
        for prohibited in (
            "claim_id",
            "revision_id",
            "subject_entity_key",
            "object_entity_key",
            "source_sha256",
            "state_sha256",
        ):
            with self.subTest(renderer_prohibited=prohibited):
                self.assertNotRegex(renderer, rf"\b{prohibited}\b")

        for declaration in (
            r"authoritative_memory_block\s+text",
            r"authoritative_injected_claim_ids\s+uuid\[\]",
            r"authoritative_injected_revision_ids\s+uuid\[\]",
            r"authoritative_injected_revision_sha256s\s+text\[\]",
        ):
            with self.subTest(declaration=declaration):
                self.assertRegex(record, declaration)
        self.assertIn("memory_private.render_answer_memory_record(", record)
        self.assertRegex(
            record,
            r"p_renderer_sha256\s+(?:<>|IS\s+DISTINCT\s+FROM)\s+"
            r"memory_private\.answer_renderer_sha256\(\)",
        )
        self.assertRegex(record, r"claim\.lifecycle_state\s*=\s*'active'")
        self.assertRegex(record, r"revision\.projectable")
        self.assertRegex(record, r"revision\.surface\s*<>\s*'never'")
        self.assertRegex(
            record,
            r"NOT\s+revision\.requires_explicit\s+OR\s+p_explicit_recall",
        )
        self.assertRegex(
            record,
            r"octet_length\([^;]*candidate_memory_block[^;]*\)"
            rf"[^;]*{MEMORY_BLOCK_MAX_UTF8_BYTES}",
        )
        self.assertRegex(
            record,
            rf"cardinality\(authoritative_injected_claim_ids\)\s*<\s*"
            rf"{ANSWER_RECORD_LIMIT}",
        )
        self.assertIn("IF NOT injection_prefix_closed THEN", record)
        self.assertIn("injection_prefix_closed := true", record)
        self.assertTrue(
            re.search(
                r"p_injected_claim_ids\s+IS\s+DISTINCT\s+FROM\s+"
                r"authoritative_injected_claim_ids",
                record,
            )
            or re.search(
                r"authoritative_injected_claim_ids\s+IS\s+DISTINCT\s+FROM\s+"
                r"p_injected_claim_ids",
                record,
            )
        )
        self.assertRegex(
            record,
            r"p_memory_block\s+IS\s+DISTINCT\s+FROM\s+"
            r"authoritative_memory_block",
        )
        for variable in (
            "authoritative_memory_block",
            "authoritative_injected_claim_ids",
            "authoritative_injected_revision_ids",
            "authoritative_injected_revision_sha256s",
        ):
            with self.subTest(caller_cannot_assign_authority=variable):
                self.assertNotRegex(record, rf"{variable}\s*:=\s*p_")

    def test_transient_answer_content_is_bind_only_and_never_persisted(self) -> None:
        self.assertEqual(
            self.contract["transport_contract"],
            {
                "extended_query_bind_parameters_required": True,
                "answer_memory_block_is_transient_parameter_only": True,
                "outbound_request_is_transient_parameter_only": True,
                "log_parameter_max_length": 0,
                "log_parameter_max_length_on_error": 0,
                "auto_explain_log_parameter_max_length_when_registered": 0,
            },
        )
        self.assertRegex(
            self.roles,
            r"current_setting\(\s*'log_parameter_max_length'\s*\)"
            r"::integer\s*<>\s*0",
        )
        self.assertRegex(
            self.roles,
            r"current_setting\(\s*'log_parameter_max_length_on_error'\s*\)"
            r"::integer\s*<>\s*0",
        )
        self.assertRegex(
            self.roles,
            r"(?s)current_setting\(\s*'auto_explain\.log_parameter_max_length',"
            r"\s*true\s*\).*?::integer\s*<>\s*0",
        )
        answer_table = _table_definition(self.foundation, "memory.answer_binding")
        provider_table = _table_definition(self.foundation, "memory.provider_call")
        for table, prohibited in (
            (answer_table, r"\bmemory_block\s+text\b"),
            (answer_table, r"\boutbound_request\s+text\b"),
            (provider_table, r"\brequest(?:_body|_text|_json)\s+"),
            (provider_table, r"\bresponse(?:_body|_text|_json)\s+"),
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotRegex(table, prohibited)
        self.assertRegex(answer_table, r"memory_block_sha256\s+text\b")
        self.assertRegex(answer_table, r"outbound_request_sha256\s+text\b")

    def test_expired_answer_bindings_have_a_worker_only_content_free_purge(self) -> None:
        signature = "memory_private.purge_expired_answer_bindings(integer)"
        self.assertIn(signature, self.contract["function_surface"])
        definitions = _function_definitions(self.foundation)
        purge = definitions["memory_private.purge_expired_answer_bindings"]
        answer_table = _table_definition(self.foundation, "memory.answer_binding")

        self.assertRegex(purge, r"session_user\s*<>\s*'governed_memory_worker'")
        self.assertRegex(
            purge,
            r"p_limit\s+IS\s+NULL\s+OR\s+p_limit\s+NOT\s+BETWEEN\s+1\s+AND\s+1000",
        )
        self.assertRegex(
            purge,
            r"expires_at\s*<=\s*pg_catalog\.transaction_timestamp\(\)",
        )
        self.assertIn("FOR UPDATE SKIP LOCKED", purge)
        self.assertRegex(purge, r"LIMIT\s+p_limit")
        audit_insert = purge.find("INSERT INTO memory.audit_event")
        delete = purge.find("DELETE FROM memory.answer_binding")
        self.assertGreaterEqual(audit_insert, 0)
        self.assertGreater(delete, audit_insert)
        audit_material = purge[audit_insert:delete]
        for prohibited in (
            "memory_block",
            "outbound_request",
            "subject_display_name",
            "object_display_name",
            "object_literal",
            "retrieval_text",
        ):
            with self.subTest(purge_audit_prohibited=prohibited):
                self.assertNotRegex(audit_material, rf"\b{prohibited}\b")
        self.assertRegex(
            purge,
            r"app\.memory_answer_binding_purge_operation_id",
        )

        trigger = re.search(
            r"CREATE\s+TRIGGER\s+[a-z][a-z0-9_]*\s+"
            r"BEFORE\s+UPDATE\s+OR\s+DELETE\s+ON\s+memory\.answer_binding\s+"
            r"FOR\s+EACH\s+ROW\s+EXECUTE\s+FUNCTION\s+"
            r"(memory_private\.[a-z][a-z0-9_]*)\(\)\s*;",
            self.foundation,
            flags=re.IGNORECASE,
        )
        self.assertIsNotNone(trigger, "answer binding lacks a dedicated guard")
        guard = definitions[trigger.group(1).lower()]
        self.assertIn("app.memory_hard_delete_operation_id", guard)
        self.assertIn("app.memory_answer_binding_purge_operation_id", guard)
        self.assertRegex(guard, r"session_user\s*<>\s*'governed_memory_worker'")
        self.assertRegex(answer_table, r"expires_at\s+timestamptz\s+NOT\s+NULL")
        self.assertRegex(
            answer_table,
            r"CONSTRAINT\s+answer_binding_retention\s+CHECK\s*\(\s*"
            r"expires_at\s*=\s*created_at\s*\+\s*interval\s*'90 days'\s*\)",
        )

    def test_claim_deletion_receipt_is_content_free_immutable_and_survives(self) -> None:
        receipt = _table_definition(
            self.foundation, "memory.claim_deletion_receipt"
        )
        expected_columns = (
            "receipt_id",
            "audit_event_id",
            "owner_user_id",
            "operation_id",
            "claim_id",
            "prior_state_sha256",
            "delete_outbox_id",
            "revision_id",
            "revision_sha256",
            "sequence_number",
            "collection_alias",
            "physical_collection_name",
            "point_id",
            "projection_manifest_sha256",
            "applied_at",
            "verified_at",
            "absence_verification_sha256",
            "absence_verification_receipt_sha256",
            "receipt_sha256",
            "created_at",
        )
        observed_columns = tuple(
            re.findall(
                r"^\s{2}([a-z][a-z0-9_]*)\s+"
                r"(?:uuid|text|integer|timestamptz)\b",
                receipt,
                flags=re.IGNORECASE | re.MULTILINE,
            )
        )
        self.assertEqual(observed_columns, expected_columns)
        self.assertRegex(
            receipt,
            r"REFERENCES\s+memory\.audit_event\(owner_user_id,\s*event_id\)"
            r"\s+ON\s+DELETE\s+RESTRICT",
        )
        self.assertRegex(
            self.foundation,
            r"(?s)CREATE\s+TRIGGER\s+claim_deletion_receipt_immutable\s+"
            r"BEFORE\s+UPDATE\s+OR\s+DELETE\s+ON\s+"
            r"memory\.claim_deletion_receipt.*?"
            r"memory_private\.guard_append_only_audit\(\)",
        )
        self.assertNotRegex(
            self.foundation,
            r"DELETE\s+FROM\s+memory\.claim_deletion_receipt\b",
        )
        finalize = _function_definitions(self.foundation)[
            "memory_private.finalize_claim_deletion"
        ]
        audit_insert = finalize.rfind("INSERT INTO memory.audit_event")
        receipt_insert = finalize.rfind(
            "INSERT INTO memory.claim_deletion_receipt"
        )
        self.assertGreaterEqual(audit_insert, 0)
        self.assertGreater(receipt_insert, audit_insert)

    def test_projection_completion_has_exact_terminal_replay_before_lease_checks(self) -> None:
        finish = _function_definitions(self.foundation)[
            "memory_private.finish_projection_job"
        ]
        lease_check = finish.find("outbox.state <> 'claimed'")
        self.assertGreaterEqual(lease_check, 0)
        replay_position = finish.find("'replayed'", 0, lease_check)
        self.assertGreaterEqual(
            replay_position,
            0,
            "terminal projection replay must precede stale-lease rejection",
        )
        terminal_replay = finish[:lease_check]
        for state in ("applied", "retryable", "failed_terminal"):
            with self.subTest(state=state):
                self.assertIn(f"'{state}'", terminal_replay)
        for binding in (
            "p_outcome",
            "p_physical_collection_name",
            "p_vector_sha256",
            "p_verification_sha256",
            "p_verification_receipt_sha256",
            "p_error_code",
            "outbox.physical_collection_name",
            "outbox.vector_sha256",
            "outbox.verification_sha256",
            "outbox.verification_receipt_sha256",
            "outbox.last_error_code",
        ):
            with self.subTest(replay_binding=binding):
                self.assertIn(binding, terminal_replay)
        self.assertRegex(
            terminal_replay,
            r"RETURN\s+QUERY\s+SELECT\s+'replayed'::text",
        )
        self.assertRegex(
            finish,
            r"(?s)resulting_state\s*:=\s*CASE.*?"
            r"p_outcome\s*=\s*'retryable'.*?"
            r"outbox\.attempt_count\s*<\s*outbox\.max_attempts.*?"
            r"THEN\s+'retryable'.*?ELSE\s+'failed_terminal'",
        )
        self.assertRegex(
            finish,
            r"(?s)SET\s+state\s*=\s*resulting_state.*?"
            r"RETURN\s+QUERY\s+SELECT\s+resulting_state,\s*false",
        )

    def test_projection_embedding_dispatch_is_durable_and_never_released_twice(self) -> None:
        definitions = _function_definitions(self.foundation)
        table = _table_definition(self.foundation, "memory.projection_outbox")
        for column in (
            "embedding_request_sha256 text",
            "embedding_dispatch_lease_token uuid",
            "embedding_dispatched_at timestamptz",
        ):
            with self.subTest(column=column):
                self.assertIn(column, table)
        self.assertIn("projection_outbox_embedding_dispatch_shape", table)
        self.assertIn(
            "acfdbf52a9201f941fcd897bc6b6a303e2c7e4f6820e9aa0465b01cc8a06ca54",
            self.foundation,
        )

        marker = definitions[
            "memory_private.mark_projection_embedding_dispatched"
        ]
        self.assertIn("FOR UPDATE", marker)
        self.assertIn("projection embedding dispatch replay forbidden", marker)
        self.assertIn("ERRCODE = '55000'", marker)
        self.assertIn("projection embedding dispatch drifted", marker)
        self.assertIn("ERRCODE = '23514'", marker)
        self.assertIn(
            "ec5de0ef28028972039d3cfa1949e5a468b7e69009720b1353e44228fa11b4f1",
            marker,
        )
        self.assertIn("memory_private.embedding_request_sha256", marker)
        self.assertIn(
            "memory_private.embedding_request_body_sha256",
            marker,
        )
        self.assertIn("FROM memory.claim AS value", marker)
        self.assertIn("FOR SHARE", marker)
        self.assertIn("target.projection_sequence", marker)
        self.assertIn("target.current_revision_id", marker)
        self.assertIn("target.lifecycle_state", marker)
        self.assertIn("pg_catalog.clock_timestamp()", marker)

        guard = definitions[
            "memory_private.guard_projection_outbox_mutation"
        ]
        self.assertRegex(
            guard,
            r"OLD\.state\s*=\s*'claimed'\s+AND\s+NEW\.state\s*=\s*'claimed'",
        )
        self.assertIn("pg_catalog.to_jsonb(OLD)", guard)
        self.assertIn("NEW.embedding_dispatch_lease_token", guard)
        self.assertIn("OLD.lease_token", guard)

        lease = definitions["memory_private.lease_projection_jobs"]
        self.assertRegex(
            lease,
            r"(?s)embedding_request_sha256\s+IS\s+NOT\s+NULL.*?"
            r"THEN\s+'failed_terminal'",
        )
        self.assertIn("embedding_dispatch_outcome_unknown", lease)
        self.assertIn(
            "d5ef651ccf1607f00c93da9f2e88219a366c178dbc2d5a015e7911af3cb05ba8",
            lease,
        )
        self.assertRegex(
            lease,
            r"outbox\.embedding_request_sha256\s+IS\s+NULL",
        )
        self.assertRegex(
            lease,
            r"(?s)earlier\.state\s*=\s*'failed_terminal'.*?"
            r"earlier\.embedding_request_sha256\s+IS\s+NOT\s+NULL",
        )

        finish = definitions["memory_private.finish_projection_job"]
        self.assertRegex(
            finish,
            r"(?s)p_outcome\s*=\s*'retryable'.*?"
            r"outbox\.embedding_request_sha256\s+IS\s+NOT\s+NULL.*?"
            r"THEN\s+'failed_terminal'",
        )
        self.assertIn("embedding_dispatch_outcome_unknown", finish)
        self.assertRegex(
            self.foundation,
            r"(?s)GRANT\s+EXECUTE\s+ON\s+FUNCTION.*?"
            r"memory_private\.mark_projection_embedding_dispatched\("
            r"\s*uuid,uuid,text,text,text,text,text,text\s*\).*?"
            r"TO\s+governed_memory_worker",
        )

    def test_hard_delete_is_a_separate_worker_only_verified_purge(self) -> None:
        finalize_signatures = [
            signature
            for signature in self.contract["function_surface"]
            if signature.startswith("memory_private.finalize_claim_deletion(")
        ]
        self.assertEqual(len(finalize_signatures), 1)
        definitions = _function_definitions(self.foundation)
        finalize = definitions["memory_private.finalize_claim_deletion"]
        self.assertRegex(
            finalize,
            r"session_user\s*<>\s*'governed_memory_worker'",
        )
        self.assertRegex(
            finalize,
            r"target\.lifecycle_state\s*<>\s*'deletion_pending'",
        )
        self.assertRegex(finalize, r"FROM\s+memory\.projection_outbox")
        self.assertRegex(
            finalize,
            r"delete_receipt\.operation\s*<>\s*'delete'",
        )
        self.assertRegex(
            finalize,
            r"delete_receipt\.state\s*<>\s*'applied'",
        )
        self.assertIn("INSERT INTO memory.audit_event", finalize)
        self.assertNotIn("DELETE FROM memory.audit_event", finalize)
        self.assertNotIn("CASCADE", finalize.upper())

        delete_targets = re.findall(
            r"\bDELETE\s+FROM\s+memory\.([a-z][a-z0-9_]*)\b",
            finalize,
            flags=re.IGNORECASE,
        )
        expected_purge_order = [
            "answer_binding",
            "claim_evidence",
            "projection_outbox",
            "proposal",
            "claim_revision",
            "claim",
            "provider_call",
            "extraction_job",
            "evidence",
            "entity",
        ]
        self.assertEqual(delete_targets, expected_purge_order)

        finish_projection = definitions["memory_private.finish_projection_job"]
        self.assertNotIn("DELETE FROM memory.claim", finish_projection)
        self.assertRegex(
            self.foundation,
            r"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+"
            r"memory_private\.finalize_claim_deletion\([^;]+\)\s+"
            r"TO\s+governed_memory_worker\s*;",
        )
        self.assertNotRegex(
            self.foundation,
            r"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+"
            r"memory_private\.finalize_claim_deletion\([^;]+\)\s+"
            r"TO\s+governed_memory_api\s*;",
        )

    def test_hard_delete_cycle_uses_deferrable_no_action_constraints(self) -> None:
        for constraint_name in (
            "claim_revision_proposal_fk",
            "proposal_correction_revision_fk",
        ):
            with self.subTest(constraint=constraint_name):
                self.assertRegex(
                    self.foundation,
                    rf"(?s)CONSTRAINT\s+{constraint_name}\s+FOREIGN KEY\s*\(.*?"
                    r"\)\s+REFERENCES\s+memory\.[a-z_]+\s*\(.*?\)\s+"
                    r"ON\s+DELETE\s+NO\s+ACTION\s+DEFERRABLE\s+"
                    r"INITIALLY\s+DEFERRED",
                    "the reciprocal proposal/revision delete cycle must use "
                    "deferrable NO ACTION; RESTRICT is checked immediately",
                )

    def test_sql_has_no_legacy_names_cascade_or_public_execute(self) -> None:
        combined = "\n".join(
            (self.roles, self.foundation, self.foundation_rollback, self.bridge, self.bridge_rollback)
        )
        legacy_trigger = "chat_log_enqueue_memory_" + "v1_consolidation"
        legacy_mentions = re.findall(
            r"[a-z0-9_]*memory_" + r"v1[a-z0-9_]*",
            combined.lower(),
        )
        self.assertTrue(legacy_mentions)
        self.assertEqual(set(legacy_mentions), {legacy_trigger})
        for prohibited in ("memory_" + "raw", "vantage"):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited.lower(), combined.lower())
        self.assertEqual(
            len(re.findall(r"\bON\s+DELETE\s+CASCADE\b", combined, re.I)),
            1,
        )
        self.assertNotRegex(
            combined,
            re.compile(r"\bDROP\b[^;]*\bCASCADE\b", re.I | re.S),
        )
        self.assertNotRegex(combined, r"GRANT\s+EXECUTE\b[^;]*\bTO\s+PUBLIC\b")


if __name__ == "__main__":
    unittest.main()
