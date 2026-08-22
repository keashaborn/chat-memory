# SeeBx platform database inventory v1

Date: 2026-08-22

Status: candidate evidence; no production migration, database write, object
retirement, deployment, restart, credential change, or deletion authority

## Purpose

This inventory separates the reusable SeeBx platform database from the retired
custom-memory attempts and the duplicate LifeSwitch catalog. It binds every
scoped relation and function to candidate runtime, database-internal,
operational, migration, extension, or unproven evidence. The same run freezes
owners, ACLs, RLS, columns, functions, policies, triggers, constraints,
indexes, extensions, roles, memberships, exact table counts, and sequence
state without reading or emitting row content.

## Authoritative receipt

- candidate commit:
  `fdffcbe0cecb5142d2ebc560e2ea1232394bddff`;
- report:
  `/var/backups/seebx-cleanup/platform-database-consumers-v1/20260822t083100z/consumer-audit.json`;
- report SHA-256:
  `42177da9a230dc28a14651b75e3d500cee6771d96e1f2198df9ae616f3b2bbf3`;
- object catalog SHA-256:
  `05aa5673634f94a02096795f8d0b40d79b036bd67815dcb482de8763ba6ccc47`;
- governance manifest SHA-256:
  `3394fc40c3fe29e3d4b4bd029acbb76059b02c45bae8e1a9bd7f8a8e0d121cb8`;
- report owner/mode: `root:root` / `0600`;
- report status: `pass`;
- deletion authority: `false`.

The committed auditor runs catalog reads inside repeatable-read, read-only
transactions. Database definitions are represented only by SHA-256 values.
Passwords, password verifiers, and application row values are absent.

## Exact current database inventory

| Schema | Tables | Exact rows | Other relations |
|---|---:|---:|---:|
| `ai_operations` | 4 | 58 | 0 |
| `catalog_dev` | 14 | 1,711 | 0 |
| `chat_history_private` | 2 | 22 | 0 |
| `chat_integrity` | 1 | 101 | 0 |
| `lifeswitch_usage` | 2 | 708 | 0 |
| `memory` | 160 | 35,118 | 6 views |
| `memory_ingest_private` | 7 | 1,216 | 0 |
| `public` | 14 | 3,676 | 1 sequence |
| `trusted_web` | 4 | 300 | 1 view |
| `user_settings` | 2 | 20 | 0 |
| **Total** | **210** | **42,930** | **8** |

These counts are exact at the receipt transaction, not PostgreSQL estimates.
They can change while the current service continues handling requests, so the
final migration must freeze and recount them.

## Consumer result

The catalog contains 794 relations/functions and 3,382 dependency edges.

| Classification | Objects | Tables | Exact table rows | Meaning |
|---|---:|---:|---:|---|
| application direct | 20 | 14 | 3,206 | candidate platform adapter references |
| database internal reachable | 26 | 12 | 1,250 | dependency reachable from a direct consumer |
| extension owned | 118 | 0 | 0 | PostgreSQL extension authority |
| migration only | 13 | 6 | 539 | installation/history reference only |
| operational reference only | 18 | 13 | 1,701 | verifier or migration utility only |
| unproven | 599 | 165 | 36,234 | no candidate runtime or reachable consumer |

Operational and migration references do not create runtime retention. An
unproven result is a review gate, not proof that deletion is safe.

## Cross-database ambiguity correction

Both the old platform database and the isolated LifeSwitch database contain a
schema named `catalog_dev`. Three candidate adapters use the isolated
`LIFESWITCH_POSTGRES_DSN` exclusively:

- `seebx/adapters/lifeswitch_catalog_postgres.py`;
- `seebx/adapters/lifeswitch_foods_postgres.py`;
- `seebx/adapters/lifeswitch_meal_plans_postgres.py`.

The platform audit excludes those exact files from platform-runtime seeding.
After that correction, the old platform `catalog_dev` has zero candidate
runtime consumers. Its 20 relations/functions are migration-only or
operational-reference-only. It remains preserved until isolated-catalog parity,
backup/restore, and cutover are proven.

## Canonical disposition

The clean platform database should retain only the following logical
capabilities:

- AI Operations incidents and delivery evidence;
- owner-bound threads, chat log, attachments, and active-thread selection;
- transcript integrity and owner-bound deletion functions after legacy-outbox
  decoupling;
- trusted-web cache, audit, transcript, rate-window, and monitor evidence;
- platform usage, telemetry, response preferences, and voice-session authority;
- required extensions and the minimum roles/grants supporting those paths.

The following remain outside the canonical platform target:

- old `catalog_dev`: consolidate into the isolated LifeSwitch catalog;
- `memory`: final verified archive/restore, then retire after Zep cutover proof;
- `memory_ingest_private`: replace the two chat-clear dependencies, verify zero
  nonterminal work, then archive/retire;
