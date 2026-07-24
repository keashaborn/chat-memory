from __future__ import annotations

import json
import unittest
import uuid

import httpx

from scripts.voice_synthetic_canary import (
    CONTRACT_VERSION,
    CanaryConfig,
    CanaryFailure,
    _pcm_to_wav,
    _synthetic_transcript_matches,
    _validate_config,
    run_canary,
)


ACTOR = "d84eacb6-5c9a-4f5c-aab8-28845b456af3"


def pcm_bytes() -> bytes:
    return b"\x01\x00" * 4_800


def no_store_headers(content_type: str = "application/json") -> dict[str, str]:
    return {
        "cache-control": "private, no-store, max-age=0",
        "content-type": content_type,
    }


def voice_session_response(
    request: httpx.Request,
) -> httpx.Response | None:
    action = request.url.path.removeprefix("/voice/session/")
    flag = {
        "acquire": "acquired",
        "heartbeat": "renewed",
        "release": "released",
    }.get(action)
    if flag is None:
        return None
    body = json.loads(request.content)
    session_id = body["session_id"]
    if request.headers.get("x-vs-voice-session-id") != session_id:
        return httpx.Response(
            400,
            headers=no_store_headers(),
            json={"detail": "session header mismatch"},
        )
    return httpx.Response(
        200,
        headers=no_store_headers(),
        json={
            "ok": True,
            "session_id": session_id,
            flag: True,
        },
    )


