# SeeBx admin and operations inventory v1

Audited at `2026-08-22T07:01:15Z`. This document is evidence for cleanup and
cutover planning. It does not authorize installation, database migration,
service restart, timer change, or production deployment.

## Audited revisions

| Surface | Production | Isolated candidate |
| --- | --- | --- |
| SeeBx backend | `49f9e60cf4321c8e42c359845c1a62a8c987614d` | `e18b54125fb903f685e9eea35dd422d9ac30a105` |
| LifeSwitch frontend | `858b61527186571cabb5580dc5159fdf6b69ae2e` | `00c9671a1e0c14ab6c7dbd97ea1d8718858317bc` |

Both candidates were clean. The production frontend retained six untracked
`.next.rollback-*` directories; they were treated as protected recovery
artifacts and were not changed.

## Canonical admin flows

| Capability | Frontend boundary | SeeBx boundary or authority | Disposition |
| --- | --- | --- | --- |
| Admin access gate | `/api/admin/access` | fresh Supabase identity and role | retain |
| User directory, role, tier, password setup, deletion | `/api/admin/users/**` | server-only Supabase administrator client | retain; Owner-only mutations remain separate from SeeBx |
| Access requests | `/api/admin/access-requests/**` | Supabase `access_requests` | retain |
| AI Operations inbox | `/api/admin/ai-operations/**` | `/admin/ai-operations/incidents**`, `ai_operations` PostgreSQL schema | retain; candidate forwards verified bearer and explicit read/manage capability |
| Conversation and memory export | `/api/admin/export` | `GET /conversation/export` | retain; candidate replaces retired `/user/{id}/export`, forwards verified bearer, and preserves hash/no-store metadata |
| Clear recent or all chat history while retaining memory | `/api/admin/forget_recent`, `/api/admin/clear_chat_history` | `POST /chat-history/clear` | retain; exact owner identity and bearer required |
| Clear all chat plus Zep memory | `/api/admin/delete_all` | `DELETE /memory/chat-and-zep/clear` | retain behind `VS_ALLOW_DELETE_ALL=true`; production currently omits the flag and therefore fails closed |
| Voice health | `/api/admin/voice-health` | `GET /metrics/voice-slo` | retain; current synthetic canary defect must be corrected by candidate cutover |
| Response inspector session | `/api/admin/inspector-session` | signed, host-bound inspector session cookie plus `inspector.view` | retained and explicitly named in frontend candidate `00c9671` |
| Development-history archive | admin panel only | quarantined, non-authoritative archive status | retain inert until separately approved archive backend exists |

## Proven retired or dormant paths

- The production-only `app/api/chat/inspect/route.ts` calls retired
  `/vantage/query` and reads `vs_vantage_*` cookies. No frontend caller was
  found. The isolated frontend candidate deletes this route in commit
  `ca2c7fe`.
- Legacy backend deletion paths under `/threads/**` and `/user/{id}/data` or
  `/recent` deliberately return HTTP 410 and name the canonical erasure route.
  They are compatibility refusals, not active data effects.
- The candidate scheduled-job catalog retires `chat-memory-git-sync.service`
  and `.timer`. They remain active in production until immutable release
  cutover; they must not be removed independently.
- `lifeswitch-release-integrity-v1` exists in candidate templates but is not
  installed on either production server. Installation belongs to immutable
  release cutover, not this inventory.
- No Qdrant, RESSE, Vantage, or retired-memory timer was present in the live
  SeeBx timer list.

## Current PostgreSQL authority split

The running `brains.service` has two effective DSNs:

- `POSTGRES_DSN` -> database `memory` on local port 5432;
- `LIFESWITCH_POSTGRES_DSN` -> database `lifeswitch` on local port 55433.

The `lifeswitch` database owns product schemas such as catalog, nutrition,
plan, training, snapshots, and chat bindings. It does not currently contain
the audited admin/operations schemas.

The `memory` database is still live authority for more than retired memory:

| Store | Audited live rows | Current use |
| --- | ---: | --- |
| `public.telemetry_event` | 898 | telemetry and voice SLO evidence |
| `ai_operations.monitor_incident_v1` | 8 | admin incident inbox |
| `ai_operations.monitor_incident_event_v1` | 38 | append-only incident events |
| `ai_operations.monitor_alert_delivery_v1` | 3 | alert delivery state |
| `ai_operations.monitor_alert_delivery_event_v1` | 9 | append-only delivery events |
| `trusted_web.retrieval_audit` | 164 | retrieval governance evidence |
| `trusted_web.response_transcript_v1` | 2 | governed response transcripts |
| `trusted_web.source_cache` | 1 | trusted-web cache |
| `lifeswitch_usage.ai_usage_event_v1` | 707 | content-free AI usage accounting |
| `lifeswitch_usage.ai_actor_registry_v1` | 1 | actor registry |

These tables, their RLS policies, security-definer functions, and conversation
tables prevent immediate retirement of `brains-postgres-1`. A later database
consolidation must migrate each retained authority into the clean database,
verify owners/grants/RLS/counts and application DSNs, then remove the old
database as a separate rollback-safe cutover.

## Scheduled operations evidence

At the audit point, the latest runs succeeded for:

- AI Operations alert delivery;
- trusted-web monitor;
- trusted-web ODS cache;
- telemetry retention;
- Git synchronization.

The voice synthetic canary failed hourly with `upstream_http_422` at the TTS
stage. Production canary v1.3 sends client-owned `model`, `speed`, and
free-form `instructions`; the current TTS boundary correctly rejects those
fields. Candidate canary v1.4 sends server-governed `conversation_style`
instead and expects the canonical conversation response runtime.

Focused backend candidate verification passed 47 tests across voice canary,
TTS governance, AI Operations, export, operational health, and telemetry.
Focused frontend candidate verification passed 36 tests across admin access,
AI Operations, archive quarantine, voice health, inspector sessions, and data
controls.

## Open cleanup gates

1. Keep the candidate export repair, inspector-session rename, and dead Vantage inspector retirement in
   the frontend release set.
2. Verify the repaired voice canary in a disposable or installed immutable
   candidate before production activation.
3. Decide and execute the retained `memory` database migration only after a
   table/function/role/RLS/data reconciliation package and rollback proof exist.
4. Install release-integrity monitoring and retire Git synchronization only as
   part of the immutable release cutover.
