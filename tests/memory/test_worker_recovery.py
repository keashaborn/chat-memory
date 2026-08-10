"""Pure retry/replay planning tests; no lease store or worker process runs."""

from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
import inspect
import json
import unittest
from uuid import UUID

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory import worker as worker_core
from rag_engine.governed_memory.worker import (
    process_ingest_item as _process_ingest_item,
    resolve_context_review as _resolve_context_review,
    transition_provider_attempt,
)
from tests.memory._fixtures import (
    CUTOVER,
    EVIDENCE_A,
    FIXTURE_PROVENANCE,
    NOW,
    OWNER_A,
    JOB_A,
    make_bridge_lease,
    make_ingest_payload,
    make_worker_actor,
)


LEASE_A = UUID("01010101-0101-4101-8101-010101010101")
LEASE_B = UUID("02020202-0202-4202-8202-020202020202")
LEASE_C = UUID("03030303-0303-4303-8303-030303030303")
PROVIDER_JOB = UUID("09090909-0909-4909-8909-090909090909")
PROVIDER_CALL_A = UUID("10101010-1010-4010-8010-101010101010")
PROVIDER_CALL_B = UUID("11111111-1111-4111-8111-111111111110")
CONTEXT_MESSAGE = UUID("04040404-0404-4404-8404-040404040404")
CONTEXT_OWNER = UUID("05050505-0505-4505-8505-050505050505")
CONTEXT_THREAD = UUID("06060606-0606-4606-8606-060606060606")
CONTEXT_EXCHANGE = UUID("07070707-0707-4707-8707-070707070707")
CONTEXT_WINDOW = UUID("08080808-0808-4808-8808-080808080808")
PROVIDER_RECEIPT_SHA256 = "9" * 64


def process_ingest_item(
    payload: dict[str, object],
    *,
    context_review_count: int = 0,
    cutover=CUTOVER,
    successor_ingest_receipt=None,
) -> dict[str, object]:
    arguments = {
        "lease_envelope": make_bridge_lease(
            payload,
            ingest_after=cutover,
            context_review_count=context_review_count,
        ),
        "actor": make_worker_actor(),
        "expected_owner_user_id": OWNER_A,
        "transaction_time": NOW,
    }
    if successor_ingest_receipt is not None:
        arguments["successor_ingest_receipt"] = successor_ingest_receipt
    return _process_ingest_item(payload, **arguments)


def resolve_context_review(
    payload: dict[str, object],
    bounded_context: object,
    *,
    source_context_review_count: int = 1,
    cutover=CUTOVER,
) -> dict[str, object]:
    lease = make_bridge_lease(
        payload,
        ingest_after=cutover,
        context_review_count=source_context_review_count,
    )
    return _resolve_context_review(
        payload,
        bounded_context,
        lease_envelope=lease,
        actor=make_worker_actor(),
        expected_owner_user_id=OWNER_A,
        transaction_time=NOW,
    )


def provider_job(
    *,
    state: str = "pending",
    attempts: int = 0,
    lease_token: UUID | None = None,
    lease_expires_at=None,
    active_provider_call_id: UUID | None = None,
    job_id: UUID = PROVIDER_JOB,
    owner_user_id: UUID = OWNER_A,
) -> dict[str, object]:
    return {
        "job_id": str(job_id),
        "owner_user_id": str(owner_user_id),
        "state": state,
        "attempts": attempts,
        "lease_token": str(lease_token) if lease_token is not None else None,
        "lease_expires_at": lease_expires_at,
        "active_provider_call_id": (
            str(active_provider_call_id)
            if active_provider_call_id is not None
            else None
        ),
    }


def provider_call(
    *,
    state: str = "reserved",
    attempt_number: int = 1,
    lease_token: UUID = LEASE_A,
    lease_expires_at=None,
    provider_call_id: UUID = PROVIDER_CALL_A,
    job_id: UUID = PROVIDER_JOB,
    owner_user_id: UUID = OWNER_A,
    request_sha256: str | None = None,
    receipt_sha256: str | None = None,
    failure_reason_code: str | None = None,
) -> dict[str, object]:
    if lease_expires_at is None:
        lease_expires_at = NOW + timedelta(seconds=30)
    if state in {"dispatched", "completed"} and request_sha256 is None:
        request_sha256 = "8" * 64
    if state == "completed" and receipt_sha256 is None:
        receipt_sha256 = PROVIDER_RECEIPT_SHA256
    if state == "retryable_failure" and failure_reason_code is None:
        failure_reason_code = "connection_failed_before_send"
    if state == "terminal_failure" and failure_reason_code is None:
        failure_reason_code = "invalid_provider_output"
    if state == "outcome_unknown" and failure_reason_code is None:
        failure_reason_code = "provider_timeout_after_dispatch"
    return {
        "provider_call_id": str(provider_call_id),
        "job_id": str(job_id),
        "owner_user_id": str(owner_user_id),
        "attempt_number": attempt_number,
        "state": state,
        "lease_token": str(lease_token),
        "lease_expires_at": lease_expires_at,
        "request_sha256": request_sha256,
        "receipt_sha256": receipt_sha256,
        "failure_reason_code": failure_reason_code,
    }


