# Governed Memory clean-successor contract

## 1. Scope and non-authority

This is the design contract for a clean successor. It does not activate Memory and does not authorize production mutation, migration, deployment, deletion, provider calls, or service changes.

The successor starts empty. It does not import or backfill existing conversations, attachments, assistant preferences, claims, cards, vectors, extraction jobs, review artifacts, filesystem packets, compatibility queues, or historical Memory tables.

## 2. Authority boundaries

- Supabase Auth remains the account and authenticated-owner authority. Only active account UUIDs are accepted.
- The conversation system remains the transcript authority. Old conversations may remain available there but are never scanned or backfilled into Memory.
- Attachment storage remains conversation-scoped untrusted reference data. Existing test attachments are not migrated. New attachment content is not Memory input unless the authenticated user explicitly requests a bounded fact to be remembered.
- A new PostgreSQL database named `governed_memory` is the only Memory truth authority.
- Qdrant is a derived, owner-filtered candidate index. It never decides truth, admission, lifecycle state, or answer eligibility.
- Provider output is an untrusted proposal until deterministic validation and explicit reviewed admission succeed.
- Assistant preferences remain a separate product surface. Existing preference rows are not migrated; missing rows resolve to product defaults.
- Consent, privacy acknowledgement, account-security audit, export receipt, and deletion receipt records remain outside the Memory claim store. Raw prompts, attachments, review packets, and test Memory content are prohibited in retained audit receipts.

## 3. Clean data plane

```text
authenticated chat message
  -> atomic content-free conversation outbox record
  -> deterministic eligibility at the Memory boundary
      -> terminal content-free decision receipt, or
      -> exact eligible source reference
  -> one bounded extraction job
  -> one canonical provider request and completion receipt
  -> validated proposal pending review
  -> explicit reviewed admission
  -> immutable claim revision and current claim state
  -> transactional projection outbox
  -> Qdrant derived point
  -> owner-filtered candidate IDs
  -> PostgreSQL revalidation
  -> bounded prompt material and answer binding
  -> inspect / correct / retract / delete
```

No step writes raw conversation or attachment content to Qdrant. Ineligible messages do not create evidence text, provider jobs, proposals, or vectors.

The existing conversation database receives one new bridge table, `public.memory_ingest_outbox`. It is inserted atomically with new post-cutover user messages and contains only owner UUID, message UUID, thread UUID, content SHA-256, state, bounded attempts, and timestamps. It contains no transcript or attachment text. The Memory worker reads the exact source row by owner/message/hash and terminally completes or expires the bridge entry. This is the only successor object installed outside the new `governed_memory` database.

## 4. PostgreSQL target

Use a new database on the existing PostgreSQL cluster. Do not install the legacy `memory` schema or its historical migration ledger into this database.

Candidate tables:

| Table | Purpose | Content rule |
|---|---|---|
| `predicate_catalog` | Release-bound predicate/object contract | Ownerless, immutable checked-in registry hash |
| `evidence` | Exact eligible source reference | Owner, conversation/message reference, source hash, bounded review excerpt only when required |
| `extraction_job` | Bounded provider-independent job state | One owner/evidence/idempotency identity and explicit terminal state |
| `provider_call` | Canonical request/completion provenance | Model/schema/operation plus privacy, budget, request and response hashes; raw envelopes have bounded retention |
| `proposal` | Validated but untrusted fact candidate | Structured relation, review state, evidence and provider binding |
| `entity` | Owner-scoped relational subjects and objects | Self/person/pet/project identity without copied account authority |
| `claim` | Stable governed claim identity and lifecycle | Owner, current revision, active/retracted/deleted state |
| `claim_revision` | Immutable fact revision | Subject, predicate, object, qualifiers, validity, confidence, bounded retrieval text |
| `claim_evidence` | Immutable claim-to-evidence provenance | Claim revision and evidence identity |
| `projection_outbox` | Idempotent derived-index work | Upsert/delete operation, revision hash, state, attempts |
| `answer_binding` | Content-free answer-use receipt | Response, claim revision, policy and prompt binding hashes |
| `audit_event` | Content-free state transition receipt | Actor, operation, object, prior/new state hashes, timestamp |

All owner-bearing tables must have enabled and forced RLS. Application and worker roles must be `NOSUPERUSER NOBYPASSRLS`. Public table DML and public function execution are revoked. Owner-scoped APIs use fixed-search-path functions and verified Supabase UUID context. Cross-owner job claiming is allowed only through a bounded, audited `SECURITY DEFINER` function with no general table authority. `predicate_catalog` is the only ownerless operational table and is read-only outside migrations.

No permanent pending backlog is allowed. Rejected, expired, skipped, and failed items receive explicit terminal states. Raw provider envelopes and bounded review excerpts require short, explicit retention periods; immutable hashes and state receipts may remain.

The first release excludes project-memory lanes, pattern/salience scoring, consolidation, inferred facts, entailment services, automatic entity-resolution generations, preference retrieval, and filesystem review packets. A current claim may be `supported`, `uncertain`, or `disputed`; only explicitly projectable current states enter Qdrant.

Database roles:

```text
governed_memory_owner   NOLOGIN
governed_memory_api     NOSUPERUSER NOBYPASSRLS
governed_memory_worker  NOSUPERUSER NOBYPASSRLS
```

The API and worker receive no direct table DML. The minimal fixed-search-path function surface is: `current_owner_id`, `record_selected_evidence`, `lease_extraction_jobs`, `complete_extraction`, `review_proposal`, `read_claim_candidates`, `list_claims`, `correct_claim`, `retract_claim`, `request_claim_deletion`, `lease_projection_jobs`, `finish_projection_job`, and `record_answer_binding`.

