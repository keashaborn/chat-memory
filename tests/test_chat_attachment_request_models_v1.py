from __future__ import annotations

import hashlib
import unittest
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import ChatAttachmentCreateReq, ChatAttachmentOwnerReq
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


if __name__ == "__main__":
    unittest.main()
