# Verbal Sage Memory V1 — Schema and Interface Design

Status: proposed design for review

Date: 2026-07-12

This document defines a feasible first production version of account-owned governed memory. It does not attempt to solve general scientific epistemology or replace structured LifeSwitch nutrition and training stores.

## 1. V1 objective

Create a secure memory path that:

1. Derives ownership from the authenticated Supabase user.
2. Preserves immutable evidence.
3. Represents remembered information as revisable claims.
4. Keeps confidence, importance, salience, sensitivity, and temporal validity separate.
5. Retrieves a small governed claim set instead of raw conversational dumps.
6. Records why each claim was selected or rejected.
7. Does not depend on Vantage/persona ownership.

## 2. V1 non-goals

- General-purpose adjudication of public scientific or historical controversies.
- Automatic production writes from unrestricted LLM output.
- Replacing deterministic nutrition, exercise, measurement, or plan tables.
- Destructive cleanup of `vantage_card`, `vantage_fact`, or Qdrant `memory_raw` before cutover.
- Per-persona factual memory.
- A complete autonomous curation ecosystem in the first milestone.

## 3. Store responsibilities

### Postgres `memory` schema

Canonical store for:

- user-owned entities;
- immutable evidence;
- claims and current retrieval state;
- evidence-for/evidence-against links;
- claim relationships such as contradiction and supersession;
- append-only assessments;
- explicit response preferences;
- retrieval traces.

### Qdrant

Derived associative index only. Qdrant points reference Postgres IDs and contain no independently authoritative memory state.

Proposed collections:

- `memory_claim_v1`: one active projection per claim;
- `memory_evidence_v1`: optional evidence projection used only for episodic/evidence fallback.

Every point includes server-generated `owner_user_id`. All searches use a mandatory owner filter.

### Existing stores

- `public.chat_log`: transcript/evidence origin during migration; runtime DDL must be removed separately.
- `memory_raw`: legacy evidence/index retained read-only during shadow comparison.
- `vantage_card`: legacy cards and four reviewed seed memories.
- `vantage_fact`: legacy extraction output; sources may be migration evidence, but claims are not canonical.
- LifeSwitch schemas: authoritative structured health/activity records.

## 4. Core epistemic rules

- The central object is a claim, not an eternal fact.
- Evidence is append-only except for explicit privacy redaction/deletion.
- Repetition can add evidence or salience; it does not automatically increase truth confidence.
- Fading changes salience, not confidence or evidence history.
- New supporting/opposing evidence creates a new assessment.
- Correction supersedes or disputes a claim without erasing history.
- Popularity, consensus, and authority are evidence metadata, not truth fields.
- V1 evaluates personal-memory evidence types; it does not compute general public-knowledge truth.

## 5. Proposed schema

Schema name: `memory`. The database inventory confirms it does not currently exist.

Postgres 16.10 and `pgcrypto` are available. UUID primary keys use `gen_random_uuid()`.

### 5.1 Types

```sql
CREATE TYPE memory.evidence_kind AS ENUM (
  'user_statement',
  'system_event',
  'structured_measurement',
  'derived_result',
  'document',
  'external_observation'
);

CREATE TYPE memory.record_status AS ENUM (
  'active',
  'quarantined',
  'redacted',
  'deleted'
);

CREATE TYPE memory.claim_status AS ENUM (
  'candidate',
  'supported',
  'uncertain',
  'disputed',
  'superseded',
  'retracted',
  'quarantined'
);

CREATE TYPE memory.evidence_stance AS ENUM (
  'supports',
  'opposes',
  'qualifies',
  'context'
);

CREATE TYPE memory.claim_relation_type AS ENUM (
  'supersedes',
  'contradicts',
  'qualifies',
  'depends_on',
  'derived_from'
);

CREATE TYPE memory.sensitivity_level AS ENUM (
  'low',
  'medium',
  'high',
  'restricted'
);
```

### 5.2 Entity and aliases

Entities are owner-local. Two users may have different entities with the same name. One user may also have two people with the same name.