class VoiceSyntheticCanaryTests(unittest.IsolatedAsyncioTestCase):
    def test_transcript_match_tolerates_one_misrecognized_word(self) -> None:
        self.assertTrue(
            _synthetic_transcript_matches("Operational voice scanner.")
        )
        self.assertTrue(
            _synthetic_transcript_matches("Voice canary is ready.")
        )
        self.assertFalse(
            _synthetic_transcript_matches("Unrelated background noise.")
        )

    def test_wav_wrapper_is_deterministic_and_valid(self) -> None:
        wrapped = _pcm_to_wav(pcm_bytes())
        self.assertEqual(wrapped[:4], b"RIFF")
        self.assertIn(b"WAVE", wrapped[:16])
        self.assertGreater(len(wrapped), len(pcm_bytes()))

    def test_configuration_requires_local_url_and_nonzero_actor(self) -> None:
        with self.assertRaisesRegex(CanaryFailure, "canary_base_url_must_be_local"):
            _validate_config(
                CanaryConfig(
                    base_url="https://example.test",
                    actor_user_id=ACTOR,
                    service_token="secret",
                )
            )
        with self.assertRaisesRegex(CanaryFailure, "invalid_canary_actor"):
            _validate_config(
                CanaryConfig(
                    base_url="http://127.0.0.1:8088",
                    actor_user_id=str(uuid.UUID(int=0)),
                    service_token="secret",
                )
            )

    async def test_success_uses_no_store_and_records_synthetic_trace(self) -> None:
        calls: list[tuple[str, str, dict | None]] = []
        protected_session_ids: list[str | None] = []
        tts_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal tts_calls
            body = None
            if request.content:
                try:
                    body = json.loads(request.content)
                except Exception:
                    body = None
            calls.append((request.method, request.url.path, body))

            session_response = voice_session_response(request)
            if session_response is not None:
                return session_response
            if request.url.path == "/voice/tts":
                tts_calls += 1
                protected_session_ids.append(
                    request.headers.get("x-vs-voice-session-id")
                )
                return httpx.Response(
                    200,
                    headers=no_store_headers("audio/pcm"),
                    content=pcm_bytes(),
                )
            if request.url.path == "/voice/openai/transcribe":
                protected_session_ids.append(
                    request.headers.get("x-vs-voice-session-id")
                )
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={
                        "transcript": "Operational voice canary.",
                        "provider": "openai",
                        "model": "gpt-4o-transcribe",
                        "language": "en",
                    },
                )
            if request.url.path == "/response/query":
                protected_session_ids.append(
                    request.headers.get("x-vs-voice-session-id")
                )
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={
                        "answer": "Governed voice canary operational.",
                        "answer_id": str(uuid.uuid4()),
                        "output_kind": "answer",
                        "runtime": "resse_response_v0_2",
                    },
                )
            if request.url.path == "/telemetry/event":
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={"accepted": 1, "rejected": 0, "errors": []},
                )
            if request.url.path == "/metrics/voice-slo":
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={
                        "contract_version": "voice_slo_v1",
                        "overall_status": "insufficient_data",
                        "sample": {
                            "evaluated_turns": 1,
                            "completed": 1,
                            "failed": 0,
                        },
                        "checks": {
                            "turn_success_rate": {
                                "status": "insufficient_data"
                            }
                        },
                    },
                )
            return httpx.Response(404, headers=no_store_headers())

        report, exit_code = await run_canary(
            CanaryConfig(
                base_url="http://127.0.0.1:8088",
                actor_user_id=ACTOR,
                service_token="service-secret",
            ),
            transport=httpx.MockTransport(handler),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["contract_version"], CONTRACT_VERSION)
        self.assertTrue(report["telemetry_recorded"])
        self.assertEqual(tts_calls, 2)
        paths = [path for _, path, _ in calls]
        self.assertEqual(paths.count("/voice/session/acquire"), 1)
        self.assertEqual(paths.count("/voice/session/heartbeat"), 3)
        self.assertEqual(paths.count("/voice/session/release"), 1)
        self.assertEqual(
            paths.index("/voice/session/acquire"),
            0,
        )
        self.assertLess(
            paths.index("/voice/session/release"),
            paths.index("/telemetry/event"),
        )
        self.assertTrue(protected_session_ids)
        self.assertEqual(len(set(protected_session_ids)), 1)
        self.assertIsNotNone(protected_session_ids[0])
        uuid.UUID(str(protected_session_ids[0]))

        response_body = next(
            body
            for _, path, body in calls
            if path == "/response/query"
        )
        self.assertTrue(response_body["no_store"])
        self.assertNotIn("thread_id", response_body)
        self.assertEqual(response_body["user_id"], ACTOR)

        telemetry_body = next(
            body
            for _, path, body in calls
            if path == "/telemetry/event"
        )
        event = telemetry_body["events"][0]
        self.assertIsNone(event["thread_id"])
        self.assertTrue(event["payload"]["synthetic"])
        self.assertEqual(
            event["payload"]["speech_to_first_audio_basis"],
            "synthetic_turn_start_v1",
        )
        serialized = json.dumps(event)
        self.assertNotIn("Operational voice canary", serialized)
        self.assertNotIn("Governed voice canary", serialized)

    async def test_response_failure_is_recorded_and_returns_nonzero(self) -> None:
        trace_payload: dict | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal trace_payload
            session_response = voice_session_response(request)
            if session_response is not None:
                return session_response
            if request.url.path == "/voice/tts":
                return httpx.Response(
                    200,
                    headers=no_store_headers("audio/pcm"),
                    content=pcm_bytes(),
                )
            if request.url.path == "/voice/openai/transcribe":
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={
                        "transcript": "Operational voice canary.",
                        "provider": "openai",
                        "model": "gpt-4o-transcribe",
                        "language": "en",
                    },
                )
            if request.url.path == "/response/query":
                return httpx.Response(
                    503,
                    headers=no_store_headers(),
                    json={"detail": "response_generation_unavailable"},
                )
            if request.url.path == "/telemetry/event":
                trace_payload = json.loads(request.content)["events"][0]
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={"accepted": 1, "rejected": 0, "errors": []},
                )
            if request.url.path == "/metrics/voice-slo":
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={
                        "contract_version": "voice_slo_v1",
                        "overall_status": "insufficient_data",
                        "sample": {
                            "evaluated_turns": 1,
                            "completed": 0,
                            "failed": 1,
                        },
                        "checks": {},
                    },
                )
            return httpx.Response(404, headers=no_store_headers())

        report, exit_code = await run_canary(
            CanaryConfig(
                base_url="http://localhost:8088",
                actor_user_id=ACTOR,
                service_token="service-secret",
            ),
            transport=httpx.MockTransport(handler),
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failure_stage"], "response")
        self.assertEqual(report["failure_code"], "upstream_http_503")
        self.assertEqual(report["alert_delivery"], "unconfigured")
        self.assertIsNotNone(trace_payload)
        self.assertEqual(trace_payload["payload"]["status"], "failed")
        self.assertEqual(
            trace_payload["payload"]["failure_stage"],
            "response",
        )

    async def test_slo_failure_fails_current_run_after_success(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            session_response = voice_session_response(request)
            if session_response is not None:
                return session_response
            if request.url.path == "/voice/tts":
                return httpx.Response(
                    200,
                    headers=no_store_headers("audio/pcm"),
                    content=pcm_bytes(),
                )
            if request.url.path == "/voice/openai/transcribe":
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={
                        "transcript": "Operational voice canary.",
                        "provider": "openai",
                        "model": "gpt-4o-transcribe",
                        "language": "en",
                    },
                )
            if request.url.path == "/response/query":
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={
                        "answer": "Operational.",
                        "runtime": "resse_response_v0_2",
                    },
                )
            if request.url.path == "/telemetry/event":
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={"accepted": 1, "rejected": 0},
                )
            if request.url.path == "/metrics/voice-slo":
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={
                        "contract_version": "voice_slo_v1",
                        "overall_status": "fail",
                        "sample": {
                            "evaluated_turns": 30,
                            "completed": 29,
                            "failed": 1,
                        },
                        "checks": {
                            "turn_success_rate": {"status": "fail"}
                        },
                    },
                )
            return httpx.Response(404, headers=no_store_headers())

        report, exit_code = await run_canary(
            CanaryConfig(
                base_url="http://127.0.0.1:8088",
                actor_user_id=ACTOR,
                service_token="service-secret",
            ),
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(report["failure_stage"], "slo")
        self.assertEqual(report["failure_code"], "voice_slo_threshold_failed")