def claimed_pair(
    *, state: str = "reserved", lease_expires_at=None
) -> tuple[dict[str, object], dict[str, object]]:
    if lease_expires_at is None:
        lease_expires_at = NOW + timedelta(seconds=30)
    job = provider_job(
        state="claimed",
        attempts=1,
        lease_token=LEASE_A,
        lease_expires_at=lease_expires_at,
        active_provider_call_id=PROVIDER_CALL_A,
    )
    call = provider_call(state=state, lease_expires_at=lease_expires_at)
    return job, call


def reserve_command(
    *, now=NOW, lease_token: UUID = LEASE_A, provider_call_id: UUID = PROVIDER_CALL_A
) -> dict[str, object]:
    return {
        "action": "reserve",
        "now": now,
        "lease_token": str(lease_token),
        "lease_expires_at": now + timedelta(seconds=30),
        "provider_call_id": str(provider_call_id),
    }


class IntakeReplayTests(unittest.TestCase):
    def test_worker_signature_has_one_lease_and_optional_successor_receipt(self) -> None:
        signature = inspect.signature(_process_ingest_item)
        self.assertIn("lease_envelope", signature.parameters)
        self.assertIn("successor_ingest_receipt", signature.parameters)
        self.assertIsNone(
            signature.parameters["successor_ingest_receipt"].default
        )
        self.assertNotIn("context_review_count", signature.parameters)
        self.assertNotIn("cutover", signature.parameters)

    def test_worker_requires_one_exact_authoritative_bridge_lease_envelope(self) -> None:
        payload = make_ingest_payload()
        lease = make_bridge_lease(payload, ingest_after=CUTOVER)
        self.assertEqual(
            tuple(sorted(lease)),
            tuple(sorted(worker_core.BRIDGE_LEASE_FIELDS)),
        )
        mutations = {
            "owner_user_id": str(CONTEXT_OWNER),
            "message_id": str(CONTEXT_MESSAGE),
            "thread_id": str(CONTEXT_THREAD),
            "exchange_id": str(CONTEXT_EXCHANGE),
            "window_id": str(CONTEXT_WINDOW),
            "window_ordinal": int(payload["window_ordinal"]) + 1,
            "window_sha256": "0" * 64,
            "content_sha256": "0" * 64,
            "source_binding_sha256": "0" * 64,
            "policy_sha256": "0" * 64,
            "source_created_at": NOW - timedelta(microseconds=1),
            "ingest_after": CUTOVER + timedelta(microseconds=1),
            "context_review_count": 2,
            "eligibility_decision": "send_external",
            "lease_expires_at": NOW,
        }
        for field, replacement in mutations.items():
            malformed = {**lease, field: replacement}
            with self.subTest(field=field), self.assertRaises(ContractViolation):
                _process_ingest_item(
                    payload,
                    lease_envelope=malformed,
                    actor=make_worker_actor(),
                    expected_owner_user_id=OWNER_A,
                    transaction_time=NOW,
                )
        for malformed in (
            {key: value for key, value in lease.items() if key != "lease_token"},
            {**lease, "untrusted_extra": True},
        ):
            with self.subTest(keys=tuple(sorted(malformed))), self.assertRaises(
                ContractViolation
            ):
                _process_ingest_item(
                    payload,
                    lease_envelope=malformed,
                    actor=make_worker_actor(),
                    expected_owner_user_id=OWNER_A,
                    transaction_time=NOW,
                )

    def test_count_one_crash_replay_acks_only_an_exact_successor_receipt(self) -> None:
        helper = getattr(worker_core, "ingest_successor_receipt_sha256", None)
        self.assertTrue(callable(helper), "ingest successor receipt hash is absent")
        self.assertEqual(
            tuple(inspect.signature(helper).parameters),
            (
                "owner_user_id",
                "operation_id",
                "bridge_source_binding_sha256",
                "decision",
                "evidence_id",
                "extraction_job_id",
            ),
        )
        payload = make_ingest_payload()
        lease = make_bridge_lease(
            payload,
            ingest_after=CUTOVER,
            context_review_count=1,
        )
        receipt = {
            "owner_user_id": payload["owner_user_id"],
            "operation_id": payload["message_id"],
            "bridge_source_binding_sha256": lease["source_binding_sha256"],
            "decision": "send_external",
            "evidence_id": str(EVIDENCE_A),
            "extraction_job_id": str(JOB_A),
        }
        receipt["receipt_sha256"] = helper(**receipt)
        result = process_ingest_item(
            payload,
            context_review_count=1,
            successor_ingest_receipt=receipt,
        )
        self.assertEqual(result["decision"], "send_external")
        self.assertEqual(result["bridge_state"], "completed")
        self.assertEqual(result["resolution"], "successor_receipt_replayed")
        self.assertEqual(result["effects"], ["ack_existing_memory_ingest"])
        self.assertEqual(result["successor_evidence_id"], str(EVIDENCE_A))
        self.assertEqual(result["successor_job_id"], str(JOB_A))
        self.assertFalse(result["provider_allowed"])
        self.assertNotIn("load_bounded_context", result["effects"])
        self.assertNotIn("create_extraction_job", result["effects"])

        for field, replacement in (
            ("owner_user_id", str(CONTEXT_OWNER)),
            ("operation_id", str(CONTEXT_MESSAGE)),
            ("bridge_source_binding_sha256", "0" * 64),
            ("evidence_id", str(CONTEXT_MESSAGE)),
            ("extraction_job_id", str(CONTEXT_MESSAGE)),
            ("receipt_sha256", "0" * 64),
        ):
            malformed = {**receipt, field: replacement}
            with self.subTest(field=field), self.assertRaises(ContractViolation):
                process_ingest_item(
                    payload,
                    context_review_count=1,
                    successor_ingest_receipt=malformed,
                )

    def test_count_zero_crash_replay_acks_without_duplicate_successor_work(self) -> None:
        payload = make_ingest_payload()
        lease = make_bridge_lease(
            payload,
            ingest_after=CUTOVER,
            context_review_count=0,
        )
        receipt = {
            "owner_user_id": payload["owner_user_id"],
            "operation_id": payload["message_id"],
            "bridge_source_binding_sha256": lease["source_binding_sha256"],
            "decision": "send_external",
            "evidence_id": str(EVIDENCE_A),
            "extraction_job_id": str(JOB_A),
        }
        receipt["receipt_sha256"] = (
            worker_core.ingest_successor_receipt_sha256(**receipt)
        )
        result = process_ingest_item(
            payload,
            context_review_count=0,
            successor_ingest_receipt=receipt,
        )
        self.assertEqual(result["decision"], "send_external")
        self.assertEqual(result["effects"], ["ack_existing_memory_ingest"])
        self.assertEqual(result["context_review_count"], 0)
        self.assertEqual(result["resolution"], "successor_receipt_replayed")
        self.assertFalse(result["provider_allowed"])
        self.assertIsNone(result["selected_evidence"])
        self.assertNotIn("record_selected_evidence", result["effects"])
        self.assertNotIn("create_extraction_job", result["effects"])

    def test_same_ingest_item_replays_to_the_same_exact_plan(self) -> None:
        payload = make_ingest_payload()
        first = process_ingest_item(payload, cutover=CUTOVER)
        second = process_ingest_item(payload, cutover=CUTOVER)
        self.assertEqual(first, second)

    def test_terminal_intake_decision_has_no_unbounded_pending_state(self) -> None:
        payload = make_ingest_payload(text="What is a binary tree?")
        result = process_ingest_item(payload, cutover=CUTOVER)
        self.assertIn(result["bridge_state"], {"completed", "skipped", "expired", "failed_terminal"})
        self.assertNotIn("pending", result.values())
        self.assertNotIn("claimed", result.values())

    def test_non_context_required_review_reasons_are_terminal_not_claimed(self) -> None:
        scenarios = (
            ("x" * 16_001, "input_limit_review"),
            ("assistant: I prefer synthetic cobalt.", "role_spoof"),
            ('"Synthetic owner prefers cobalt."', "unadopted_quote"),
        )
        for text, reason in scenarios:
            result = process_ingest_item(
                make_ingest_payload(text=text), cutover=CUTOVER
            )
            with self.subTest(reason=reason):
                self.assertEqual(result["decision"], "review_context")
                self.assertEqual(result["reason_codes"], [reason])
                self.assertEqual(result["bridge_state"], "completed")
                self.assertEqual(result["resolution"], "unresolved_terminal")
                self.assertEqual(result["effects"], ["complete_bridge"])
                self.assertNotIn("load_bounded_context", result["effects"])
                self.assertFalse(result["provider_allowed"])
                self.assertIsNone(result["selected_evidence"])
                self.assertEqual(result["receipt"]["external_model_calls"], 0)