```sql
CREATE TABLE memory.entity (
  entity_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  entity_key text NOT NULL,
  entity_type text NOT NULL,
  canonical_name text NOT NULL,
  normalized_name text NOT NULL,
  status memory.record_status NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, entity_key),
  UNIQUE (owner_user_id, entity_id)
);

CREATE INDEX entity_owner_name_idx
  ON memory.entity(owner_user_id, entity_type, normalized_name)
  WHERE status='active';

CREATE TABLE memory.entity_alias (
  owner_user_id uuid NOT NULL,
  entity_id uuid NOT NULL,
  alias text NOT NULL,
  normalized_alias text NOT NULL,
  alias_type text NOT NULL DEFAULT 'observed',
  evidence_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, entity_id, normalized_alias),
  FOREIGN KEY (owner_user_id, entity_id)
    REFERENCES memory.entity(owner_user_id, entity_id)
    ON DELETE CASCADE
);

CREATE INDEX entity_alias_lookup_idx
  ON memory.entity_alias(owner_user_id, normalized_alias);
```

The `entity_alias.evidence_id` foreign key is added after `memory.evidence` exists.

### 5.3 Evidence

Evidence identifies what was recorded, not whether the resulting interpretation is true.

```sql
CREATE TABLE memory.evidence (
  evidence_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  kind memory.evidence_kind NOT NULL,
  source_system text NOT NULL,
  external_id text NOT NULL,
  content text,
  content_sha256 text,
  observed_at timestamptz,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  directness numeric(4,3),
  source_reliability numeric(4,3),
  independence_key text,
  sensitivity memory.sensitivity_level NOT NULL DEFAULT 'medium',
  status memory.record_status NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (owner_user_id, source_system, external_id),
  UNIQUE (owner_user_id, evidence_id),
  CHECK (directness IS NULL OR directness BETWEEN 0 AND 1),
  CHECK (source_reliability IS NULL OR source_reliability BETWEEN 0 AND 1)
);

CREATE INDEX evidence_owner_time_idx
  ON memory.evidence(owner_user_id, observed_at DESC, recorded_at DESC)
  WHERE status='active';
```

V1 evidence kinds have defined semantics:

- `user_statement`: the user stated something; authoritative for the occurrence of the statement, not automatically for external truth.
- `system_event`: the application directly recorded an operation.
- `structured_measurement`: a value and method from a LifeSwitch/measurement record.
- `derived_result`: a reproducible calculation with input references.
- `document`: imported text or artifact.
- `external_observation`: later extension for external evidence.

### 5.4 Claims

`claim` contains the current retrieval projection. Assessment history remains append-only.

```sql
CREATE TABLE memory.claim (
  claim_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  subject_entity_id uuid NOT NULL,
  predicate text NOT NULL,
  object_entity_id uuid,
  object_literal jsonb,
  qualifiers jsonb NOT NULL DEFAULT '{}'::jsonb,
  canonical_text text NOT NULL,
  canonical_key text NOT NULL,
  status memory.claim_status NOT NULL DEFAULT 'candidate',
  confidence numeric(4,3) NOT NULL DEFAULT 0.500,
  importance numeric(4,3) NOT NULL DEFAULT 0.500,
  salience numeric(4,3) NOT NULL DEFAULT 0.500,
  sensitivity memory.sensitivity_level NOT NULL DEFAULT 'medium',
  valid_from timestamptz,
  valid_to timestamptz,
  last_confirmed_at timestamptz,
  retrieval_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, canonical_key),
  UNIQUE (owner_user_id, claim_id),
  FOREIGN KEY (owner_user_id, subject_entity_id)
    REFERENCES memory.entity(owner_user_id, entity_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_user_id, object_entity_id)
    REFERENCES memory.entity(owner_user_id, entity_id)
    ON DELETE RESTRICT,
  CHECK ((object_entity_id IS NULL) <> (object_literal IS NULL)),
  CHECK (confidence BETWEEN 0 AND 1),
  CHECK (importance BETWEEN 0 AND 1),
  CHECK (salience BETWEEN 0 AND 1),
  CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);

CREATE INDEX claim_owner_active_idx
  ON memory.claim(owner_user_id, status, updated_at DESC);

CREATE INDEX claim_owner_subject_predicate_idx
  ON memory.claim(owner_user_id, subject_entity_id, predicate);

CREATE INDEX claim_owner_retrieval_idx
  ON memory.claim(owner_user_id, salience DESC, importance DESC, confidence DESC)
  WHERE status IN ('supported','uncertain','disputed');
```

The application computes `canonical_key` from normalized subject, predicate, object, qualifiers, temporal bounds, and owner. It must not include wording-only fields.

### 5.5 Evidence links

