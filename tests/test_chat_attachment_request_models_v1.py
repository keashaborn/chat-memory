from __future__ import annotations

import hashlib
import json
import unittest
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from seebx.capabilities.conversation import attachment_routes
from seebx.capabilities.conversation.attachment_routes import (
    ChatAttachmentCreateReq,
    ChatAttachmentOwnerReq,
)
from seebx.capabilities.conversation.attachments import MAX_ATTACHMENT_BYTES


OWNER_ID = "11111111-1111-4111-8111-111111111111"
THREAD_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def attachment_payload(content: str = "attachment validation") -> dict[str, object]:
    return {
        "user_id": OWNER_ID,
        "thread_id": THREAD_ID,
        "filename": "Pasted text.md",
        "media_type": "text/markdown",
        "content": content,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


class ChatAttachmentRequestModelV1Tests(unittest.TestCase):
    def test_accepts_canonical_json_uuid_strings_with_uuid_runtime_types(self) -> None:
        value = ChatAttachmentCreateReq.model_validate(attachment_payload())
        self.assertEqual(value.user_id, uuid.UUID(OWNER_ID))
        self.assertEqual(value.thread_id, uuid.UUID(THREAD_ID))
        self.assertIsInstance(value.user_id, uuid.UUID)
        self.assertIsInstance(value.thread_id, uuid.UUID)

        owner = ChatAttachmentOwnerReq.model_validate({"user_id": OWNER_ID})
        self.assertEqual(owner.user_id, uuid.UUID(OWNER_ID))
        self.assertIsInstance(owner.user_id, uuid.UUID)

    def test_fastapi_json_body_regression_accepts_canonical_strings(self) -> None:
        test_app = FastAPI()

        @test_app.post("/attachments")
        async def create(body: ChatAttachmentCreateReq):
            return {
                "user_id": str(body.user_id),
                "thread_id": str(body.thread_id),
            }

        response = TestClient(test_app).post(
            "/attachments",
            json=attachment_payload(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {"user_id": OWNER_ID, "thread_id": THREAD_ID},
        )

    def test_accepts_exact_expanded_content_boundary(self) -> None:
        content = "x" * MAX_ATTACHMENT_BYTES
        value = ChatAttachmentCreateReq.model_validate(attachment_payload(content))
        self.assertEqual(len(value.content.encode("utf-8")), MAX_ATTACHMENT_BYTES)

    def test_rejects_non_string_and_noncanonical_uuid_values(self) -> None:
        invalid_values = [
            uuid.UUID(OWNER_ID),
            1,
            True,
            OWNER_ID.encode("ascii"),
            THREAD_ID.upper(),
            OWNER_ID.replace("-", ""),
            "{" + OWNER_ID + "}",
            " " + OWNER_ID,
            OWNER_ID + " ",
            "not-a-uuid",
        ]
        for invalid in invalid_values:
            with self.subTest(invalid=repr(invalid)):
                payload = attachment_payload()
                payload["user_id"] = invalid
                with self.assertRaises(ValidationError):
                    ChatAttachmentCreateReq.model_validate(payload)
                with self.assertRaises(ValidationError):
                    ChatAttachmentOwnerReq.model_validate({"user_id": invalid})

    def test_extra_fields_and_existing_content_contract_remain_denied(self) -> None:
        extra = attachment_payload()
        extra["unexpected"] = True
        with self.assertRaises(ValidationError):
            ChatAttachmentCreateReq.model_validate(extra)

        bad_hash = attachment_payload()
        bad_hash["content_sha256"] = "0" * 64
        with self.assertRaises(ValidationError):
            ChatAttachmentCreateReq.model_validate(bad_hash)

        oversized = attachment_payload("x" * (MAX_ATTACHMENT_BYTES + 1))
        with self.assertRaises(ValidationError):
            ChatAttachmentCreateReq.model_validate(oversized)

        bad_media = attachment_payload()
        bad_media["media_type"] = "application/pdf"
        with self.assertRaises(ValidationError):
            ChatAttachmentCreateReq.model_validate(bad_media)

        bad_filename = attachment_payload()
        bad_filename["filename"] = "unsafe/name.md"
        with self.assertRaises(ValidationError):
            ChatAttachmentCreateReq.model_validate(bad_filename)

    def test_http_and_postgres_effects_have_canonical_owners(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        app_source = (repository / "app.py").read_text()
        route_source = (
            repository
            / "seebx/capabilities/conversation/attachment_routes.py"
        ).read_text()
        adapter_source = (
            repository
            / "seebx/adapters/conversation_attachments.py"
        ).read_text()

        for former_app_owner in (
            '@app.post("/attachments")',
            '@app.get("/attachments/{attachment_id}")',
            "async def create_chat_attachment(",
            "async def get_chat_attachment_status(",
            "async def retry_chat_attachment(",
            "async def delete_chat_attachment(",
        ):
            self.assertNotIn(former_app_owner, app_source)
        for direct_database_effect in (
            "import asyncpg",
            "POSTGRES_DSN",
            "conversation.chat_attachments",
        ):
            self.assertNotIn(direct_database_effect, route_source)
        self.assertIn("app.include_router(conversation_attachment_router)", app_source)
        self.assertIn("CREATE_ATTACHMENT_SQL", adapter_source)
        self.assertIn("DELETE_ATTACHMENT_SQL", adapter_source)


class FakeAttachmentStore:
    def __init__(self) -> None:
        self.connection = object()
        self.closed_contexts = 0
        self.now = datetime(2026, 8, 18, tzinfo=timezone.utc)

    @asynccontextmanager
    async def owner_connection(self, owner_user_id: uuid.UUID):
        self.owner_user_id = owner_user_id
        try:
            yield self.connection
        finally:
            self.closed_contexts += 1

    async def create_attachment(self, connection, **kwargs):
        self.created = (connection, kwargs)
        return {
            "id": kwargs["attachment_id"],
            "thread_id": kwargs["thread_id"],
            "filename": kwargs["filename"],
            "media_type": kwargs["media_type"],
            "content_sha256": kwargs["content_sha256"],
            "byte_size": kwargs["byte_size"],
            "status": "ready",
            "created_at": self.now,
        }

    async def fetch_attachment_status(self, connection, **kwargs):
        self.status_read = (connection, kwargs)
        return {
            "id": kwargs["attachment_id"],
            "thread_id": uuid.UUID(THREAD_ID),
            "message_id": None,
            "filename": "Pasted text.md",
            "media_type": "text/markdown",
            "content_sha256": hashlib.sha256(b"bounded").hexdigest(),
            "byte_size": 7,
            "status": "ready",
            "created_at": self.now,
            "updated_at": self.now,
            "deleted_at": None,
        }

    async def fetch_retry_candidate(self, connection, **kwargs):
        self.retry_read = (connection, kwargs)
        return {
            "id": kwargs["attachment_id"],
            "content": "bounded",
            "content_sha256": hashlib.sha256(b"bounded").hexdigest(),
            "byte_size": 7,
        }

    async def update_retry_status(self, connection, **kwargs):
        self.retry_write = (connection, kwargs)
        return {"id": kwargs["attachment_id"], "status": kwargs["status"]}

    async def delete_attachment(self, connection, **kwargs):
        self.deleted = (connection, kwargs)
        return {"id": kwargs["attachment_id"], "deleted_at": self.now}


class ChatAttachmentRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_crud_success_shapes_and_context_cleanup(self) -> None:
        store = FakeAttachmentStore()
        attachment_id = "1a8beae3-58e5-4fb7-8f64-4fbb2bddcb73"
        body = ChatAttachmentCreateReq.model_validate(
            attachment_payload("bounded")
        )
        owner_body = ChatAttachmentOwnerReq.model_validate(
            {"user_id": OWNER_ID}
        )
        actor = AsyncMock(return_value=OWNER_ID)

        with (
            patch.object(attachment_routes, "_store", return_value=store),
            patch.object(attachment_routes, "require_actor", actor),
        ):
            created = await attachment_routes.create_chat_attachment(
                body,
                object(),
            )
            status = await attachment_routes.get_chat_attachment_status(
                attachment_id,
                OWNER_ID,
                object(),
            )
            retry = await attachment_routes.retry_chat_attachment(
                attachment_id,
                owner_body,
                object(),
            )
            deleted = await attachment_routes.delete_chat_attachment(
                attachment_id,
                OWNER_ID,
                object(),
            )

        self.assertEqual(created["status"], "ok")
        self.assertEqual(created["attachment"]["thread_id"], THREAD_ID)
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["attachment"]["id"], attachment_id)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(
            json.loads(retry.body),
            {
                "status": "ok",
                "attachment_id": attachment_id,
                "processing_status": "ready",
            },
        )
        self.assertEqual(deleted["status"], "ok")
        self.assertEqual(deleted["attachment_id"], attachment_id)
        self.assertEqual(store.closed_contexts, 4)
        self.assertEqual(store.retry_write[1]["status"], "ready")
        self.assertEqual(actor.await_count, 4)


if __name__ == "__main__":
    unittest.main()