- `public.chat_messages`, `public.chat_sessions`, `public.feedback_signals`,
  `public.vantage_answer_trace`, `public.vs_profiles`, and their sequence:
  archive/retire after exact recovery proof;
- `public.vb_form_entries`, `public.vb_form_templates`, and
  `public.vb_form_versions`: migrate valid owner-bound rows into the isolated
  LifeSwitch Forms schema, quarantine invalid-owner rows, verify exact
  reconciliation and rollback, then retire the old source tables.

The production audit's 26 database-internal objects are transitional
retention, not an assertion that every object belongs in the final clean
database. Twelve are in `memory_ingest_private` because the production
chat-clear functions still depend on the legacy outbox boundary. The verified
disposable forward migration reduces the internal-reachable set to 14 and the
`memory_ingest_private` subset to zero; production remains unchanged.

## Exact disposition receipt

The fail-closed disposition builder verified every object, role, membership,
extension, table count, governance-section hash, and source-audit hash before
writing this private immutable receipt:

- receipt:
  `/var/backups/seebx-cleanup/platform-database-disposition-v1/20260822t092028z/disposition.json`;
- receipt SHA-256:
  `08055f709d63fb8eb40a41efddd9a3606e90b8b5a85a99df431fe334875e8b10`;
- owner/mode: `root:root` / `0600`;
- objects dispositioned: 794;
- deletion authority: `false`;
- production-change authority: `false`;
- clean-baseline generation: blocked only by 12 current
  `memory_ingest_private` dependencies, object-set SHA-256
  `341dd2f6c9ad3a5296bb3e758efc6edd9f1cb266890a6dee75e2d783793e3d9a`.

The extension review found no non-extension platform column dependency on
`citext`; all four `pg_trgm` indexes belong to the duplicate `catalog_dev`; and
the only `unaccent` callers belong to `catalog_dev` or retired `memory`.
Those three extensions are excluded from the clean platform target.
`pgcrypto` remains because the retained
`ai_operations.record_monitor_observation_v1` function calls `public.digest`.
`plpgsql` remains because retained platform functions use it.

## Forward-state zero-blocker receipt

The exact clean candidate commit
`0ee2b9fc07ebc63ef7acfa919ff63545296847fb` was restored and migrated only in
disposable database `ls_zep_zep_audit_20260822t1058z`. Its complete 794-object
audit produced:

- audit SHA-256:
  `8ba29409fcaf5acb8a686d4fbd892a796c50399ba0314e36a4d18984d40bf2eb`;
- governance manifest SHA-256:
  `a5aa06018328e2baf1e5179641f06834168cc5cd56121b5798c931af02e5e130`;
- object catalog SHA-256:
  `4c34b82a22bef7d92a845e69ee796f73c15f5049632bb9ced795844207f52647`;
- disposition SHA-256:
  `0a810208224f56f369462d0faf73c642766acb8723763a5a2b607cbf0ad14042`;
- `baseline_generation_allowed: true`, `baseline_blockers: []`, and zero
  database-internal-reachable objects under `memory_ingest_private`.

Rollback restored the exact source baseline and the disposable database was
deleted. This supersedes the 12-object blocker for candidate baseline design,
but it does not alter or authorize production.

## Governance findings

The governance manifest freezes 10 schemas, 218 relations, 2,867 columns, 576
functions, 315 policies, 232 non-internal triggers, 2,302 constraints, 777
indexes, five extensions, 42 non-system roles, nine memberships, 210 exact
table counts, and one sequence.

The retained owner-scoped chat, usage, integrity, telemetry, voice, and user
settings tables have RLS enabled and forced. The application login
`brains_app` is not a superuser and does not bypass RLS. The administrative
login `sage` is superuser and must not become a service credential in the clean
target.

Two security-normalization items remain before clean installation:

1. `trusted_web` is owned by the login role `brains_app`; its internal cache,
   request-window, and retrieval-audit tables do not use RLS. The target should
   use a separate NOLOGIN owner with least-privilege application grants, and
   owner-scoped transcript data must retain forced RLS.
2. Forty-two non-system roles include many retired custom-memory maintainers.
   The clean role baseline must be generated from retained capabilities rather
   than copied wholesale.

## Required next gate

1. Run the already-decided Forms valid/quarantine migration and rollback proof
   against a separately isolated disposable LifeSwitch database.
2. Generate the clean platform baseline only from the verified zero-blocker
   forward-state disposition.
3. Build a clean platform database from versioned migrations in a disposable
   PostgreSQL instance.
4. Copy only approved rows and verify exact counts, keys, sequence state,
   owners, grants, RLS, function, policy, trigger, constraint, and index hashes.
5. Run application contracts, cross-owner denial, deletion/export, search,
   voice, admin, and paired frontend tests.

No passing result in this document authorizes a production database change.
