# OpenAI-Only Consolidation Checkpoint — 2026-06-30

## Backend / Brains

Path: `/opt/chat-memory`

Completed:

- Backend model gateway constrained to OpenAI.
- Provider-prefixed or stale models such as `xai:grok-*` normalize/fallback to approved OpenAI models.
- Added backend OpenAI realtime session endpoint:
  - `POST /voice/openai/session`
- Added canonical backend OpenAI TTS endpoint:
  - `POST /voice/tts`
- Removed legacy xAI/Grok WebSocket bridge:
  - removed `/ws/voice`
  - removed `XAI_API_KEY` runtime path
  - removed `api.x.ai` runtime path
- Removed duplicate legacy backend `/tts` route.
- Backend retains `OPENAI_API_KEY`; frontend does not need it.

Validated:

- `brains.service` active.
- Backend forbidden-provider audit clean:
  - no `XAI_API_KEY`
  - no `api.x.ai`
  - no `VOICE_WS_TOKEN`
  - no `/ws/voice`
  - no `@app.websocket`
- `/voice/tts` dry-run returns 200.
- `/voice/openai/session` dry-run returns 200.

Relevant commits:

- `6d7a3ad Constrain backend model gateway to OpenAI`
- `62fb27e Add OpenAI realtime voice session endpoint`
- `91f735b Add backend OpenAI TTS endpoint`
- `25f96a6 Remove legacy xAI voice bridge`
- `ac59b61 Remove duplicate legacy TTS route`

## Desired Runtime Shape

Browser → Next.js BFF → Brains → OpenAI

Only Brains should hold `OPENAI_API_KEY`.