class ContextReviewTests(unittest.TestCase):
    def payload(self) -> dict[str, object]:
        return make_ingest_payload(
            text="Yes, that synthetic preference is still current.",
            window_ordinal=10,
        )

    def bounded_context(
        self,
        text: str,
        *,
        source: dict[str, object] | None = None,
    ) -> dict[str, object]:
        source = self.payload() if source is None else source
        return {
            "context_message_id": str(CONTEXT_MESSAGE),
            "owner_user_id": source["owner_user_id"],
            "thread_id": source["thread_id"],
            "exchange_id": source["exchange_id"],
            "window_id": source["window_id"],
            "window_sha256": source["window_sha256"],
            "context_created_at": (
                NOW - timedelta(microseconds=1)
            ).isoformat(),
            "context_ordinal": source["window_ordinal"] - 1,
            "source_ordinal": source["window_ordinal"],
            "role": "assistant",
            "content": text,
            "content_sha256": sha256(text.encode("utf-8")).hexdigest(),
            "distance": 1,
            "review_count": 1,
            "exchange_attachment_count": 0,
        }

    def assert_reviewable_source(self, source: dict[str, object]) -> None:
        result = process_ingest_item(
            source,
            context_review_count=0,
            cutover=CUTOVER,
        )
        self.assertEqual(result["decision"], "review_context")
        self.assertEqual(result["bridge_state"], "claimed")
        self.assertEqual(result["context_review_count"], 1)
        self.assertEqual(
            result["effects"],
            ["mark_memory_ingest_context_review", "load_bounded_context"],
        )

    def test_first_review_context_classification_stays_claimed(self) -> None:
        result = process_ingest_item(
            self.payload(),
            context_review_count=0,
            cutover=CUTOVER,
        )
        self.assertEqual(result["decision"], "review_context")
        self.assertEqual(result["bridge_state"], "claimed")
        self.assertEqual(
            result["effects"],
            ["mark_memory_ingest_context_review", "load_bounded_context"],
        )
        self.assertEqual(result["context_review_count"], 1)
        self.assertNotIn("complete_bridge", result["effects"])
        self.assertNotIn("source_sha256", result["receipt"])
        self.assertNotIn("content_sha256", result["receipt"])

    def test_context_review_replay_is_terminal_without_a_second_load(self) -> None:
        result = process_ingest_item(
            self.payload(),
            context_review_count=1,
            cutover=CUTOVER,
        )
        self.assertEqual(result["decision"], "review_context")
        self.assertEqual(result["bridge_state"], "completed")
        self.assertEqual(result["resolution"], "unresolved_terminal")
        self.assertEqual(result["context_review_count"], 1)
        self.assertEqual(
            result["effects"], ["mark_memory_ingest_context_review"]
        )
        self.assertNotIn("load_bounded_context", result["effects"])
        self.assertNotIn("complete_bridge", result["effects"])
        self.assertFalse(result["provider_allowed"])
        self.assertIsNone(result["selected_evidence"])
        self.assertEqual(result["receipt"]["external_model_calls"], 0)

    def test_one_bounded_review_can_resolve_to_exact_send_external(self) -> None:
        question = "Do you still prefer the synthetic cobalt interface theme?"
        answer = "Yes, that synthetic preference is still current."
        source = self.payload()
        context = self.bounded_context(question, source=source)
        self.assertEqual(
            set(context),
            {
                "context_message_id",
                "owner_user_id",
                "thread_id",
                "exchange_id",
                "window_id",
                "window_sha256",
                "context_created_at",
                "context_ordinal",
                "source_ordinal",
                "role",
                "content",
                "content_sha256",
                "distance",
                "review_count",
                "exchange_attachment_count",
            },
        )
        result = resolve_context_review(
            source,
            context,
            cutover=CUTOVER,
        )
        self.assertEqual(result["decision"], "send_external")
        self.assertEqual(result["resolution"], "context_resolved")
        self.assertEqual(result["bridge_state"], "completed")
        self.assertEqual(result["context_review_count"], 1)
        self.assertTrue(result["provider_allowed"])
        self.assertEqual(
            result["effects"],
            [
                "record_selected_evidence",
                "create_extraction_job",
                "complete_bridge",
            ],
        )
        self.assertEqual(
            result["selected_evidence"]["source_sha256"],
            make_ingest_payload(text=answer)["content_sha256"],
        )
        self.assertEqual(
            result["selected_evidence"]["selected_sha256"],
            sha256(answer.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            result["selected_evidence"]["context_sha256"],
            sha256(question.encode("utf-8")).hexdigest(),
        )
        provider_context = json.dumps(
            result["provider_context"], sort_keys=True, ensure_ascii=False
        )
        self.assertIn(question, provider_context)
        self.assertNotIn(answer, provider_context)
        durable_evidence = json.dumps(
            result["selected_evidence"], sort_keys=True, ensure_ascii=False
        )
        self.assertNotIn(question, durable_evidence)
        self.assertEqual(
            set(result["selected_evidence"]) & {"context_message_id", "context_sha256"},
            {"context_message_id", "context_sha256"},
        )
        self.assertNotIn("context_created_at", result["selected_evidence"])
        self.assertNotIn("context_ordinal", result["selected_evidence"])
        self.assertNotIn("source_ordinal", result["selected_evidence"])
        durable_receipt = json.dumps(
            result["receipt"], sort_keys=True, ensure_ascii=False
        )
        self.assertNotIn(question, durable_receipt)
        self.assertNotIn("context_created_at", result["receipt"])
        self.assertNotIn("context_ordinal", result["receipt"])
        self.assertNotIn("source_ordinal", result["receipt"])

    def test_context_authority_requires_exact_leased_source_bindings(self) -> None:
        question = "Do you still prefer the synthetic cobalt interface theme?"
        mutations = {
            "owner_user_id": str(CONTEXT_OWNER),
            "thread_id": str(CONTEXT_THREAD),
            "exchange_id": str(CONTEXT_EXCHANGE),
            "window_id": str(CONTEXT_WINDOW),
            "window_sha256": "0" * 64,
        }
        for field, forged_value in mutations.items():
            source = self.payload()
            self.assert_reviewable_source(source)
            context = self.bounded_context(question, source=source)
            context[field] = forged_value
            with self.subTest(field=field), self.assertRaises(ContractViolation):
                resolve_context_review(source, context, cutover=CUTOVER)

    def test_context_authority_requires_exact_distinct_adjacent_ordinals(self) -> None:
        question = "Do you still prefer the synthetic cobalt interface theme?"
        source = self.payload()
        self.assert_reviewable_source(source)
        mutations = (
            {"context_ordinal": source["window_ordinal"]},
            {
                "context_ordinal": source["window_ordinal"],
                "source_ordinal": source["window_ordinal"] + 1,
            },
            {"source_ordinal": source["window_ordinal"] + 1},
            {"context_message_id": source["message_id"]},
        )
        for mutation in mutations:
            context = self.bounded_context(question, source=source)
            context.update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(
                ContractViolation
            ):
                resolve_context_review(source, context, cutover=CUTOVER)

    def test_tied_timestamp_is_ordered_by_authoritative_adjacent_ordinals(self) -> None:
        question = "Do you still prefer the synthetic cobalt interface theme?"
        source = self.payload()
        self.assert_reviewable_source(source)
        context = self.bounded_context(question, source=source)
        context["context_created_at"] = source["created_at"]
        result = resolve_context_review(source, context, cutover=CUTOVER)
        self.assertEqual(result["resolution"], "context_resolved")
        self.assertEqual(result["decision"], "send_external")

    def test_future_and_pre_cutover_context_are_rejected(self) -> None:
        question = "Do you still prefer the synthetic cobalt interface theme?"
        source = self.payload()
        self.assert_reviewable_source(source)
        for created_at in (
            NOW + timedelta(microseconds=1),
            CUTOVER - timedelta(microseconds=1),
        ):
            context = self.bounded_context(question, source=source)
            context["context_created_at"] = created_at.isoformat()
            with self.subTest(created_at=created_at), self.assertRaises(
                ContractViolation
            ):
                resolve_context_review(source, context, cutover=CUTOVER)

    def test_wrong_role_and_multi_context_inputs_are_rejected(self) -> None:
        question = "Do you still prefer the synthetic cobalt interface theme?"
        source = self.payload()
        self.assert_reviewable_source(source)
        wrong_role = self.bounded_context(question, source=source)
        wrong_role["role"] = "user"
        with self.assertRaises(ContractViolation):
            resolve_context_review(source, wrong_role, cutover=CUTOVER)

        first = self.bounded_context(question, source=source)
        second = dict(first)
        second["context_message_id"] = str(CONTEXT_OWNER)
        with self.assertRaises(ContractViolation):
            resolve_context_review(source, [first, second], cutover=CUTOVER)

    def test_context_with_any_exchange_attachment_is_terminally_blocked(self) -> None:
        question = "Do you still prefer the synthetic cobalt interface theme?"
        source = self.payload()
        self.assert_reviewable_source(source)
        context = self.bounded_context(question, source=source)
        context["exchange_attachment_count"] = 1
        result = resolve_context_review(source, context, cutover=CUTOVER)
        self.assertEqual(result["decision"], "block_local")
        self.assertEqual(result["resolution"], "unresolved_terminal")
        self.assertEqual(result["bridge_state"], "completed")
        self.assertEqual(result["effects"], ["complete_bridge"])
        self.assertFalse(result["provider_allowed"])
        self.assertIsNone(result["provider_context"])
        self.assertIsNone(result["selected_evidence"])
        self.assertEqual(result["receipt"]["external_model_calls"], 0)
        serialized = json.dumps(result["receipt"], sort_keys=True)
        self.assertNotIn("attachment_id", serialized)
        self.assertNotIn("attachment_content", serialized)

    def test_sensitive_or_identifier_context_never_reaches_provider(self) -> None:
        contexts = (
            "Do you still prefer cobalt? My password is SYNTHETIC_SECRET.",
            "Do you still prefer cobalt? Email synthetic@example.invalid.",
            "Do you still prefer cobalt? Card 4111 1111 1111 1111.",
            "Do you still prefer cobalt? Recovery token " + "A7" * 32 + ".",
            "Do you still prefer cobalt? Bank account number 123456789.",
        )
        for text in contexts:
            source = self.payload()
            self.assert_reviewable_source(source)
            context = self.bounded_context(text, source=source)
            result = resolve_context_review(source, context, cutover=CUTOVER)
            with self.subTest(text=text):
                self.assertEqual(result["bridge_state"], "completed")
                self.assertFalse(result["provider_allowed"])
                self.assertIsNone(result["provider_context"])
                self.assertIsNone(result["selected_evidence"])
                self.assertEqual(result["receipt"]["external_model_calls"], 0)
                serialized = json.dumps(result["receipt"], sort_keys=True)
                self.assertNotIn(text, serialized)

    def test_health_or_trauma_prior_context_is_sensitive_local_only(self) -> None:
        contexts = (
            "Were you treated for synthetic cancer after surgery?",
            "Were you assaulted during that synthetic life event?",
        )
        for text in contexts:
            source = self.payload()
            self.assert_reviewable_source(source)
            result = resolve_context_review(
                source,
                self.bounded_context(text, source=source),
                cutover=CUTOVER,
            )
            with self.subTest(text=text):
                self.assertEqual(result["decision"], "route_internal")
                self.assertEqual(result["reason_codes"], ["sensitive_local_only"])
                self.assertEqual(result["resolution"], "unresolved_terminal")
                self.assertEqual(result["bridge_state"], "completed")
                self.assertEqual(result["effects"], ["complete_bridge"])
                self.assertFalse(result["provider_allowed"])
                self.assertIsNone(result["provider_context"])
                self.assertIsNone(result["selected_evidence"])
                self.assertEqual(result["receipt"]["external_model_calls"], 0)
                self.assertNotIn(
                    text,
                    json.dumps(result["receipt"], sort_keys=True),
                )

    def test_excluded_context_questions_route_internal_without_evidence(self) -> None:
        cases = (
            ("What response format do you prefer?", "assistant_preference"),
            (
                "Do you still prefer the cobalt interface for our project?",
                "project_scope_excluded",
            ),
            ("Log your workout repetitions?", "structured_product_event"),
        )
        for text, reason in cases:
            source = self.payload()
            self.assert_reviewable_source(source)
            result = resolve_context_review(
                source,
                self.bounded_context(text, source=source),
                cutover=CUTOVER,
            )
            with self.subTest(reason=reason):
                self.assertEqual(result["decision"], "route_internal")
                self.assertEqual(result["reason_codes"], [reason])
                self.assertEqual(result["bridge_state"], "completed")
                self.assertEqual(result["effects"], ["complete_bridge"])
                self.assertFalse(result["provider_allowed"])
                self.assertIsNone(result["provider_context"])
                self.assertIsNone(result["selected_evidence"])
                self.assertEqual(result["receipt"]["external_model_calls"], 0)

    def test_context_resolver_requires_persisted_source_and_context_review_count_one(self) -> None:
        signature = inspect.signature(_resolve_context_review)
        self.assertIn("lease_envelope", signature.parameters)
        self.assertNotIn("lease_source", signature.parameters)

    def test_context_resolver_rejects_non_authoritative_review_counts(self) -> None:
        valid_source = self.payload()
        valid_context = self.bounded_context(
            "Do you still prefer the synthetic cobalt interface theme?",
            source=valid_source,
        )
        valid_lease = make_bridge_lease(
            valid_source,
            ingest_after=CUTOVER,
            context_review_count=1,
        )
        malformed_pairs = (
            ({**valid_lease, "context_review_count": 0}, valid_context),
            ({**valid_lease, "context_review_count": 2}, valid_context),
            (
                valid_lease,
                {
                    key: value
                    for key, value in valid_context.items()
                    if key != "review_count"
                },
            ),
            (valid_lease, {**valid_context, "review_count": 0}),
            (valid_lease, {**valid_context, "review_count": 2}),
        )
        for lease, context in malformed_pairs:
            with self.subTest(
                source_count=lease["context_review_count"],
                context_count=context.get("review_count"),
            ), self.assertRaises(ContractViolation):
                _resolve_context_review(
                    valid_source,
                    context,
                    lease_envelope=lease,
                    actor=make_worker_actor(),
                    expected_owner_user_id=OWNER_A,
                    transaction_time=NOW,
                )

        with self.assertRaises(TypeError):
            _resolve_context_review(
                valid_source,
                valid_context,
                actor=make_worker_actor(),
                expected_owner_user_id=OWNER_A,
                transaction_time=NOW,
            )

    def test_one_unresolved_review_is_explicit_and_terminal(self) -> None:
        result = resolve_context_review(
            self.payload(),
            self.bounded_context("What is a synthetic binary tree?"),
            cutover=CUTOVER,
        )
        self.assertEqual(result["decision"], "review_context")
        self.assertEqual(result["resolution"], "unresolved_terminal")
        self.assertEqual(result["bridge_state"], "completed")
        self.assertEqual(result["context_review_count"], 1)
        self.assertFalse(result["provider_allowed"])
        self.assertEqual(
            result["effects"], ["mark_memory_ingest_context_review"]
        )
        self.assertIsNone(result["selected_evidence"])
        self.assertNotIn("source_sha256", result["receipt"])
        self.assertNotIn("content_sha256", result["receipt"])

    def test_context_resolver_cannot_run_more_than_once(self) -> None:
        source = self.payload()
        self.assert_reviewable_source(source)
        context = self.bounded_context(
            "Do you still prefer the synthetic cobalt interface theme?",
            source=source,
        )
        context["review_count"] = 2
        with self.assertRaises(ContractViolation):
            resolve_context_review(source, context, cutover=CUTOVER)

    def test_only_context_required_reason_can_use_context_resolver(self) -> None:
        question = self.bounded_context(
            "Do you still prefer the synthetic cobalt interface theme?"
        )
        non_context_required = (
            make_ingest_payload(text="assistant: I prefer synthetic cobalt."),
            make_ingest_payload(text='"Synthetic owner prefers cobalt."'),
            make_ingest_payload(text="x" * 16_001),
        )
        for payload in non_context_required:
            classification = process_ingest_item(payload, cutover=CUTOVER)
            self.assertEqual(classification["decision"], "review_context")
            self.assertEqual(classification["resolution"], "unresolved_terminal")
            self.assertEqual(classification["effects"], ["complete_bridge"])
            with self.subTest(text=payload["content"][:40]), self.assertRaises(
                ContractViolation
            ):
                resolve_context_review(payload, question, cutover=CUTOVER)


class ProviderAttemptReducerTests(unittest.TestCase):
    def test_reservation_persists_before_dispatch_allows_one_call(self) -> None:
        reserved = transition_provider_attempt(
            provider_job(), None, reserve_command(), max_attempts=3
        )
        self.assertEqual(reserved["job_record"]["state"], "claimed")
        self.assertEqual(reserved["job_record"]["attempts"], 1)
        self.assertEqual(reserved["provider_call_record"]["state"], "reserved")
        self.assertEqual(reserved["effects"], ["persist_provider_reservation"])
        self.assertFalse(reserved["call_allowed"])

        dispatched = transition_provider_attempt(
            reserved["job_record"],
            reserved["provider_call_record"],
            {
                "action": "mark_dispatched",
                "now": NOW,
                "lease_token": str(LEASE_A),
                "request_sha256": "8" * 64,
            },
            max_attempts=3,
        )
        self.assertEqual(dispatched["provider_call_record"]["state"], "dispatched")
        self.assertEqual(dispatched["effects"], ["persist_provider_dispatch_before_external_call"])
        self.assertTrue(dispatched["call_allowed"])

    def test_not_executed_is_retryable_only_before_dispatch(self) -> None:
        reserved_job, reserved_call = claimed_pair()
        retryable = transition_provider_attempt(
            reserved_job,
            reserved_call,
            {
                "action": "fail_not_executed",
                "now": NOW,
                "lease_token": str(LEASE_A),
                "reason_code": "connection_failed_before_send",
            },
            max_attempts=3,
        )
        self.assertEqual(retryable["job_record"]["state"], "retryable")
        self.assertEqual(
            retryable["provider_call_record"]["state"], "retryable_failure"
        )
        self.assertFalse(retryable["call_allowed"])

        dispatched_job, dispatched_call = claimed_pair(state="dispatched")
        with self.assertRaises(ContractViolation):
            transition_provider_attempt(
                dispatched_job,
                dispatched_call,
                {
                    "action": "fail_not_executed",
                    "now": NOW,
                    "lease_token": str(LEASE_A),
                    "reason_code": "provider_proved_not_accepted",
                },
                max_attempts=3,
            )

    def test_terminal_provider_failure_requires_persisted_dispatch(self) -> None:
        reserved_job, reserved_call = claimed_pair()
        command = {
            "action": "fail_terminal",
            "now": NOW,
            "lease_token": str(LEASE_A),
            "reason_code": "provider_request_rejected",
        }
        with self.assertRaisesRegex(
            ContractViolation, "provider_call_not_failable"
        ):
            transition_provider_attempt(
                reserved_job,
                reserved_call,
                command,
                max_attempts=3,
            )

        dispatched_job, dispatched_call = claimed_pair(state="dispatched")
        failed = transition_provider_attempt(
            dispatched_job,
            dispatched_call,
            command,
            max_attempts=3,
        )
        self.assertEqual(failed["job_record"]["state"], "failed_terminal")
        self.assertEqual(
            failed["provider_call_record"]["state"], "terminal_failure"
        )
        self.assertEqual(
            failed["provider_call_record"]["request_sha256"], "8" * 64
        )

    def test_lease_equality_is_expired_and_routes_by_dispatch_state(self) -> None:
        for state, expected_call_state, expected_job_state in (
            ("reserved", "retryable_failure", "retryable"),
            ("dispatched", "outcome_unknown", "failed_terminal"),
        ):
            job, call = claimed_pair(state=state, lease_expires_at=NOW)
            with self.subTest(state=state, active_command="rejected"):
                with self.assertRaises(ContractViolation):
                    transition_provider_attempt(
                        job,
                        call,
                        {
                            "action": (
                                "mark_dispatched"
                                if state == "reserved"
                                else "fail_unknown"
                            ),
                            "now": NOW,
                            "lease_token": str(LEASE_A),
                            **(
                                {"request_sha256": "8" * 64}
                                if state == "reserved"
                                else {"reason_code": "provider_timeout_after_dispatch"}
                            ),
                        },
                        max_attempts=3,
                    )
            expired = transition_provider_attempt(
                job,
                call,
                {
                    "action": "lease_expired",
                    "now": NOW,
                    "lease_token": str(LEASE_A),
                },
                max_attempts=3,
            )
            with self.subTest(state=state, lease_expired="accepted"):
                self.assertEqual(
                    expired["provider_call_record"]["state"], expected_call_state
                )
                self.assertEqual(expired["job_record"]["state"], expected_job_state)
                self.assertFalse(expired["call_allowed"])

    def test_malformed_persisted_job_and_call_shapes_fail_closed(self) -> None:
        valid_job, valid_call = claimed_pair()
        malformed_jobs = (
            {**provider_job(), "unexpected": None},
            {key: value for key, value in provider_job().items() if key != "job_id"},
            provider_job(
                state="claimed",
                attempts=1,
                lease_token=LEASE_A,
                lease_expires_at=NOW + timedelta(seconds=30),
            ),
            {**provider_job(), "attempts": True},
        )
        for malformed in malformed_jobs:
            with self.subTest(job=malformed), self.assertRaises(ContractViolation):
                transition_provider_attempt(
                    malformed, None, reserve_command(), max_attempts=3
                )

        malformed_calls = (
            {**valid_call, "unexpected": None},
            {key: value for key, value in valid_call.items() if key != "job_id"},
            {**valid_call, "request_sha256": "8" * 64},
            provider_call(
                state="retryable_failure",
                failure_reason_code="free text is prohibited",
            ),
        )
        for malformed in malformed_calls:
            with self.subTest(call=malformed), self.assertRaises(ContractViolation):
                transition_provider_attempt(
                    valid_job,
                    malformed,
                    {
                        "action": "fail_not_executed",
                        "now": NOW,
                        "lease_token": str(LEASE_A),
                        "reason_code": "connection_failed_before_send",
                    },
                    max_attempts=3,
                )

    def test_failure_reason_is_a_closed_code_not_free_text(self) -> None:
        cases = (
            ("reserved", "fail_not_executed"),
            ("dispatched", "fail_terminal"),
            ("dispatched", "fail_unknown"),
        )
        for state, action in cases:
            job, call = claimed_pair(state=state)
            with self.subTest(action=action), self.assertRaises(ContractViolation):
                transition_provider_attempt(
                    job,
                    call,
                    {
                        "action": action,
                        "now": NOW,
                        "lease_token": str(LEASE_A),
                        "reason_code": "provider timed out: https://example.invalid",
                    },
                    max_attempts=3,
                )

    def test_reserve_requires_exact_prior_call_shape_for_job_state(self) -> None:
        prior = provider_call(state="retryable_failure")
        invalid = (
            (provider_job(), prior),
            (provider_job(state="retryable", attempts=1), None),
            (
                provider_job(state="retryable", attempts=1),
                provider_call(
                    state="retryable_failure",
                    job_id=UUID("12121212-1212-4212-8212-121212121212"),
                ),
            ),
        )
        for job, call in invalid:
            with self.subTest(job_state=job["state"], call=call), self.assertRaises(
                ContractViolation
            ):
                transition_provider_attempt(
                    job,
                    call,
                    reserve_command(
                        lease_token=LEASE_B, provider_call_id=PROVIDER_CALL_B
                    ),
                    max_attempts=3,
                )

        resumed = transition_provider_attempt(
            provider_job(state="retryable", attempts=1),
            prior,
            reserve_command(lease_token=LEASE_B, provider_call_id=PROVIDER_CALL_B),
            max_attempts=3,
        )
        self.assertEqual(resumed["job_record"]["state"], "claimed")
        self.assertEqual(resumed["job_record"]["attempts"], 2)
        self.assertEqual(resumed["provider_call_record"]["attempt_number"], 2)

    def test_every_post_dispatch_unknown_is_terminal_with_zero_recall(self) -> None:
        for reason in (
            "connection_reset_after_dispatch",
            "dispatch_crash",
            "provider_timeout_after_dispatch",
            "response_persistence_failed_after_dispatch",
        ):
            job, call = claimed_pair(state="dispatched")
            result = transition_provider_attempt(
                job,
                call,
                {
                    "action": "fail_unknown",
                    "now": NOW,
                    "lease_token": str(LEASE_A),
                    "reason_code": reason,
                },
                max_attempts=3,
            )
            with self.subTest(reason=reason):
                self.assertEqual(result["job_record"]["state"], "failed_terminal")
                self.assertEqual(
                    result["provider_call_record"]["state"], "outcome_unknown"
                )
                self.assertEqual(result["effects"], ["persist_outcome_unknown_terminal"])
                self.assertFalse(result["call_allowed"])
                self.assertTrue(result["terminal"])

    def test_invalid_output_is_terminal_and_terminal_jobs_never_reenter(self) -> None:
        job, call = claimed_pair(state="dispatched")
        failed = transition_provider_attempt(
            job,
            call,
            {
                "action": "fail_terminal",
                "now": NOW,
                "lease_token": str(LEASE_A),
                "reason_code": "invalid_provider_output",
            },
            max_attempts=3,
        )
        self.assertEqual(failed["job_record"]["state"], "failed_terminal")
        self.assertEqual(
            failed["provider_call_record"]["state"], "terminal_failure"
        )
        self.assertFalse(failed["call_allowed"])
        with self.assertRaises(ContractViolation):
            transition_provider_attempt(
                failed["job_record"],
                failed["provider_call_record"],
                reserve_command(
                    lease_token=LEASE_B, provider_call_id=PROVIDER_CALL_B
                ),
                max_attempts=3,
            )


if __name__ == "__main__":
    unittest.main()
