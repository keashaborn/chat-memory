# SeeBx runtime asset ledger v1

Status: read-only runtime inventory and cleanup disposition; no stop, delete,
drop, migration, deployment, or production-write authority

Evidence date: 2026-08-18 UTC

Production source: `49f9e60cf4321c8e42c359845c1a62a8c987614d`

Cleanup integration pre-retirement evidence commit:
`15d28c5665641f20a25a558f2727ca799771a070` on
`codex/seebx-cleanup-integration-20260818`; 107 commits ahead of production,
clean, unpushed, and undeployed. The bounded voice-canary repair is
commit `e7005538` within this integration line.

## Purpose

This ledger extends the structural design map to the operational objects that
source-code import graphs do not prove: services, timers, cron entries,
containers, listeners, databases, schemas, and runtime settings. An asset is
not retired until both the code graph and this runtime graph show zero required
consumer.

## Host and network surface

| Asset | Verified state | Disposition | Gate |
|---|---|---|---|
| Ubuntu host | EC2, 4 vCPU, 15.7 GiB RAM, 96 GiB root volume | KEEP | Continue patching and capacity monitoring; Ubuntu is not the cleanup problem |
| Caddy | Public listeners on 80/443 | KEEP | Preserve authenticated routing; include configuration in deployment evidence |
| SSH | Listener on 22; UFW permits public IPv4/IPv6 ingress, while AWS permits only two explicit public `/32` addresses | KEEP, HARDEN SEPARATELY | Reconcile the two named operator addresses, prefer SSM/private access, then remove public SSH when an independently tested recovery path exists |
| `brains.service` | Active; runs `/opt/chat-memory` with Uvicorn on `0.0.0.0:8088`; UFW permits 8088 only from `172.31.0.0/16`; AWS permits it only from the LifeSwitch frontend at `172.31.43.160/32` | KEEP + RENAME/CONSOLIDATE | Narrow UFW to the same exact frontend source before final service rename; preserve SSM/control-plane recovery |
| Local PostgreSQL listeners | 5432 and 55433 on loopback | TRANSITIONAL + KEEP | 5432 is the old platform database; 55433 is the isolated LifeSwitch database |
| Local Redis listener | 6379 on loopback; no live service setting or TCP consumer; inspection saw only its own client | RETIRE CANDIDATE | Preserve a hashed data artifact, then stop reversibly before container/data removal |

The instance has public IPv4 `13.58.169.54` and security group
`launch-wizard-10` (`sg-09ca1e52ffde0fd22`). AWS does not expose 8088 to the
internet. The group still carries stale-looking ingress for 6333, 6379, 5432,
and 8080 even though those services are absent or loopback-only. Each rule
requires one final source-owner check, then removal and a logical group rename.

## Services and timers

| Unit | Verified state | Disposition | Required action |
|---|---|---|---|
| `brains.service` | active | KEEP + CONSOLIDATE | Preserve production until reviewed candidates pass deployment gate |
| `chat-memory-git-sync.timer` | active daily | KEEP | Verify branch/remote policy and ensure it is the only automated Git writer |
| `trusted-web-monitor-v1.timer` | active | KEEP + CONSOLIDATE | Fold into the single search capability without losing audit/cache behavior |
| `trusted-web-ods-cache.timer` | active | KEEP + CONSOLIDATE | Same search capability and retention contract |
| `telemetry-retention-v1.timer` | active | KEEP | Recast behind the OpenTelemetry contract before backend selection |
| `ai-operations-alert-delivery-v1.timer` | active; delivery not fully configured | DECIDE/REPAIR | Name the consumer and alert backend; do not keep a silent timer indefinitely |
| `voice-synthetic-canary.timer` | active hourly; service currently fails with HTTP 422 | KEEP + REPAIR | Candidate `e7005538` removes the rejected free-form field and uses server-owned `conversation_style=direct` |
| Gravity service/timer | disabled | RETIRE CANDIDATE | Confirm no external caller or rollback authority, then remove through an exact manifest |

The canary failure is internal contract drift. The route intentionally rejects
caller-supplied TTS instructions, while the canary still sent them. The OpenAI
model, voice, and speech endpoint remain compatible. Fifteen focused tests pass
for the isolated repair. The superseded Fractal Monism v0.2 compiler, runtime,
selector, data bundle, and tests are isolated by the separately hash-bound
retirement manifest; Relational Monism v0.4 is the sole active philosophy
runtime in the cleanup candidate.

## Cron

The `ubuntu` crontab contains one active legacy memory job:

```text
0 3 * * * cd /opt/chat-memory && ./eval_all_users.sh >> /opt/chat-memory/logs/cron_eval_all.log 2>&1
```

`eval_all_users.sh` targets Qdrant collection `memory_raw` on
`127.0.0.1:6333` and invokes the retired evaluation/card path. Qdrant is not
running. Recent scheduled runs add start records but perform no useful memory
work; older runs also failed when the cron environment lacked the provider key.

Disposition: **RETIRE CANDIDATE**. The removal batch must save the exact
crontab, hash the script and relevant log, remove only this line, wait past one
scheduled window or run an equivalent verification, and prove Zep/chat behavior
is unchanged. It must not delete the script or historical log in the same
batch.

## Containers and storage

