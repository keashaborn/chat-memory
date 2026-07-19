# RESSE User Preference Contract v0.1

Status: proposed; isolated implementation only
Date: 2026-07-19
Runtime integration authorized: no

## 1. Purpose

Define how explicit user preferences and user-entered profile context may
influence RESSE without changing assistant identity, runtime policy, safety,
retrieval authority, Memory V1 ownership, or Fractal Monism routing.

The governing rule is:

> User preferences are always available to the authorized selector, but they
> are not automatically injected into every model request.

## 2. Authority boundaries

Backend-owned and never user-configurable:

- assistant identity (`RESSE`);
- response-mode classification and precedence;
- high-stakes behavior;
- FM lens and corpus eligibility;
- source authority tiers and retrieval budgets;
- memory intent, ownership, evidence, extraction, and promotion;
- authentication, tools, permissions, and service configuration.

User-owned within this bounded contract:

- response length: `concise`, `balanced`, or `detailed`;
- technical depth: `plain`, `balanced`, or `expert`;
- preferred format: `auto`, `prose`, `bullets`, or `steps`;
- encouragement: `minimal` or `neutral`;
- optional free-form response-style instructions;
- nickname, occupation, and “More about you” profile context.

## 3. Separate data classes

### 3.1 Response preferences

Response preferences affect presentation only. Structured preferences are
preferred over free-form instructions because they are deterministic,
auditable, and do not consume prompt tokens as raw prose.

Free-form instructions:

- are optional;
- are normalized and length-bounded;
- are treated as untrusted user preference text;
- are selected only when the trusted application context marks them relevant;
- are suppressed in `HIGH_STAKES` mode;
- are suppressed when they contain recognizable control or override language;
- never become system policy.

### 3.2 User profile context

Nickname, occupation, and “More about you” are user-provided data. They are
not instructions and are not automatically verified facts.

Each field is selected independently by trusted relevance signals. An
occupation need not be sent for a weather question; “More about you” need not
be sent for a server restart; a nickname need not be used in every answer.

Selected profile text must be passed to a future prompt adapter as labeled
user-provided data, never concatenated into the developer-policy block.

### 3.3 Governed memory

This contract does not retrieve, filter, write, promote, or own memory.
`response_mode` and the preference selector remain independent of Memory V1's
intent router. Relevant governed memory may still be selected by Memory V1.

## 4. Selection inputs

The selector consumes trusted application context:

- `response_mode`;
- whether nickname is relevant;
- whether occupation is relevant;
- whether “More about you” is relevant;
- whether custom response instructions are relevant.

These relevance flags must not be constructed directly from arbitrary client
JSON in production. The isolated module accepts typed values so the eventual
adapter boundary is explicit and testable.

## 5. Mode behavior

### HIGH_STAKES

- suppress free-form custom instructions;
- suppress user profile context;
- force concise, plain, clarity-first presentation defaults;
- preserve provider and application safety requirements;
- do not suppress independently authorized governed facts or structured data.

### TECHNICAL

- allow structured length, depth, and format preferences;
- allow custom response instructions only when marked relevant and safe;
- select profile fields only when independently relevant;
- keep FM material disabled under the runtime policy.

### FM_EXPLICIT, COACHING, and ORDINARY

- allow structured preferences;
- select custom instructions and profile fields only when relevant;
- do not let preference text change response mode, FM routing, or memory intent.

## 6. Input shape

Only these top-level objects are accepted:

```json
{
  "preferences": {
    "response_length": "balanced",
    "technical_depth": "balanced",
    "format": "auto",
    "encouragement": "neutral",
    "custom_instructions": "Prefer direct, plain language."
  },
  "profile": {
    "nickname": "Eric",
    "occupation": "Psychologist, BCBA",
    "more_about_you": "I build practical systems and prefer direct analysis."
  }
}
```

Unknown and legacy fields are ignored and reported. A client-provided
`assistant_profile_id`, `vantage_id`, `mix`, `limits`, `routing`, `roleplay`,
`pragmatics`, or memory-owner value has no effect.

## 7. Text limits

- nickname: 64 characters;
- occupation: 160 characters;
- “More about you”: 2,000 characters;
- custom response instructions: 1,200 characters.

Text is Unicode-normalized, control characters are removed, line endings are
normalized, and truncation is recorded in validation notes.

## 8. Output contract

The selector returns structured JSON containing:

- fixed assistant identity;
- fixed independent memory-intent owner;
- selected structured preferences;
- selected user-profile fields;
- optional untrusted custom response text;
- suppressed fields with reason codes;
- ignored input fields and validation notes;
- invariants declaring that user text cannot override policy.

It does not build a prompt, call a model, access a database, retrieve memory,
query Qdrant, or mutate application state.

## 9. Integration boundary

Future integration requires joint review of:

- the final Memory V1 profile and retrieval interfaces;
- prompt block ordering and authority labels;
- server-side ownership and authorization;
- observability that does not log private profile text;
- legacy user-instructions migration;
- frontend save/load behavior and correction/deletion flows.

The isolated module and evaluation fixture may be merged only after those
interfaces are stable. This contract does not authorize production wiring.

## 10. Acceptance criteria

- No user input changes the `RESSE` identity.
- No user input changes memory ownership or intent routing.
- Raw profile and instruction text is not always selected.
- High-stakes mode suppresses all free-form preference and profile text.
- Recognizable policy-override attempts are suppressed.
- Unknown and legacy fields have no effect and remain observable.
- Selection is deterministic for identical typed inputs.
- The module has no database, HTTP, provider, Qdrant, or shared runtime
  dependency.