## 5. State machines

Eligibility decisions are distinct and durable: `send_external`, `skip_zero_call`, `route_internal`, `block_local`, and `review_context`. They must not collapse into one generic skip.

Extraction job:

```text
eligible -> pending -> claimed -> completed
                              -> failed_terminal
                              -> retryable (bounded attempts only)
```

Proposal:

```text
pending_review -> admitted
               -> rejected
               -> expired
```

Claim:

```text
active -> correction_pending (immediately nonretrievable)
       -> corrected (reviewed new immutable revision)
       -> retracted
       -> deleted
```

Projection:

```text
pending -> claimed -> applied
                   -> retryable (bounded)
                   -> failed_terminal
```

Every transition is idempotent under an exact owner/object/operation key. A correction request immediately suppresses the disputed current revision and emits an idempotent vector deletion; its replacement remains a proposal until reviewed admission creates the next immutable revision. Provider calls occur only after eligibility and job reservation and are bounded to one call per exact extraction attempt.

## 6. Qdrant target

- Logical alias: `governed_memory_active`.
- Physical build collection: `governed_memory_build_<release_hash>`.
- Initial state: empty.
- Initial vector contract: `text-embedding-3-large`, 3072 dimensions, Float32 L2-normalized vectors, Dot distance; model, dimension, normalization, renderer, metric, and payload schema are release-bound and must pass a deterministic retrieval fixture before activation.
- Point identity: stable `claim_id`; correction overwrites the point with the current revision under the same claim identity.
- Required payload: `owner_user_id`, `claim_id`, `revision_id`, `revision_number`, `status`, `predicate`, `sensitivity`, `domains`, `intents`, `surface`, `requires_explicit`, `updated_at`, `embedding_model`, `dimensions`, `renderer_sha256`, `source_sha256`, `vector_sha256`, and `projection_manifest_sha256`.
- Required payload indexes: `owner_user_id`, `status`, `sensitivity`, `domains`, and `intents`.
- Required query behavior: mandatory authenticated-owner filter, bounded top-k, then PostgreSQL revalidation of current revision and active state.
- Retraction/deletion: durable PostgreSQL lifecycle transition first, then idempotent outbox deletion; stale Qdrant points can never survive PostgreSQL revalidation into a prompt.

Hard deletion is a durable operation rather than a cross-store transaction: mark the claim `deletion_pending`, make it immediately nonretrievable, apply and verify the Qdrant deletion, purge claim/revision/evidence content, then retain only the content-free deletion receipt. Every step is replayable and observable.

No legacy collection is aliased, copied, merged, or used to seed this collection.

## 7. Runtime and API target

The target runtime is `brains.service` plus one inactive-until-authorized `governed-memory-worker.service`. The worker handles eligibility, extraction, projection, deletion finalization, and bounded retries through the PostgreSQL state machines. There are no versioned timer families or filesystem review routers in the successor.

The successor code lives under one versionless package:

```text
rag_engine/governed_memory/
  auth.py
  contracts.py
  eligibility.py
  extraction.py
  repository.py
  admission.py
  projection.py
  retrieval.py
  lifecycle.py
  api.py
  worker.py
```

It must not import `memory_v1_*` modules. Retained invariants are ported with focused tests; old implementations do not become successor dependencies.

Target internal/API surfaces:

- conversation append remains separate from Memory;
- deterministic eligibility and exact eligible enqueue;
- list pending proposals for an authorized reviewer;
- admit or reject one exact proposal;
- list owner claims;
- correct, retract, and delete one exact owner claim;
- owner-filtered retrieval with PostgreSQL revalidation;
- content-free response inspection and answer binding;
- health containing only canonical database, queue, projection, and answer-use status.

Owner UUID is derived from authentication and is not accepted as an authoritative path or body field. Candidate owner-facing routes are:

```text
GET    /memory/status
GET    /memory/claims
GET    /memory/claims/{claim_id}
GET    /memory/proposals
POST   /memory/proposals/{proposal_id}/review
POST   /memory/claims/{claim_id}/correct
POST   /memory/claims/{claim_id}/retract
DELETE /memory/claims/{claim_id}
GET    /memory/operations/{operation_id}
```

There is no public evidence-ingestion or retrieval endpoint. Brains invokes those modules internally.

The new database removes the current single-database response transaction. Retrieval therefore records the content-free exposure binding in `governed_memory` before the response-model call. If binding persistence fails, Memory is omitted. The assistant message is then stored in the conversation database with the binding hash. A binding orphaned by later chat-persistence failure is harmless and expires under policy.

Normal chat must not fail because Memory selection is unavailable. Retrieval failure returns an ordinary answer without Memory and records a content-free failure receipt. Memory mutation, admission, and owner-verification failures fail closed.

## 8. Cutover and data policy

- Record an exact `ingest_after` cutover timestamp.
- Never scan messages older than that timestamp.
- Every existing active account begins with zero Memory claims and default assistant preferences.
- Preserve account UUID authority; do not create a copied account directory in Memory.
- Existing conversation and attachment data remains outside the successor until handled by a later privacy/deletion workstream.
- All legacy Memory state is quarantined from the successor, then deleted only after the successor passes the release gates and separate deletion authorization is granted.

## 9. Git and source policy

Active Python modules, services, routes, tests, and documentation use unversioned current names under one `memory/` package. Version identifiers remain only in immutable migration and release records.

Historical source is preserved once in an immutable external Git bundle/tag with hashes, then removed from the deployable branch. It must not remain in active `archive/`, `retired/`, test discovery, import paths, build inputs, runtime manifests, or operational documentation.
