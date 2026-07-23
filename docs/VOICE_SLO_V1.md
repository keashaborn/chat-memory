# Governed Voice SLO V1

Status: initial production measurement contract.

This contract measures the user-visible governed voice path:

`speech -> transcription -> /response/query -> validated answer -> streamed TTS`

It does not authorize direct Realtime answer generation.

## Window and evidence threshold

- Rolling window: 1 to 30 days; default 30 days.
- Owner scope: the authenticated owner only.
- Minimum evidence: 30 completed turns for latency checks and 30
  non-cancelled turns for reliability.
- Before the minimum is met, the result is `insufficient_data`, never `pass`.
- Voice operational telemetry remains subject to the 30-day retention policy.

## Initial service objectives

| Measure | Initial objective |
| --- | ---: |
| Successful non-cancelled turn rate | >= 99% |
| Transcription p95 | <= 3,000 ms |
| Governed response p95 | <= 10,000 ms |
| TTS first-audio p95 | <= 3,000 ms |
| End-of-speech to first-audio p95 | <= 15,000 ms |

These are initial objectives, not claims that production currently satisfies
them. Targets may change only through a versioned contract update backed by
production evidence.

## Timing definitions

- `transcription_ms`: browser request start to validated transcription response.
- `response_ms`: governed chat request start to validated, persisted answer.
- `tts_first_audio_ms`: TTS request start to the first scheduled audible PCM.
- `end_of_speech_to_first_audio_ms`:
  `speech_to_first_audio_ms - speech_ms`.

The end-of-speech measure intentionally includes local silence detection,
network transit, transcription, governed generation, persistence, and TTS
startup. It excludes the user's own speaking duration.

`total_turn_ms` includes audio playback and is reported diagnostically, but it
is not an initial latency SLO because it grows with answer length.

## Privacy and authority

- Aggregates expose no transcript, answer, request identifier, or actor
  identifier.
- Source rows remain protected by the telemetry owner RLS policy.
- Client timing is valid for user-experience measurement but is not trusted for
  billing, authorization, or security decisions.
- Server-side stage timing may be added for diagnosis; user-perceived browser
  timing remains the SLO authority.

## Endpoint

`GET /metrics/voice-slo?window_days=30`

The endpoint requires the same authenticated actor boundary as other telemetry
metrics and returns explicit per-measure `pass`, `fail`, or
`insufficient_data`.
