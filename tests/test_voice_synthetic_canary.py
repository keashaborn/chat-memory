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
    _post_monitor_traces,
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

    async def test_trace_requires_private_monitor_observation(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers=no_store_headers(),
                json={
                    "accepted": 1,
                    "rejected": 0,
                    "monitor_observations": 0,
                    "errors": [],
                },
            )

        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8088",
            transport=httpx.MockTransport(handler),
        ) as client:
            with self.assertRaisesRegex(
                CanaryFailure,
                "monitor_observation_not_recorded",
            ):
                await _post_monitor_traces(
                    client,
                    headers={},
                    voice_turn_id=str(uuid.uuid4()),
                    status="failed",
                    failure_stage="tts",
                    failure_code="upstream_http_422",
                    metrics={},
                    slo_payload={
                        "contract_version": "voice_slo_v1",
                        "overall_status": "insufficient_data",
                        "sample": {
                            "evaluated_turns": 1,
                            "completed": 0,
                            "failed": 1,
                        },
                        "checks": {},
                    },
                    slo_failure_code="",
                )

    async def test_slo_recording_failure_is_isolated_from_current_trace(self) -> None:
        events: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            event = json.loads(request.content)["events"][0]
            events.append(event)
            return httpx.Response(
                200,
                headers=no_store_headers(),
                json={
                    "accepted": 1,
                    "rejected": 0,
                    "monitor_observations": (
                        1 if event["event_type"] == "voice.turn.trace" else 0
                    ),
                    "errors": [],
                },
            )

        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8088",
            transport=httpx.MockTransport(handler),
        ) as client:
            observations, slo_store = await _post_monitor_traces(
                client,
                headers={},
                voice_turn_id=str(uuid.uuid4()),
                status="completed",
                failure_stage="none",
                failure_code="",
                metrics={},
                slo_payload={
                    "contract_version": "voice_slo_v1",
                    "overall_status": "pass",
                    "sample": {
                        "evaluated_turns": 40,
                        "completed": 40,
                        "failed": 0,
                    },
                    "checks": {},
                },
                slo_failure_code="",
            )

        self.assertEqual(observations, 1)
        self.assertEqual(slo_store, "failed")
        self.assertEqual(
            [event["event_type"] for event in events],
            ["voice.turn.trace", "voice.slo.observation"],
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
                event_type = body["events"][0]["event_type"]
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={
                        "accepted": 1,
                        "rejected": 0,
                        "monitor_observations": (
                            1 if event_type == "voice.turn.trace" else 0
                        ),
                        "errors": [],
                    },
                )
            if request.url.path == "/metrics/voice-slo":
                self.assertEqual(request.url.params["window_days"], "7")
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
        self.assertEqual(report["alert_store"], "recorded")
        self.assertEqual(report["slo_alert_store"], "recorded")
        self.assertEqual(report["monitor_observations"], 1)
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

        for _, path, body in calls:
            if path == "/voice/tts":
                self.assertEqual(body["conversation_style"], "direct")
                self.assertNotIn("instructions", body)

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
        slo_event = next(
            body["events"][0]
            for _, path, body in calls
            if path == "/telemetry/event"
            and body["events"][0]["event_type"]
            == "voice.slo.observation"
        )
        self.assertEqual(
            slo_event["payload"]["overall_status"],
            "insufficient_data",
        )
        self.assertEqual(slo_event["payload"]["window_days"], 7)

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
                event = json.loads(request.content)["events"][0]
                if event["event_type"] == "voice.turn.trace":
                    trace_payload = event
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={
                        "accepted": 1,
                        "rejected": 0,
                        "monitor_observations": (
                            1 if event["event_type"] == "voice.turn.trace" else 0
                        ),
                        "errors": [],
                    },
                )
            if request.url.path == "/metrics/voice-slo":
                self.assertEqual(request.url.params["window_days"], "7")
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
        self.assertEqual(report["alert_store"], "recorded")
        self.assertEqual(report["slo_alert_store"], "recorded")
        self.assertEqual(report["monitor_observations"], 1)
        self.assertIsNotNone(trace_payload)
        self.assertEqual(trace_payload["payload"]["status"], "failed")
        self.assertEqual(
            trace_payload["payload"]["failure_stage"],
            "response",
        )

    async def test_slo_failure_does_not_fail_current_canary(self) -> None:
        trace_events: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal trace_events
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
                trace_events.extend(json.loads(request.content)["events"])
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={
                        "accepted": 1,
                        "rejected": 0,
                        "monitor_observations": 1,
                    },
                )
            if request.url.path == "/metrics/voice-slo":
                self.assertEqual(request.url.params["window_days"], "7")
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
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["failure_stage"], "none")
        self.assertIsNone(report["failure_code"])
        self.assertEqual(report["alert_store"], "recorded")
        self.assertEqual(report["slo_alert_store"], "recorded")
        self.assertEqual(report["alert_delivery"], "not_needed")
        self.assertEqual(report["monitor_observations"], 2)
        self.assertEqual(len(trace_events), 2)
        current_event, slo_event = trace_events
        self.assertEqual(current_event["event_type"], "voice.turn.trace")
        self.assertEqual(current_event["payload"]["status"], "completed")
        self.assertEqual(current_event["payload"]["failure_stage"], "none")
        self.assertEqual(slo_event["event_type"], "voice.slo.observation")
        self.assertEqual(slo_event["subject_type"], "voice_slo")
        self.assertEqual(slo_event["payload"]["window_days"], 7)
        self.assertEqual(slo_event["payload"]["overall_status"], "fail")
        self.assertEqual(
            slo_event["payload"]["failed_checks"],
            ["turn_success_rate"],
        )

    async def test_slo_query_failure_does_not_fail_current_canary(self) -> None:
        trace_events: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal trace_events
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
            if request.url.path == "/metrics/voice-slo":
                self.assertEqual(request.url.params["window_days"], "7")
                return httpx.Response(
                    503,
                    headers=no_store_headers(),
                    json={"detail": "database detail must not be retained"},
                )
            if request.url.path == "/telemetry/event":
                trace_events.extend(json.loads(request.content)["events"])
                return httpx.Response(
                    200,
                    headers=no_store_headers(),
                    json={
                        "accepted": 1,
                        "rejected": 0,
                        "monitor_observations": 1,
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
        self.assertEqual(report["failure_stage"], "none")
        self.assertIsNone(report["failure_code"])
        self.assertEqual(report["alert_delivery"], "not_needed")
        self.assertEqual(report["slo_alert_store"], "recorded")
        self.assertEqual(report["monitor_observations"], 2)
        self.assertEqual(report["slo"]["overall_status"], "unavailable")
        self.assertEqual(
            report["slo"]["failure_code"],
            "voice_slo_query_failed",
        )
        serialized_report = json.dumps(report)
        self.assertNotIn("database detail", serialized_report)
        self.assertEqual(len(trace_events), 2)
        self.assertEqual(trace_events[0]["payload"]["status"], "completed")
        self.assertEqual(
            trace_events[1]["payload"]["overall_status"],
            "unavailable",
        )
        self.assertEqual(
            trace_events[1]["payload"]["sample"],
            {"evaluated_turns": 0, "completed": 0, "failed": 0},
        )
