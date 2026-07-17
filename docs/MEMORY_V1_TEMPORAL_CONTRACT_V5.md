# Memory V1 temporal interval and precision contract V5

Status: proposed, specification-only, not runtime-active

Server: seebx backend

Model packet schema: `specs/memory_v1_relational_extraction_v5.schema.json`

Canonical sorted-JSON model schema SHA-256:
`6292b44788bbdf652000b79668d18fd065747f9607380b7bfaf21f0a9b2cb9f7`

Evaluation binding:
`evals/memory_v1_temporal_entity_resolution_v5_cases.jsonl`

Canonical sorted-JSON evaluation SHA-256:
`332783f8d293b607ae7b2a6006da40bf4f04778c94e6bc46fa24693099a70a4b`

This contract does not authorize a migration, production extraction, durable
observation, candidate creation, promotion, projection, retrieval, prompt
influence, or allowlist change.

## Decision

Temporal meaning is not reduced to `valid_from` and `valid_to`. V5 preserves
five independent properties:

1. semantic: occurrence, state validity, planned time, observation time, or
   none;
2. shape: none, instant, bounded interval, open interval, or recurring;
3. basis: instant, calendar, relative, recurring, or none;
4. precision: exact, minute, day, month, year, or unknown;
5. certainty: exact, approximate, bounded, or unknown.

`relative` is a source form and basis, not a precision. “About two years ago”
must not become an exact timestamp or an invented day.

## Normalized value

Every observation carries one temporal object, including an explicit no-time
object. The object contains:

- `semantic`;
- `shape`;
- `basis`;
- `source_form`;
- `certainty`;
- `precision`;
- exactly one compatible value form: `instant`, `calendar_range`,
  `instant_range`, `relative_offset`, or `recurrence`;
- `anchored_to_source_time`;
- `normalization_policy_version`;
- reason codes.

The server supplies the trusted evidence timestamp and normalization policy.
The model cannot supply an anchor timestamp, timezone, policy version, or
database range literal.

### Instant values

Use an RFC3339 instant only when the source supports a timezone-aware point in
time. Minute precision remains minute precision; seconds must not be invented.

Use a half-open `tstzrange` (`[)`) for a bounded or open instant window. Do not
represent a single instant as a zero-width range.

### Calendar values

Use a half-open `daterange` (`[)`) for day, month, or year precision. Calendar
facts do not acquire a timezone merely to fit a timestamp column.

Examples:

| Supported meaning | Normalized range | Precision |
|---|---|---|
| July 1, 2026 | `[2026-07-01, 2026-07-02)` | day |
| March 2026 | `[2026-03-01, 2026-04-01)` | month |
| 2025 | `[2025-01-01, 2026-01-01)` | year |
| since July 1, 2026 | `[2026-07-01, )` | day |

### Partial calendar anchoring

A missing year can be filled only by the deterministic
`memory_temporal_normalization_v5` policy and must set
`anchored_to_source_time=true`.

- For occurrence or state language, a month-only reference resolves to the
  most recent non-future occurrence of that month relative to the evidence
  timestamp.
- For explicitly planned language, it resolves to the next non-past occurrence.
- Explicit discourse context overrides this rule only when the source span
  supports the year.
- If the rule yields more than one plausible year, normalization defers with
  `calendar_anchor_ambiguous`.

Thus a March occurrence in a source recorded in July 2026 becomes March 2026
with month precision, not March 1 as the claimed event date.

### Relative values

A numeric relative expression is stored structurally:

```json
{
  "direction": "past",
  "magnitude": 2,
  "unit": "year",
  "approximate": true,
  "anchor_source": "evidence_observed_at"
}
```

The server attaches the evidence timestamp. No concrete range is derived from
an approximate offset until a separately reviewed tolerance policy exists.
Vague expressions such as “lately,” “a while ago,” or “recently” do not receive
invented bounds. They remain unknown or defer as
`relative_span_underspecified`.

### Recurrence

The first V5 contract supports only `unspecified_repeated`. It records that a
state or activity recurs without inventing a schedule. Exact schedules and
RRULE generation require a later registry version.

### Observation time is not validity time

Evidence recorded at a time proves only when the statement was observed. It
does not prove that a state began then. “I am blocked” must not create a durable
open state beginning at message time. Transient states remain deferred unless
the storage policy explicitly permits them.

`as_of_source_observation` uses `semantic=observation_time` and
`source_form=implicit_source_time`; the server inserts the trusted evidence
timestamp. It cannot be used as an event occurrence date.

### Planned time is not occurrence

Planned or proposed actions use `semantic=planned_time`. They cannot create a
completed occurrence or current-state claim without later evidence.

## Persistence target

The dedicated observation design is amended to use a one-to-one
`memory.observation_temporal` relation rather than one generic timestamp range.
The logical columns are:

- owner and observation composite key;
- semantic, shape, basis, source form, certainty, and precision;
- nullable `instant_at timestamptz`;
- nullable `calendar_range daterange`;
- nullable `instant_range tstzrange`;
- validated relative-offset JSON;
- validated recurrence JSON;
- source-anchor flag and normalization policy version;
- deterministic normalized-value hash.

Database checks enforce the exact value-form matrix and canonical `[)` bounds.
All owner foreign keys are composite. The row is immutable with the observation
except for the same audited redaction/deletion procedure.

Current `memory.claim.valid_from` and `valid_to` remain compatibility fields.
Only verified instant-based values may populate them. Calendar, relative, and
recurring values must not be flattened into those columns. A later claim
temporal materialization must preserve the observation values and claim
revision that produced it.

## Hash and dedup semantics

The normalized temporal object participates in the observation hash. Equivalent
calendar ranges with different source phrases deduplicate semantically only
after preserving both evidence records. Approximate relative expressions do not
deduplicate against exact calendar ranges merely because a guessed date would
be nearby.

Temporal precision does not alter truth confidence. Recency and fading remain
separate retrieval signals.

## Twenty-five-case gate

The same source manifest must prove:

- the family event retains month precision and a calendar interval;
- the continuing residence/state remains open-ended;
- current project statements use source observation time only;
- current pet attributes use open state validity where supported;
- planned pet-related material is not normalized as completed;
- question-only and transient-state cases create no temporal observations;
- no model-authored anchor, timezone, owner, durable ID, or range literal is
  accepted.
