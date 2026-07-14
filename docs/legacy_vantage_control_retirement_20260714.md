# Legacy Vantage Control Retirement — 2026-07-14

## Scope

This batch retired legacy Vantage control paths after the Vantage initiator shutdown. It removed obsolete generators, self-calls, mirrors, fallback stores, HTTP editors, and dead admin inventory without changing Memory V1 data, enabling Memory V1 prompt injection, or removing response controls that still have live consumers.

Production commits:

- seebx backend: `e5fc54e cleanup: retire legacy Vantage controls`
- Verbal Sage frontend: `adbf130 cleanup: retire legacy profile mirrors`
- Verbal Sage frontend: `c70bd70 cleanup: remove dead Vantage control UI`

## Retired backend paths

The following Brains routes are no longer registered:

- `POST /gravity/rebuild`
- `GET /temporal/{user_id}`
- `POST /vb_desire/rebuild`
- `POST /profiles/upsert`
- `GET /profiles/{user_id}/default`
- `POST /vantages/sync`
- `GET /vantages/{user_id}`
- `GET/POST /vantage/rag_policy`

The following executable modules were removed from the active source tree:

- `rag_engine/gravity.py`
- `rag_engine/vb_desire_profile.py`
- `rag_engine/temporal_policy.py`

Gravity loading, misalignment metadata, and gravity score bonuses were removed from personal retrieval. Temporal self-fetching and the disabled re-entry prefix path were removed. VB-desire profile imports and generation were removed. The Vantage registry actor-auth test was removed with the deleted registry API.

The active card API/export kind list no longer selects `gravity_profile` or `vb_desire_profile`. Historical Qdrant points remain preserved, and the personal retrieval filter continues to exclude `gravity_daemon` and `vb_desire_daemon` sources.

The `public.vs_profiles` and `vantage_profile.registry` tables were not dropped or modified. Their four and two rows, respectively, remain preserved under the validated shutdown archive.

## Retired frontend paths

- Deleted `/api/profiles/apply_default` and removed the AuthGate best-effort fallback. Supabase session metadata remains the account/settings source.
- Deleted `/api/vantages/sync` and removed active/preset mirror calls. Assistant Profile presets continue to save to Supabase user metadata and local cache.
- Deleted unreachable `components/admin/LabControlsPanel.tsx`.
- Deleted the static Vantage control registry and its display-only Admin Console section.

## Deliberately preserved boundary

The following controls are still live and were not treated as dead code:

- `vs_vantage_id`
- `vs_vantage_mix`
- `vs_vantage_routing`
- `vs_vantage_limits`
- `vs_vantage_pragmatics`
- `vs_vantage_definition_overlay` / `vs_vantage_roleplay`
- Vantage-scoped `user_instructions` cards
- prompt-time preference/profile card loaders

The Verbal Sage chat and inspect routes still read these cookies and send their values to `/vantage/query`. Brains still consumes them for current response/retrieval behavior. The internal `vantage_identity.rag_policy` readers also remain until the reviewed RESSE response-mode/corpus contract replaces them.

These controls must be removed in replacement order, not by deleting the UI first:

1. Install the reviewed RESSE response-mode and corpus-selection contract.
2. Activate reviewed Memory V1 response preferences, project knowledge, and governed claims under their separate gates.
3. Remove Vantage from prompt-card selection and user-instruction ownership.
4. Make backend policy authoritative, then remove the remaining Assistant Profile dials/cookies and legacy card loaders.
5. Archive or delete preserved legacy tables and Qdrant points only after export/deletion semantics and restore boundaries are approved.

## Verification

Backend isolated and production checks passed:

- Python compilation
- `vantage_query_support_test.py`
- `query_embedding_cache_test.py`
- `memory_v1_intent_test.py`
- `memory_v1_shadow_policy_test.py`
- `legacy_rag_route_retirement_test.py`
- `legacy_route_retirement_test.py`
- explicit registered-route assertions

Frontend isolated and production Next.js builds passed, including TypeScript and all 71 static pages. Both production services are active; Verbal Sage returns HTTP `200`; Brains returns authenticated HTTP `200` and unauthenticated HTTP `401`.

The live Brains OpenAPI contains none of the eight retired route patterns and still contains `/vantage/query`, 27 nutrition paths, and 37 training paths. Qdrant `memory_claim_v1` remains green with six points.

Memory V1 owner-scoped counts remain `173 evidence / 6 claims / 3 preferences / 5 project heads / 6 outbox / 152 traces`; a different actor UUID sees zero across every relation. The legacy initiator remains inactive and disabled.

## Next Memory V1 step

Classify and review the accumulated non-injecting ordinary-use preference/project traces. Measure false positives, false negatives, minimality, cross-lane contamination, and latency. Do not enable preference/project prompt influence until that trace review is complete.