```sql
CREATE TABLE memory.claim_evidence (
  owner_user_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  stance memory.evidence_stance NOT NULL,
  relevance numeric(4,3) NOT NULL DEFAULT 1.000,
  rationale text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, claim_id, evidence_id, stance),
  FOREIGN KEY (owner_user_id, claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id)
    ON DELETE CASCADE,
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE RESTRICT,
  CHECK (relevance BETWEEN 0 AND 1)
);

CREATE INDEX claim_evidence_evidence_idx
  ON memory.claim_evidence(owner_user_id, evidence_id);
```

### 5.6 Claim relationships

```sql
CREATE TABLE memory.claim_relation (
  owner_user_id uuid NOT NULL,
  from_claim_id uuid NOT NULL,
  to_claim_id uuid NOT NULL,
  relation_type memory.claim_relation_type NOT NULL,
  rationale text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, from_claim_id, to_claim_id, relation_type),
  FOREIGN KEY (owner_user_id, from_claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id)
    ON DELETE CASCADE,
  FOREIGN KEY (owner_user_id, to_claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id)
    ON DELETE CASCADE,
  CHECK (from_claim_id <> to_claim_id)
);
```

Corrections create a new claim, add a `supersedes` relation, and transition the older claim to `superseded`. Opposing claims may remain active as `disputed` and use `contradicts`.

### 5.7 Append-only assessments

```sql
CREATE TABLE memory.claim_assessment (
  assessment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  status memory.claim_status NOT NULL,
  support_score numeric(4,3),
  opposition_score numeric(4,3),
  confidence numeric(4,3) NOT NULL,
  method text NOT NULL,
  method_version text NOT NULL,
  rationale text,
  inputs jsonb NOT NULL DEFAULT '{}'::jsonb,
  supersedes_assessment_id uuid,
  assessed_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (owner_user_id, claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id)
    ON DELETE CASCADE,
  FOREIGN KEY (supersedes_assessment_id)
    REFERENCES memory.claim_assessment(assessment_id)
    ON DELETE RESTRICT,
  CHECK (support_score IS NULL OR support_score BETWEEN 0 AND 1),
  CHECK (opposition_score IS NULL OR opposition_score BETWEEN 0 AND 1),
  CHECK (confidence BETWEEN 0 AND 1)
);

CREATE INDEX claim_assessment_history_idx
  ON memory.claim_assessment(owner_user_id, claim_id, assessed_at DESC);
```

### 5.8 User response preferences

Preferences are separate from factual claims and cannot alter factual confidence.

```sql
CREATE TABLE memory.user_preference (
  preference_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  preference_key text NOT NULL,
  value jsonb NOT NULL,
  status memory.record_status NOT NULL DEFAULT 'active',
  explicit boolean NOT NULL DEFAULT false,
  confidence numeric(4,3) NOT NULL DEFAULT 0.500,
  evidence_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, preference_key),
  FOREIGN KEY (owner_user_id, evidence_id)
    REFERENCES memory.evidence(owner_user_id, evidence_id)
    ON DELETE SET NULL,
  CHECK (confidence BETWEEN 0 AND 1)
);
```

Initial allowlisted keys should remain small: response length, formatting preference, tone, and voice. Backend policy controls do not belong here.

### 5.9 Retrieval traces

```sql
CREATE TABLE memory.retrieval_trace (
  trace_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL,
  request_id text,
  answer_id uuid,
  thread_id uuid,
  query_text text NOT NULL,
  intent text NOT NULL,
  domain text NOT NULL,
  token_budget integer NOT NULL,
  selected_count integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memory.retrieval_trace_item (
  owner_user_id uuid NOT NULL,
  trace_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  selected boolean NOT NULL,
  rank integer,
  semantic_score numeric,
  policy_score numeric,
  final_score numeric,
  reason_codes text[] NOT NULL DEFAULT '{}',
  prompt_tokens integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, trace_id, claim_id),
  FOREIGN KEY (trace_id)
    REFERENCES memory.retrieval_trace(trace_id)
    ON DELETE CASCADE,
  FOREIGN KEY (owner_user_id, claim_id)
    REFERENCES memory.claim(owner_user_id, claim_id)
    ON DELETE RESTRICT
);
```

Trace retention may be shorter than claim/evidence retention. Query text may later be replaced by a hash plus redacted debug copy depending on privacy policy.

## 6. Database security

All owner-scoped tables use row-level security. The runtime database role must not own the schema and must not have `BYPASSRLS`.

Each authenticated operation starts a transaction and sets:

