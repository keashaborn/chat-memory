# SeeBx Redis retirement plan v1

Status: verified stop candidate; no production action authorized by this file.

## Exact target

- Host: SeeBx backend
- Container: `brains-redis-1`
- Compose project/service: `brains` / `redis`
- Image: `redis:7` (observed runtime version `7.4.6`)
- Host publication: `127.0.0.1:6379`
- Network: `brains_default`
- Persistence bind: `/home/ubuntu/brains/redis_data` to `/data`
- Restart policy: `unless-stopped`

This plan never targets `brains-postgres-1`, `lifeswitch-postgres-current`,
Qdrant, Zep, `brains.service`, or a host-wide Docker cleanup.

## Evidence recorded 2026-08-19 UTC

The container had been running for 267,961 seconds when inspected. Aggregate
Redis evidence was:

- 203 keys: 201 strings and 2 hashes;
- 0 expiring keys and 203 persistent keys;
- 30,416 bytes of key memory and 1.33 MiB total Redis memory;
- minimum and maximum key idle time both matched the full container uptime;
- zero active application connections;
- no `GET`, `SET`, `HGET`, `HSET`, queue, stream, publish, or subscribe command
  appeared in command statistics since restart;
- only inspection commands appeared in command statistics;
- no running process exposed a Redis-related environment key;
- `brains.service` exposed no Redis or cache environment key;
- no Redis package dependency or client import exists in the active tracked
  application runtime;
- the only executable client found outside the canonical repository was the
  dormant `/home/ubuntu/brains/memory_api/app.py`; no process or systemd unit
  invokes it;
- tracked mentions in `prompt_builder.py` and `vantage_router.py` are only
  natural-language technical-topic classifiers, not Redis clients.

Persistence occupied 236 KiB. The observed files were an RDB snapshot plus an
AOF base, incremental file, and manifest. Existing persistence is recovery
material, not proof of a current consumer.

A second read-only inspection at 273,878 seconds of container uptime returned
the same 203-key/type/TTL/key-memory totals. Minimum and maximum idle time both
again equaled the complete uptime. The only command statistics were inspection
commands; there were no established port-6379 connections after inspection,
and `brains.service` had logged zero Redis, port-6379, or connection-refused
errors since the container start.

Exact runtime identity at the refresh:

- container ID:
  `9e8a36c1aefc2427b23f9f4be3af3b094b91d0506dfee6bb46e923aa36f90219`;
- image ID:
  `sha256:b4e53bb4637c329c42395eec0b86851f5314f6ed597ec217b5e4e19f7c102c11`;
- Compose configuration hash:
  `3c7eeb9e9a2e6fde8d8aab683232044ebd119ac6e7aafa10180b6ead5796fded`;
- `docker-compose.yml` SHA-256:
  `2e956db84101c7eab9b8ddfd26a4bc91db95fd028a35f62681d628b1528f63c7`;
- `docker-compose.override.yml` SHA-256:
  `ec3b1cdbb68ad1ca3949574236d8cf8162ae16d0d1bcef978c5133695b8d3c59`.

| Live persistence file | Bytes | SHA-256 |
|---|---:|---|
| `dump.rdb` | 14,119 | `fc8a3873b5b5958ff352e5cbaa77aab87856bcae2f6727322db82ba895304595` |
| `appendonly.aof.1.base.rdb` | 88 | `7a132cc2652c35e84de72fa2c2d394d56c5ed47f463be2521303dd1747eb5340` |
| `appendonly.aof.1.incr.aof` | 203,802 | `5d810a110e792885a34a8bcfe07d1a72e7d16285cd3f6bbccfefc015e567813a` |
| `appendonly.aof.manifest` | 88 | `ca465c5845ad4d4c0a2b4cda8853efeb9423cb4242a4eaaa5ecbaa00a4e75492` |

Redis reported no unsaved changes, no active save/rewrite, and successful last
RDB/AOF persistence. This refresh did not save, copy, stop, or remove anything.

## Preconditions

Before the stop, an operator must create one timestamped receipt containing:

- exact container ID, image ID/digest, configuration hash, mount source, and
  Compose file hashes;
- Redis version, uptime, key count, key-type counts, TTL counts, aggregate key
  memory, command statistics, and active connection count;
- SHA-256 and byte count for every persistence file;
- confirmation that the backup target is encrypted at rest;
- current `brains.service` Git commit and service/health state;
- current frontend Git commit and authenticated smoke-test state;
- the exact rollback command and observation window.

Do not print key names, key values, credentials, connection strings, or process
environment values into the receipt.

## Reversible stop batch

The approved execution batch must perform these actions in order:

1. Request a final Redis background save and wait until persistence reports
   success and no save is in progress.
2. Copy the RDB/AOF files into a new root-owned, mode-`0700`, encrypted-at-rest
   backup directory outside the live bind mount.
3. Hash the copied files and verify byte-for-byte hash equality with the live
   persistence files.
4. Record the pre-stop container/configuration evidence and service health.
5. Stop only `brains-redis-1`. Do not remove the container, Compose service,
   image, network, or `/home/ubuntu/brains/redis_data`.
6. Verify the Redis listener is absent.
7. Verify `brains.service`, PostgreSQL, Qdrant, Zep-dependent chat behavior, and
   the authenticated Verbal Sage text path remain healthy.
8. Repeat service and application checks after a defined observation period.

The stop is a production change and requires separate explicit authorization.

## Success criteria

The reversible stop succeeds only if:

- Redis remains stopped and port 6379 remains absent;
- SeeBx health stays green without degraded dependency flags;
- authenticated text chat succeeds;
- Zep prompt retrieval and turn-sync behavior show no Redis dependency;
- PostgreSQL and Qdrant remain healthy;
- no service repeatedly restarts or logs Redis connection errors;
- the evidence receipt and backup hashes are complete.

Any failed criterion triggers rollback.

## Rollback

Rollback starts the exact stopped container, verifies `PONG`, confirms all 203
keys are present, and repeats the SeeBx and frontend health checks. If the live
bind mount is damaged, restore the verified persistence copy before starting
the container. Rollback must not recreate Redis from an unverified image or
empty data directory.

## Later removal gate

Stopping Redis does not authorize deletion. Permanent removal requires a second
decision after the observation window and must separately authorize:

- removal of the Redis service from the Compose definition;
- removal of the stopped container;
- removal of the bind-mounted persistence directory;
- removal of the dormant `memory_api` client;
- removal of the Redis image if no other container uses it.

The verified backup and receipt must be retained according to the backend
recovery policy even after final removal.
