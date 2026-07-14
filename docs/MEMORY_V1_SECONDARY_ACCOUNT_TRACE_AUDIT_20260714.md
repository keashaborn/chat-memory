# Memory V1 secondary-account ordinary trace audit — 2026-07-14

## Scope

- Authenticated non-admin account: `557ea042-cb82-48f8-9429-472e96c957ef`.
- Test content was explicitly synthetic and authorized for inspection.
- Eighteen user turns were captured across three thread IDs: thirteen setup and
  ordinary-use turns followed by five recall turns in a fresh thread.

## Cross-thread recall

The five recall answers correctly recovered the synthetic name, interests,
family, profession/work history, and writing direction. The final broad
"personal challenge" question selected the architecture-to-writing transition;
the recent alcohol statement was another plausible interpretation, so this is
a relevance ambiguity rather than an ownership error.

The five answers cited 15 raw-memory references covering eight unique Qdrant
points. Every point had:

- `payload.user_id = 557ea042-cb82-48f8-9429-472e96c957ef`
- `source = frontend/chat:user`
- `vantage_id = RESSE`

No point belonged to the primary owner. The active Qdrant retriever applies an
exact `payload.user_id` filter derived from the authenticated route owner.
Vantage is not used as an ownership filter. The only active caller is the
authenticated `/vantage/query` route after exact actor/body UUID enforcement.

## Memory V1 and RLS results

- All ordinary specialized traces selected zero records, injected no Memory V1
  prompt block, and exposed no Memory V1 content to the answer model.
- The account owns no reviewed Memory V1 claims, preferences, or project
  records, so zero selection is correct.
- Under forced RLS, the LifeSwitch actor saw all five of its recall traces and
  zero primary-owner traces.
- Under forced RLS, the primary actor saw zero LifeSwitch recall traces.
- No known primary-account identity marker appeared in the LifeSwitch answers.

## Assessment

Ordinary cross-thread account isolation passes. This completes the required
second-account ordinary-trace gate. It validates authenticated capture,
owner-filtered raw recall, and reciprocal trace isolation.

It does not validate automatic Memory V1 consolidation: the successful recall
came from the legacy `memory_raw` path. Raw Qdrant payloads still use the legacy
`user_id` ownership field and do not yet carry `owner_user_id`. The enterprise
follow-up is to add an immutable canonical owner field plus a fail-closed
post-search owner assertion while Memory V1 ingestion/consolidation is brought
online.