| Container | Verified state | Data boundary | Disposition |
|---|---|---|---|
| `lifeswitch-postgres-current` | healthy; digest-pinned PostgreSQL; port 55433; named volume; internal network | canonical LifeSwitch domain data | KEEP |
| `brains-postgres-1` | running PostgreSQL 16; port 5432; host bind for data | transitional platform/chat/search/telemetry data plus retired schemas | KEEP DURING MIGRATION |
| `brains-redis-1` | running Redis 7; port 6379; host bind; 203 persistent keys/~1.30 MB; key families are 200 `open-webui`, two `mem`, and one `tool_servers`; no TTLs; last durable save 2026-08-16T04:08:10Z | no verified live capability owner; retained data belongs to retired Open WebUI/memory/tool-server surfaces | RETIRE CANDIDATE |

Docker is the current production container engine. Rootless Podman remains the
selected Work Runner engine. Converting the production host from Docker to
Podman is not part of cleanup and would add avoidable risk.

## PostgreSQL databases

### Isolated LifeSwitch PostgreSQL

The live service uses `127.0.0.1:55433/lifeswitch`. Retained schemas are
`catalog_dev`, `lifeswitch_chat`, `lifeswitch_nutrition`, `lifeswitch_plan`,
`lifeswitch_snapshot`, `lifeswitch_training`, and the minimal required
`public` surface. This database remains the canonical LifeSwitch domain store.

### Transitional platform PostgreSQL

The live service uses `127.0.0.1:5432/memory`. The database is approximately
1.48 GB and cannot be removed while production still reads its active platform
schemas.

| Database/schema | Disposition | Gate |
|---|---|---|
| live `memory` database | MIGRATE REQUIRED PLATFORM SURFACE, THEN RETIRE | Generate exact object/function/role/caller ledger and prove disposable replay |
| `public`, `chat_history_private`, `chat_integrity`, `trusted_web`, `lifeswitch_usage` | MIGRATE/RENAME AS DECLARED | Row, function, ACL/RLS, trigger, and application parity |
| old `catalog_dev` | CONSOLIDATE | Move remaining callers to the isolated LifeSwitch catalog; one writable catalog afterward |
| `ai_operations` | DECIDE | Keep only if a named admin/alert consumer remains |
| `user_settings` | REBUILD OR RETIRE | Current API is unmounted and tables are empty |
| `memory_ingest_private` | DECOUPLE THEN RETIRE | Remove deletion-function dependency and preserve terminal receipt evidence |
| `memory` schema and Vantage schemas | ARCHIVE THEN RETIRE | Zero Python, SQL-function, timer, cron, frontend, and recovery dependency |
| `memory_extraction_v2_20260714_test` | RETIRE CANDIDATE; 1,380 MB, 12 application schemas, 117 tables, zero connections, zero repository/config references | Snapshot/export requirement and exact drop manifest |
| `memory_v1_pet_core_clone_20260724` | RETIRE CANDIDATE; 1,405 MB, 14 application schemas, 229 tables, zero connections, zero repository/config references | Same gate; clone is not production authority |
| `lifeswitch_training_family_stage_20260727` | RETIRE CANDIDATE; 15 MB, three application schemas, 29 tables, zero connections, zero repository/config references | Confirm migration/evaluation evidence is preserved elsewhere, then exact drop manifest |

## Security configuration debt

The root-restricted Docker Compose configuration and an old backup contain
hard-coded database and retired API credentials. Secret values are deliberately
excluded from this ledger. File permissions reduce casual exposure but do not
make tracked/static credentials acceptable, especially because Docker access
can reveal container environment values.

Disposition: **ROTATE AND EXTERNALIZE** in a separate bounded security batch.
The batch must inventory every consumer without printing values, install
mode-restricted environment or managed-secret sources, rotate the active
PostgreSQL role credential, remove the retired token, verify both databases and
the application, and retain an encrypted recovery path. It must not combine
credential rotation with database drops or the 96-commit application deployment.

## Runtime settings

The live service currently receives:

- platform PostgreSQL at `127.0.0.1:5432/memory`;
- isolated LifeSwitch PostgreSQL at `127.0.0.1:55433/lifeswitch`;
- Zep credentials;
- OpenAI credentials;
- no `REDIS_URL`.

Secret values are not recorded in this ledger. Retired Qdrant settings and old
memory flags are removed only after the production package, unit drop-ins,
timers, cron, and frontend callers all show zero dependency.

## Immediate bounded batches

1. Finish full integration-candidate tests and production-equivalent nonpublic
   process verification. Review the 96-commit line as one declared structural
   release; do not deploy the canary repair by extracting an unreviewed subset.
2. Deploy only after route/OpenAPI/data/identity/Zep/voice/search/domain parity,
   rollback, and frontend bearer-forwarding evidence; then run one explicit
   synthetic voice canary and verify the timer returns healthy.
3. Retire only the legacy `eval_all_users.sh` crontab line with a crontab
   rollback file and post-window verification.
4. Preserve a hashed Redis data/config manifest and stop Redis reversibly; wait
   through an observation window before removing its container or data.
5. Produce an exact PostgreSQL object/caller/row/ACL ledger for the three clone
   databases and retired schemas. Database drops remain later, separately
   authorized actions.
6. Rotate/externalize static Docker credentials without combining the change
   with a database drop or application deployment.
7. Preserve the exact frontend-only AWS 8088 rule, narrow UFW 8088 from the
   full VPC to that frontend `/32`, and retire stale 6333/6379/5432/8080 rules
   after final source-owner verification; logically rename the security group.
8. Archive historical Memory/Vantage/RESSE documents and code outside the
   active package only after controlling and rollback evidence is indexed.

## Completion rule

No runtime asset disappears from this ledger until its replacement, named
consumer, data authority, recovery artifact, and post-change verification are
explicit. A stopped service is not deleted; a migrated database is not dropped;
and an archived code tree is not removed until the next independently reviewed
batch.
