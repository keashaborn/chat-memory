# Legacy Vantage Initiator Controlled Shutdown — 2026-07-14

## Decision

`vantage-initiator.service` was stopped and disabled on the seebx backend. The daemon was a Vantage-scoped legacy mutation system, not Memory V1 automation: it continuously created initiator jobs and drive snapshots and scanned, consolidated, or decayed legacy Vantage fact/card data for `default`, `EVA`, `MORGAN`, `RILEY`, and `RESSE`.

No database row, schema, route, source file, Qdrant point, or systemd unit was deleted. The shutdown is reversible. Future Memory V1 automation must be account-owner-scoped, evidence-preserving, append-only where applicable, transactionally audited, replay-safe, and independent of response-mode/Vantage identity.

## Final pre-stop state

Server: seebx backend.

- Controllers: `5`
- Drive snapshots: `299549`; maximum `snapshot_id=299549`
- Jobs: `767372`; maximum `job_id=767372`
- Job runs: `767380`; maximum `run_id=767380`
- Unfinished job-run rows at the stop boundary: `1`
- Job-run rows with a non-null error field: `16`

The unfinished legacy row was preserved unchanged. It records the exact shutdown boundary and was not rewritten to fabricate a completed outcome.

## Final archives

Snapshot directory:

`/home/ubuntu/brains/snapshots/vantage_initiator_shutdown_20260714T190431Z`

| File | Bytes | Mode | Restore catalog | SHA-256 |
|---|---:|---:|---:|---|
| `legacy_vantage_schemas.dump` | 73,573,928 | `600` | 147 entries | `ce0e1b34c91e5bcdfaf6c9d1bb71c499fbb8eac8fdc426bce538935d7c18ee1d` |
| `public_vs_profiles.dump` | 2,913 | `600` | 4 entries | `775f0ea36759f7a21668bd0cefcd189ca0f27db278e0754b6be6166738c8dc1c` |
| `vantage-initiator.service` | 556 | `600` | n/a | `e8977020ee5a0cce89c6163b544098581565afdaa4be4e561eff776087c8470c` |

The first archive contains `vantage_initiator`, `vantage_card`, `vantage_fact`, `vantage_profile`, and `vantage_identity`. The second contains only `public.vs_profiles`. Both custom-format archives passed `pg_restore -l`; required schema/table entries were checked explicitly.

## Shutdown proof

After the service stopped, two samples 15 seconds apart were identical:

```text
299549|299549|767372|767372|767380|767380
299549|299549|767372|767372|767380|767380
```

The fields are drive-snapshot count/max ID, job count/max ID, and job-run count/max ID. Final systemd state was:

```text
MainPID=0
ActiveState=inactive
UnitFileState=disabled
```

## Production verification

- Brains remained active. Authenticated `/healthz` and `/openapi.json` returned HTTP `200`.
- `/vantage/query` remained registered. The live API retained 27 nutrition paths and 37 training paths.
- Postgres, Qdrant, Redis, and the vetting worker remained running.
- Qdrant `memory_claim_v1` remained `green` with six points.
- Verbal Sage `verbalsage-v2.service` remained active; `http://127.0.0.1:3010/` returned HTTP `200`.
- Memory V1 owner-scoped read-only verification returned `173 evidence / 6 claims / 3 preferences / 5 project heads / 6 outbox / 152 traces`.
- The same Memory V1 query under a different actor UUID returned zero for every relation, confirming forced-RLS isolation remained effective.

## Boundary

This shutdown only stops legacy background mutation. It does not retire the remaining legacy HTTP routes, prompt-time preference/profile cards, raw-memory retrieval, temporal metadata code, gravity/VB-desire modules, `vantage_identity.rag_policy` internal reads, or stored legacy data. Those require deterministic caller/consumer removal and regression verification in separate batches.

Memory V1 retrieval remains non-injecting at this boundary. Memory V1 consolidation, contradiction evaluation, salience change, and lifecycle automation have not been enabled as background jobs.

## Rollback

Server: seebx backend.

```bash
sudo systemctl enable --now vantage-initiator.service
systemctl show vantage-initiator.service -p ActiveState -p UnitFileState -p MainPID --no-pager
```

Rollback resumes mutation of the preserved legacy schemas. Use it only if a verified production dependency on the legacy scheduler appears.