```sql
SET LOCAL app.user_id = '<verified-supabase-uuid>';
```

Representative policy:

```sql
ALTER TABLE memory.claim ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.claim FORCE ROW LEVEL SECURITY;

CREATE POLICY claim_owner_isolation ON memory.claim
USING (owner_user_id = current_setting('app.user_id', true)::uuid)
WITH CHECK (owner_user_id = current_setting('app.user_id', true)::uuid);
```

Equivalent policies apply to every owner-scoped table. Background jobs run one owner at a time and set the same transaction-local identity. Migration and privacy-deletion roles are separate, audited roles.

## 7. Brains identity boundary

V1 endpoints do not accept an authoritative `user_id` in JSON.

Current frontend behavior is retained:

1. Verbal Sage validates the Supabase session.
2. Verbal Sage supplies the infrastructure service token and actor UUID.
3. Brains requires the actor UUID for every user-memory read/write.
4. Brains validates UUID format and uses it as the only owner identity.
5. Brains sets the Postgres RLS identity and mandatory Qdrant owner filter.

If a legacy request body contains `user_id`, Brains rejects a mismatch. After compatibility removal, the field is ignored or forbidden.

Longer-term hardening may replace the plain actor header with a signed internal assertion or direct Supabase JWT verification. V1 still gains defense in depth from service authentication, actor enforcement, RLS, and Qdrant owner filters.

## 8. Service interfaces

All routes are internal Brains routes protected by service and actor authentication.

### 8.1 Record evidence

`POST /memory/v1/evidence`

```json
{
  "kind": "user_statement",
  "source_system": "public.chat_log",
  "external_id": "chat_log:<uuid>",
  "content": "...",
  "observed_at": "2026-07-12T18:00:00Z",
  "sensitivity": "medium",
  "metadata": {
    "thread_id": "<uuid>",
    "request_id": "<id>"
  }
}
```

The actor UUID supplies ownership. Idempotency is `(owner_user_id, source_system, external_id)`.

### 8.2 Propose candidates

`POST /memory/v1/evidence/{evidence_id}/propose`

Returns candidate entities, claims, evidence stances, and comparison results. V1 performs no durable claim write unless the deterministic validator and configured review policy permit it.

### 8.3 Review/apply one candidate

`POST /memory/v1/candidates/{candidate_id}/apply`

Requires an internal governance capability and exact candidate/version confirmation. It transactionally writes entities, claim, evidence links, initial assessment, relations, and an outbox event for Qdrant projection.

### 8.4 Retrieve memory packet

`POST /memory/v1/retrieve`

```json
{
  "query": "Have I had any deaths in my family recently?",
  "intent": "personal_recall",
  "domain": "personal",
  "max_claims": 4,
  "max_tokens": 500,
  "allow_evidence_fallback": false
}
```

Representative response:

```json
{
  "version": "memory_packet_v1",
  "trace_id": "<uuid>",
  "claims": [
    {
      "claim_id": "<uuid>",
      "text": "The user's mother DeeDee died.",
      "status": "supported",
      "confidence": 0.82,
      "importance": 0.90,
      "valid_from": null,
      "valid_to": null,
      "use_instruction": "answer_directly_if_relevant",
      "evidence_refs": ["<uuid>"]
    }
  ],
  "rejected_counts": {
    "domain": 2,
    "policy": 1,
    "budget": 0
  },
  "token_estimate": 54
}
```

No rejected claim text enters the model prompt.

### 8.5 Inspect

`GET /memory/v1/inspect`

Read-only, capability-gated view of entities, claims, evidence references, assessments, relations, policies, and retrieval traces. Content visibility respects sensitivity.

## 9. Retrieval pipeline

V1 retrieval order:

1. Verify actor and establish RLS identity.
2. Classify intent/domain using deterministic rules initially.
3. Route structured health questions to the relevant LifeSwitch service first.
4. Embed the query and search `memory_claim_v1` with mandatory owner/status filters.
5. Load candidate claims from Postgres by owner and ID.
6. Apply sensitivity, temporal validity, status, domain, and surface policy.
7. Rerank using separate components:
   - semantic relevance;
   - entity/predicate match;
   - importance;
   - salience;
   - confidence;
   - temporal relevance.
8. Enforce claim and token budgets.
9. Optionally load minimal evidence for explicit recall, disputed claims, or verification.
10. Write the retrieval trace.
11. Render a structured memory packet for the meaning planner/language model.

