from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import unittest
from uuid import UUID

from rag_engine.governed_memory.contracts import (
    ContractViolation,
    bridge_source_binding_sha256,
    canonical_sha256,
    sha256_text,
)
from rag_engine.governed_memory.conversation_source import (
    LEASED_CHAT_LOG_FIELDS,
    split_leased_chat_log_message,
)
from rag_engine.governed_memory.eligibility import EligibilityPolicy


ROOT = Path(__file__).resolve().parents[2]
FORWARD = (
    ROOT
    / "governed-memory-migrations"
    / "0002_conversation_bridge"
    / "forward.pgsql"
)
ROLLBACK = FORWARD.with_name("rollback.pgsql")
OWNER = UUID("11111111-1111-4111-8111-111111111111")
MESSAGE = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
THREAD = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OUTBOX = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
LEASE = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
CREATED_AT = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
CONTENT = "Synthetic owner prefers the cobalt interface theme."


def _leased_row() -> dict[str, object]:
    content_sha256 = sha256_text(CONTENT)
    window_sha256 = canonical_sha256(
        "governed_memory.ingest_window",
        {
            "owner_user_id": OWNER,
            "thread_id": THREAD,
            "exchange_id": MESSAGE,
            "window_id": MESSAGE,
            "message_id": MESSAGE,
            "content_sha256": content_sha256,
        },
    )
    policy_sha256 = EligibilityPolicy(ingest_after=CREATED_AT).policy_sha256
    values: dict[str, object] = {
        "outbox_id": OUTBOX,
        "owner_user_id": OWNER,
        "message_id": MESSAGE,
        "thread_id": THREAD,
        "exchange_id": MESSAGE,
        "window_id": MESSAGE,
        "window_ordinal": 0,
        "window_sha256": window_sha256,
        "content_sha256": content_sha256,
        "source_binding_sha256": bridge_source_binding_sha256(
            owner_user_id=OWNER,
            message_id=MESSAGE,
            thread_id=THREAD,
            exchange_id=MESSAGE,
            window_id=MESSAGE,
            window_ordinal=0,
            window_sha256=window_sha256,
            content_sha256=content_sha256,
            policy_sha256=policy_sha256,
            source_created_at=CREATED_AT,
        ),
        "policy_sha256": policy_sha256,
        "source_created_at": CREATED_AT,
        "ingest_after": CREATED_AT,
        "context_review_count": 0,
        "eligibility_decision": None,
        "lease_token": LEASE,
        "lease_expires_at": CREATED_AT + timedelta(minutes=2),
        "role": "user",
        "content": CONTENT,
    }
    if tuple(values) != LEASED_CHAT_LOG_FIELDS:
        raise AssertionError("synthetic leased row field order drifted")
    return values


class ConversationSourceTests(unittest.TestCase):
    def test_exact_leased_row_splits_into_closed_lease_and_payload(self) -> None:
        lease, payload = split_leased_chat_log_message(_leased_row())
        self.assertNotIn("content", lease)
        self.assertEqual(payload["owner_user_id"], str(OWNER))
        self.assertEqual(payload["message_id"], str(MESSAGE))
        self.assertEqual(payload["exchange_id"], str(MESSAGE))
        self.assertEqual(payload["window_id"], str(MESSAGE))
        self.assertEqual(payload["window_ordinal"], 0)
        self.assertEqual(payload["role"], "user")
        self.assertEqual(payload["content"], CONTENT)
        self.assertNotIn("attachments", payload)
        self.assertNotIn("attachment_ids", payload)

    def test_reader_result_rejects_role_content_and_lineage_drift(self) -> None:
        mutations = {
            "role": "assistant",
            "content": CONTENT + " drift",
            "exchange_id": UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        }
        for field, value in mutations.items():
            row = _leased_row()
            row[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ContractViolation):
                    split_leased_chat_log_message(row)


class ConversationBridgeMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.forward = FORWARD.read_text(encoding="utf-8")
        cls.rollback = ROLLBACK.read_text(encoding="utf-8")

    def test_bridge_is_bound_to_production_shape_without_read_or_import_surface(self) -> None:
        self.assertIn("pg_catalog.current_database() <> 'memory'", self.forward)
        self.assertIn("session_user <> 'brains_app'", self.forward)
        self.assertIn("FROM public.chat_log AS source", self.forward)
        self.assertIn("source_row.source_xmin <> current_xid", self.forward)
        self.assertIn("pg_catalog.pg_current_xact_id()", self.forward)
        self.assertIn(
            "tgname = 'chat_log_enqueue_memory_v1_consolidation'",
            self.forward,
        )
        self.assertIn("tgenabled <> 'D'", self.forward)
        self.assertIn("tgtype <> 5", self.forward)
        self.assertIn("memory.enqueue_chat_log_consolidation()", self.forward)
        self.assertNotRegex(
            self.forward,
            r"(?i)\b(?:COPY|dblink|postgres_fdw|CREATE\s+SUBSCRIPTION|"
            r"CREATE\s+PUBLICATION)\b",
        )
        self.assertNotRegex(
            self.forward,
            r"(?i)GRANT\s+SELECT\b[^;]*\bTO\s+"
            r"(?:memory_ingest_writer|governed_memory_worker)\b",
        )

    def test_source_logging_preflight_parses_preloaded_libraries_without_escaped_regex(self) -> None:
        preflight = self.forward.split("DO $preflight$", 1)[1].split(
            "$preflight$;", 1
        )[0]
        self.assertIn("pg_catalog.string_to_array(", preflight)
        self.assertIn("pg_catalog.btrim(configured.library_name)", preflight)
        self.assertIn("= 'pgaudit'", preflight)
        self.assertNotIn(r"\\s*pgaudit", preflight)
        self.assertIn("current_setting('log_duration') <> 'off'", preflight)
        self.assertRegex(
            preflight,
            r"current_setting\(\s*'log_parameter_max_length'\s*\)"
            r"::integer\s*<>\s*0",
        )
        self.assertNotIn("NOT IN (-1, 0)", preflight)

    def test_postflight_rechecks_all_five_exact_role_identities(self) -> None:
        postflight = self.forward.split("DO $postflight$", 1)[1].split(
            "$postflight$;", 1
        )[0]
        for role in (
            "brains_app",
            "governed_memory_api",
            "governed_memory_worker",
            "memory_ingest_writer",
            "memory_erasure_requester",
        ):
            with self.subTest(role=role):
                self.assertIn(f"rolname = '{role}'", postflight)
        self.assertIn("AND rolcanlogin AND NOT rolsuper", postflight)
        self.assertGreaterEqual(
            postflight.count("governed_memory.inactive_installation"), 2
        )

    def test_every_source_runtime_membership_edge_is_rejected_both_directions(
        self,
    ) -> None:
        runtime_roles = {
            "governed_memory_api",
            "governed_memory_worker",
            "memory_ingest_writer",
            "memory_erasure_requester",
        }

        def membership_role_sets(sql: str) -> list[tuple[set[str], set[str]]]:
            return [
                (
                    set(re.findall(r"'([^']+)'", granted)),
                    set(re.findall(r"'([^']+)'", member)),
                )
                for granted, member in re.findall(
                    r"WHERE granted_role\.rolname IN \((.*?)\)"
                    r"\s+OR member_role\.rolname IN \((.*?)\)",
                    sql,
                    flags=re.DOTALL,
                )
            ]

        preflight = self.forward.split("DO $preflight$", 1)[1].split(
            "$preflight$;", 1
        )[0]
        postflight = self.forward.split("DO $postflight$", 1)[1].split(
            "$postflight$;", 1
        )[0]
        rollback_preflight = self.rollback.split("DO $preflight$", 1)[1].split(
            "$preflight$;", 1
        )[0]
        for label, block in (
            ("forward preflight", preflight),
            ("forward postflight", postflight),
            ("rollback preflight", rollback_preflight),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    membership_role_sets(block),
                    [(runtime_roles, runtime_roles)],
                )
                self.assertIn("membership.roleid", block)
                self.assertIn("membership.member", block)

        def rejected(member_role: str, granted_role: str) -> bool:
            return member_role in runtime_roles or granted_role in runtime_roles

        for runtime_role in runtime_roles:
            with self.subTest(runtime_role=runtime_role, direction="member"):
                self.assertTrue(rejected(runtime_role, "unrelated_role"))
            with self.subTest(runtime_role=runtime_role, direction="roleid"):
                self.assertTrue(rejected("unrelated_role", runtime_role))
        self.assertFalse(rejected("unrelated_member", "unrelated_role"))

    def test_outbox_is_private_and_reader_is_exact_lease_token_only(self) -> None:
        self.assertIn(
            "CREATE TABLE memory_ingest_private.memory_ingest_outbox",
            self.forward,
        )
        reader = self.forward.split(
            "CREATE FUNCTION memory_ingest_private.read_leased_chat_log_message(",
            1,
        )[1].split("$function$;", 1)[0]
        self.assertIn("target.lease_token <> p_lease_token", reader)
        self.assertIn("target.lease_expires_at <= pg_catalog.clock_timestamp()", reader)
        self.assertIn("source.id = target.message_id", reader)
        self.assertIn("source.owner_user_id = target.owner_user_id", reader)
        self.assertIn("source.thread_id = target.thread_id", reader)
        self.assertIn(
            "source_row.source IS DISTINCT FROM 'frontend/chat:user'",
            reader,
        )
        self.assertIn("FROM public.chat_attachments AS attachment", reader)
        self.assertIn(
            "attachment.message_id = target.message_id",
            reader,
        )
        self.assertNotRegex(
            reader,
            r"attachment\.(?:content|filename|media_type|content_sha256)",
        )
        self.assertNotRegex(reader, r"(?i)\bLIMIT\b|\bOFFSET\b")

    def test_attachment_bound_message_is_excluded_at_enqueue_lease_and_read(self) -> None:
        enqueue = self.forward.split(
            "CREATE FUNCTION memory_ingest_private.enqueue_chat_log_message(",
            1,
        )[1].split("$function$;", 1)[0]
        lease = self.forward.split(
            "CREATE FUNCTION memory_ingest_private.lease_memory_ingest(",
            1,
        )[1].split("$function$;", 1)[0]
        reader = self.forward.split(
            "CREATE FUNCTION memory_ingest_private.read_leased_chat_log_message(",
            1,
        )[1].split("$function$;", 1)[0]
        self.assertIn("attachment.message_id = p_message_id", enqueue)
        self.assertIn("AND NOT EXISTS (", lease)
        self.assertIn("attachment.message_id = value.message_id", lease)
        self.assertIn("attachment.message_id = target.message_id", reader)
        for definition in (enqueue, lease, reader):
            self.assertNotRegex(
                definition,
                r"attachment\.(?:content|filename|media_type|content_sha256)",
            )

    def test_pilot_limit_is_owner_serialized_replay_first_and_all_state(self) -> None:
        enqueue = self.forward.split(
            "CREATE FUNCTION memory_ingest_private.enqueue_chat_log_message(",
            1,
        )[1].split("$function$;", 1)[0]
        lock_at = enqueue.index("actor::text || '|memory_ingest|pilot_limit'")
        replay_lookup_at = enqueue.index(
            "FROM memory_ingest_private.memory_ingest_outbox AS value"
        )
        replay_return_at = enqueue.index(
            "RETURN QUERY SELECT 'replayed'::text, existing.outbox_id"
        )
        count_at = enqueue.index("SELECT pg_catalog.count(*)", replay_return_at)
        limit_return_at = enqueue.index(
            "RETURN QUERY SELECT 'pilot_limit_reached'::text, NULL::uuid"
        )
        insert_at = enqueue.index(
            "INSERT INTO memory_ingest_private.memory_ingest_outbox"
        )
        self.assertLess(lock_at, replay_lookup_at)
        self.assertLess(replay_lookup_at, replay_return_at)
        self.assertLess(replay_return_at, count_at)
        self.assertLess(count_at, limit_return_at)
        self.assertLess(limit_return_at, insert_at)
        cap = enqueue[count_at:limit_return_at]
        self.assertIn("value.owner_user_id = actor", cap)
        self.assertIn(
            "value.source_created_at >= captured_at - interval '24 hours'",
            cap,
        )
        self.assertIn("value.source_created_at <= captured_at", cap)
        self.assertIn(") >= 20 THEN", enqueue)
        self.assertNotRegex(cap, r"\bstate\b")

    def test_enqueue_accepts_no_caller_owner_content_or_lineage(self) -> None:
        signature = self.forward.split(
            "CREATE FUNCTION memory_ingest_private.enqueue_chat_log_message(",
            1,
        )[1].split(")\nRETURNS", 1)[0]
        self.assertEqual(
            re.sub(r"\s+", " ", signature).strip(),
            "p_message_id uuid, p_policy_sha256 text",
        )
        enqueue = self.forward.split(
            "CREATE FUNCTION memory_ingest_private.enqueue_chat_log_message(",
            1,
        )[1].split("$function$;", 1)[0]
        self.assertIn(
            "source_row.source IS DISTINCT FROM 'frontend/chat:user'",
            enqueue,
        )
        for prohibited in (
            "owner",
            "thread",
            "content",
            "source_created",
            "exchange",
            "window",
            "attachment",
        ):
            self.assertNotIn(prohibited, signature)

    def test_rollback_is_private_empty_only_and_requires_deactivation(self) -> None:
        self.assertIn("pg_catalog.current_database() <> 'memory'", self.rollback)
        self.assertIn(
            "pg_catalog.current_setting('transaction_isolation') <> 'read committed'",
            self.rollback,
        )
        self.assertIn("FROM pg_catalog.pg_auth_members AS membership", self.rollback)
        self.assertIn("granted_role.oid = membership.roleid", self.rollback)
        self.assertIn("member_role.oid = membership.member", self.rollback)
        self.assertNotIn("pg_catalog.pg_has_role(", self.rollback)
        self.assertIn(
            "SELECT 1 FROM memory_ingest_private.memory_ingest_outbox LIMIT 1",
            self.rollback,
        )
        lock_at = self.rollback.index(
            "LOCK TABLE memory_ingest_private.memory_ingest_outbox\n"
            "  IN ACCESS EXCLUSIVE MODE;"
        )
        empty_check_at = self.rollback.index("DO $empty_only$")
        drop_at = self.rollback.index(
            "DROP TABLE memory_ingest_private.memory_ingest_outbox;"
        )
        self.assertLess(lock_at, empty_check_at)
        self.assertLess(empty_check_at, drop_at)
        self.assertIn(
            "DROP TABLE memory_ingest_private.memory_ingest_outbox",
            self.rollback,
        )
        self.assertNotIn("CASCADE", self.rollback.upper())
