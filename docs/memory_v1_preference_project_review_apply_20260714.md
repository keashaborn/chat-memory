# Memory V1 preference/project controlled review apply — 2026-07-14 UTC

## Scope

Server: seebx backend. Branch: `memory_v1_foundation`. Starting commit: `465d7c498a6fd6b6bbfc336beebb0b0ee2231436`.

The controlled review phase recorded eight append-only candidate decisions for owner `1240822d-ac9a-4096-95aa-e2b24d36ef50`: three preference candidates and five `verbal-sage` project-knowledge candidates. Every decision was `accept`. This accepted the candidate for later durable-apply consideration; it did not create durable memory or make the candidate retrievable by Memory V1.

## Locked inputs

| Input | SHA-256 |
|---|---|
| Preference/project extraction manifest | `25d59e6f777afb4cf37057278876d58af9b11502bded6c52f0a546261d7b2cee` |
| Preference/project extraction report | `fce21afc06065b2b99c0fa11bde844da2eee158e21cd8ed90506c99345829728` |
| Candidate apply manifest | `c0187b7ba6b92a2b485eb910629451df37daeebe025f2e23ad54a99b58caab5d` |
| Candidate apply report | `6d97bd2a52eb66687fca42f22769876918a880c821f33b7a2dfca6e89523c967` |
| Controlled review manifest | `3c847d0e3321bdfb80416abf4a25c9752d3ae842ddb17bd11b7d17f0c9caa946` |

The review manifest binds each candidate ID and candidate hash to a unique request ID, rationale, sorted reason codes, user reviewer identity, and `durable_apply_authorized=false`.

## Decisions

| Lane | Candidate ID | Decision | Preserved interpretation |
|---|---|---|---|
| Preference | `91cec4a9-a471-55a9-9097-1d45f29c2280` | accept | Explicit boundary against out-of-context surfacing of named personal memories |
| Preference | `008c6200-72fd-5dfa-9d9d-357b4017e95d` | accept | Vocal prominence and metaphorical lyrics as a music-recommendation dimension |
| Preference | `5f52649c-2e12-59a4-bfe2-dcc6e36ffff8` | accept | Concrete surface subject with a different underlying meaning as a separate music dimension |
| Project | `6d12fe82-b902-5117-bc38-724176aa18f6` | accept | Long-term proposed direction, not a completed capability |
| Project | `ed627de8-8042-50c1-b53f-698510087168` | accept | Historical, source-dated state with epistemic qualification |
| Project | `b32c57ee-cb63-5099-b707-4510055ff79b` | accept | Historical, time-bounded architecture state |
| Project | `d2803521-0d9d-559c-a267-104f8bf0c6cc` | accept | User-reported AI-assisted development with the ambiguous skill claim excluded |
| Project | `ca2a91b5-7ceb-5adf-aa71-7a884d8f47ba` | accept | AB design as a proposed background evaluation capability |

The two music preferences remain separate because they describe different retrieval and recommendation dimensions. Historical and proposed project states remain explicitly historical or proposed.

## Backup and transaction

Validated full production backup:

- Path: `/home/ubuntu/brains/snapshots/memory_pre_preference_project_reviews_20260714T040941Z.dump`
- Size: `73,564,550` bytes
- SHA-256: `22be0f384680119213b16e759e9a8951779bcff9fa18d30df40cac0854b411a2`
- Validation: `pg_restore --list` succeeded.

An earlier application-role dump was correctly blocked by forced RLS. Its partial file was renamed `/home/ubuntu/brains/snapshots/memory_pre_preference_project_reviews_20260714T040816Z.incomplete` and was not treated as a backup.

The production apply ran as one serializable transaction through `memory.review_preference_candidate` and `memory.review_project_candidate`. It inserted exactly eight rows: three preference reviews and five project reviews.

## Verification

- Full isolated schema/RLS/integration CI passed, including the new controlled-review integration test.
- All nine live preflight controls passed: exact owner context, application role, transaction mode, forced RLS, no direct review inserts, no direct durable inserts, controlled-function execution, hardened function ownership/configuration, and append-only guards.
- Replay returned the same eight review IDs and `database_writes=0`.
- Independent SQL found review number `1` for all rows, eight `accept` decisions, eight unique request IDs, and eight unique request hashes.
- Cross-owner visibility test returned zero review rows.
- Preference/project replacement rows, durable heads, revisions, and apply events all remained zero.
- Candidate, evidence, claim, lifecycle, outbox, project-registration, and project-space counts did not change.
- `brains.service` remained active with zero restarts; authenticated `/healthz` returned `status=ok`; authenticated `/readyz` returned `postgres=true`.

Apply report SHA-256: `7e3e7aff89cd7e31b5733ff373b8ccd4115c32f27ae10fbd4134b79827393ff7`.

Replay report SHA-256: `fa2385dc171afb11bd4d9ce45c5a2826962e7da1f0a9f543f8a79c242c49070e`.

## Stop boundary

No durable preference or project-knowledge apply was authorized or performed. Designing and dry-running the durable apply is the next separate phase.
