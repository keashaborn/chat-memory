# Governed voice synthetic canary v1

Status: implemented but not activated until a dedicated configuration file is
installed.

## Purpose

The canary verifies the production voice dependency chain without creating a
user-visible chat:

1. Generate a fixed, non-user TTS phrase.
2. Wrap the returned PCM as WAV and submit it to OpenAI transcription.
3. Submit the transcript to the existing authenticated `/response/query` path
   with `no_store=true` and no `thread_id`.
4. Submit the validated governed answer to TTS.
5. Store only bounded timing/status telemetry under a dedicated synthetic actor.
6. Evaluate the synthetic actor's rolling 30-day `voice_slo_v1` aggregate.

The runner never logs or stores the transcript or answer. It outputs one
aggregate-only JSON result. It exits nonzero on a current dependency failure or
after a mature SLO window fails a threshold.

## Boundaries

- The canary actor must be a UUID used only for this service.
- The canary uses the backend service token and must call a loopback URL.
- `/response/query` is always called with `no_store=true`; it cannot create a
  thread or normal transcript/answer rows.
- The telemetry record has `synthetic=true`, no thread ID, no text, and the
  `voice_turn_trace_v1` timing schema.
- The systemd service reads only `/etc/verbalsage/voice-canary.env`; it does not
  receive the application database or OpenAI credentials.
- A webhook is optional. Its payload contains only the contract version, stable
  failure stage/code, and timestamp.

## Activation file

`/etc/verbalsage/voice-canary.env` must be owned by root and mode `0600`:

```text
VOICE_SYNTHETIC_CANARY_ENABLED=authorized
VOICE_CANARY_ACTOR_USER_ID=<dedicated UUID>
VOICE_CANARY_BASE_URL=http://127.0.0.1:8088
VOICE_CANARY_TIMEOUT_SECONDS=90
VS_SERVICE_TOKEN=<same backend service token>
# Optional:
# VOICE_CANARY_ALERT_WEBHOOK_URL=<private alert receiver>
```

Install the committed service and timer, run the service once, inspect its
single JSON journal record, and only then enable the hourly timer.