Tone, formatting, gravity, VB desire, and Vantage do not affect claim relevance or confidence.

## 10. Candidate and correction behavior

V1 starts conservatively:

- deterministic extractors and current reviewed personal-event examples;
- LLM extraction may propose structured candidates but cannot write directly;
- exact owner boundary on every comparison;
- repeated evidence attaches to an existing claim instead of duplicating it;
- corrections produce new claims and supersession relations;
- multi-subject evidence must split before promotion;
- sensitive/high-impact claims require review until reliability is demonstrated.

## 11. Background jobs

### First production milestone

- `EvidenceIngestJob`
- `CandidateProposalJob`
- `CandidateComparisonJob`
- `ClaimProjectionJob`
- `RetrievalTraceAuditJob`

### Later

- `EntityResolutionJob`
- `ContradictionDetectionJob`
- `AssessmentRefreshJob`
- `SalienceDecayJob`
- `TemporalExpiryJob`
- `ReconfirmationJob`
- `OutcomeUseAuditJob`
- `LifeSwitchContextAdapterJob`
- ABA baseline/intervention analysis jobs

Every job is idempotent, owner-scoped, retryable, and auditable. Failed jobs retain durable state and do not partially promote memory.

## 12. Migration and cutover

1. Snapshot current counts and code revisions.
2. Create the new schema and runtime/migration roles.
3. Add cross-user RLS tests before inserting production memory.
4. Import the four reviewed durable cards and their source references.
5. Project them to `memory_claim_v1`.
6. Run shadow retrieval without changing prompts.
7. Compare old/new selection, prompt size, and rejection reasons.
8. Import only explicitly reviewed identity/background/preferences.
9. Switch normal prompt memory to `memory_packet_v1`.
10. Disable raw-chat and legacy Vantage-card prompt injection.
11. Quarantine legacy writers and background jobs.
12. Retain old stores read-only through a rollback window.

`vantage_fact.source` rows may be re-imported as evidence only when:

- `metadata.user_id` is a valid UUID;
- the referenced `public.chat_log` row has the same canonical owner;
- the source has not been deleted;
- the import is idempotent.

Existing `vantage_fact.claim` rows are not migrated wholesale because they are document-centered, globally keyed, and not owner-constrained.

## 13. Acceptance tests

### Security

- Actor A cannot read, write, link, inspect, trace, export, or delete Actor B's records.
- A mismatched legacy body `user_id` is rejected.
- Missing actor identity is rejected.
- Qdrant calls cannot execute without an owner filter.
- Background jobs cannot process multiple owners under one RLS identity.

### Memory correctness

- DeeDee recall selects the DeeDee claim only.
- Neko correction supersedes Nemo for normalization.
- A technical deployment question selects no personal claims.
- Changing Vantage does not change factual availability.
- Repeated evidence links to one claim.
- Opposing evidence changes assessment without deleting either side.
- Salience decay suppresses spontaneous use but explicit recall can still retrieve the claim.
- Structured nutrition totals come from LifeSwitch, not conversational memory.

### Prompt control

- Normal answers contain governed claims, not raw chat.
- Rejected claims never enter the prompt.
- Every selected claim has reason codes and a trace.
- Memory packets remain under the configured claim/token budget.

## 14. Automation and Codex workflow

Work is split into bounded phases with acceptance tests. Codex can use the configured `ssh seebx` and `ssh verbalsage` access for routine inspection, patching, tests, service checks, and iteration.

Codex proceeds autonomously within an approved phase for:

- read-only inspection;
- creating migration/code/test files;
- running static checks and tests;
- restarting development/test services when already authorized;
- iterating until the phase's acceptance tests pass.

Codex stops for explicit approval before:

- applying a production schema migration;
- modifying or deleting production memory data;
- disabling a live legacy path;
- rotating credentials or changing firewall/network policy;
- pushing commits or opening a pull request unless already requested;
- making a product decision outside this design.

Each phase ends with:

1. exact files changed;
2. tests and outputs;
3. unresolved risks;
4. rollback method;
5. the next bounded phase.

## 15. Open design decisions before implementation

1. Confirm schema name `memory`.
2. Confirm that V1 uses backend-enforced RLS with a non-owner runtime role.
3. Confirm initial auto-promotion policy: reviewed/manual only versus a narrow deterministic allowlist.
4. Confirm retrieval-trace retention and whether raw query text may be stored.
5. Confirm whether explicit user preferences can be edited in the user interface or remain backend-managed initially.
