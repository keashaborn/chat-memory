# Schema Ledger V1 evidence and authority

## Baseline

The candidate was built from production commit `d554a3d9756ede7cd71c76b2a8de08bf26b14886`, tree `5e747e056221b1a037c723e57db51885424c8245`, 304 refs with hash `0b631be16a1a04a3eb3d6ff78bd845d891eaaa007de2db0b2143e99b697a7c1f`, and 216 registered worktrees with hash `ae6d549d88eabbdcf89f06beb45081c1f373eecc227e8d0d77bd61f5fa38a47f`. Production remained clean during each source inventory. PostgreSQL catalog access ran in one `REPEATABLE READ, READ ONLY` transaction with `transaction_read_only=on`. No application table rows were selected. The schema-only dump was hashed after removing only PostgreSQL's matched volatile `\restrict`/`\unrestrict` token lines and was not retained. Qdrant access used only collection-list and collection-detail GET endpoints; no point, scroll, search, or payload-value endpoint was used.

This is a proposed initial intended baseline, not proof of historical migration application. PostgreSQL remains live authority before and after candidate activation. The ledger becomes intended-history authority only when separately reviewed and committed.

## Sanitized production inventory

The catalog evidence contains 11,967 individually fingerprinted objects:

| Object kind | Count |
|---|---:|
| schemas | 18 |
| relations | 264 |
| columns | 3,395 |
| constraints | 2,452 |
| indexes | 957 |
| sequences | 13 |
| views/materialized views | 6 |
| functions/procedures | 588 |
| triggers, including internal constraint triggers | 1,949 |
| RLS policies | 303 |
| types/domains/ranges/enums | 48 |
| extensions | 5 |
| effective grants | 1,969 |
| database-scheduled jobs | 0 |

The `memory` schema accounts for 157 relations, 2,142 columns, 1,981 constraints, 618 indexes, 396 functions/procedures, 1,541 triggers, 284 policies, 43 types, 2 views, and 1,277 grant records. Of its 157 relations, 147 have RLS enabled and forced. Across all application schemas, 164 of 264 relations have RLS enabled and forced. Installed extensions are `citext 1.6`, `pg_trgm 1.6`, `pgcrypto 1.3`, `plpgsql 1.0`, and `unaccent 1.1`. Neither `pg_cron` nor TimescaleDB scheduling is installed, and no scheduler table rows were read.

Repository evidence binds 667 SQL files: 276 forward, 208 rollback, 9 bootstrap, 1 other schema source, and 173 test fixtures. It also binds 864 non-SQL runtime candidates by path/blob/SHA-256, including 361 that directly reference SQL paths. Operational metadata binds 31 systemd service-unit files that reference `/opt/chat-memory`, 24 linked timers (17 memory-prefixed), and zero matching `/etc/cron.d` files. Timer schedule expressions are not retained; only their field count and deterministic hash are stored.

The SQL lexical safety inventory is descriptive, never authorization: 291 files contain grant/revoke operations, 254 object drops, 128 data updates, 73 inserts, 35 role mutations, 10 deletes, and 2 truncations. A flagged file is not run. All production-oriented SQL apply state and execution ownership remain `unverifiable` because production has no migration-history relation.

Qdrant reports version 1.11.0 and 23 collections. Four collections expose payload-index schema metadata. The memory collections use owner-filter fields where configured. This is evidence of derived-index configuration only; it does not make Qdrant authoritative and does not certify application-level owner filtering.

## Lane separation

The ledger keeps these policy/authority lanes distinct: personal memory; response preferences; life preferences; projects; LifeSwitch typed live context; Fractal Monism response policy; audit/review; derived-index coordination; other application schemas; and schema governance. Fractal Monism is policy-only and never a memory owner or evidence source. LifeSwitch live context stays separate from durable personal-memory claims. Qdrant collections stay outside PostgreSQL schema authority.

## Canonical data contract

All JSON is UTF-8, ASCII-escaped, key-sorted, compact, newline-terminated, integer/boolean/string/null only, and byte-for-byte canonical. Duplicate fields, floats, CR, NUL, control characters, unknown top-level fields, noncanonical paths, duplicate IDs, duplicate unacknowledged hashes, missing dependencies, cycles, and unsorted repeated fields are rejected.

Known sensitive identifier tokens embedded in legacy schema/source names are replaced before evidence is written with deterministic, collision-resistant aliases derived from a one-way token hash plus contextual hash. The plaintext identifier is absent from the evidence, ledger, tools, tests, and documentation. Fresh production comparisons apply the same transform before hashing, so drift detection remains exact without storing the identifier.

SQL source projection has one implementation: `tools/source_record_projection.py`. It owns path validation, sensitive-token aliasing, blob hashing, source kind and lane classification, lexical declaration and unsafe-operation classification, rollback pairing, and transaction classification. The production evidence helper and offline validator both execute that exact module; neither contains a fallback or duplicate projector. Validation compares all 667 sanitized records field-by-field and requires zero differences plus exact canonical bytes and SHA-256.

Every catalog object fingerprint includes its exact sanitized metadata. Depending on object kind this includes owner, ACL/grants, relation kind, RLS enabled/forced, column type/nullability/identity/generated/default hash, key/constraint definition hash, index definition/predicate and readiness, sequence ownership/settings, view definition hash/population, function signature/owner/language/security/definition/config hash, trigger definition/function/enabled state, policy roles/command/using/check hashes, type labels/default, and extension version. Raw SQL expressions and bodies are never written to the evidence files.

Migration enumeration order is deterministic path order, not asserted historical apply order. Dependencies are empty because none can be proven from production evidence. Rollback pairing is recorded only when an exact paired source path exists. Recovery is classified as `paired_rollback`, `rollback_script`, `test_only`, or `none_declared`; it is not a promise that rollback is safe.

## Reconciliation result and discrepancies

The proposed live-object baseline matches all 11,967 observed catalog fingerprints because it is seeded from that exact read-only snapshot. This means the candidate accurately records the observed live schema; it does not establish source provenance.

Material unresolved discrepancies:

1. Production has no migration-history relation. Historical apply state, operator, order, and dependency graph are unverifiable.
2. The 667 SQL sources do not uniquely attribute the 11,967 live objects. Source declaration extraction is lexical evidence only.
3. Three duplicate source-hash groups exist and are explicitly classified; duplicate content is not silently collapsed.
4. One-time apply/rollback utilities and ordinary runtime entrypoints coexist in repository history. The ledger inventories them but does not bless or execute them.
5. Qdrant contains non-memory and Fractal Monism collections alongside memory-derived indexes. The ledger records the boundary but does not merge their authority lanes.

Future migrations must have a unique ID and SHA-256, declared dependency list, execution owner, transaction classification, apply evidence, intended object fingerprints, explicit recovery characteristic, and append-only reconciliation result. Altering an existing migration is drift; add a superseding migration instead.
