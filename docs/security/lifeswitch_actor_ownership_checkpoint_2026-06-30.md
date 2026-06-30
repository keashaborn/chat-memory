# LifeSwitch Actor Ownership Security Checkpoint

Date: 2026-06-30
Server: seebx / Brains backend
Path: /opt/chat-memory
Service: brains.service

## Summary

Completed a backend security-hardening pass for LifeSwitch and related user-owned routes.

The purpose was to prevent access or mutation of another user's data by changing owner_user_id, object ids, thread ids, card ids, relationship ids, or conversation ids.

## Security model

Protected Brains routes now use two boundaries.

1. Service boundary

Required header:

    x-vs-service-token

This proves the request came through trusted infrastructure, normally the Verbal Sage / LifeSwitch frontend BFF.

2. Actor boundary

User-owned LifeSwitch routes also require:

    x-vs-actor-user-id

Brains verifies the actor against the requested owner.

For owner routes:

    x-vs-actor-user-id == owner_user_id

For object-id routes, Brains derives the owner from the database object and verifies the actor against that derived owner.

## Shared helper

Created:

    rag_engine/lifeswitch_auth.py

Primary helper:

    require_actor_matches_owner(req, owner_user_id)

Behavior:

    missing actor -> 401 missing_actor_user_id
    wrong actor   -> 403 actor_owner_mismatch
    invalid UUID  -> 400 invalid actor_user_id / owner_user_id
    match         -> canonical owner UUID string

## Areas hardened

LifeSwitch nutrition:
- nutrition log entry/day/batch routes
- my_foods routes
- servings routes
- food override routes
- meal plan routes
- meals and meal item routes

LifeSwitch training:
- my_exercises routes
- conditioning prescription routes
- workout template routes
- workout template exercise and segment routes
- workout template share routes
- workout session routes
- set and set segment routes
- conditioning session routes

LifeSwitch plan:
- profile
- profile upsert
- comments
- comment create
- history

LifeSwitch measurements:
- entries
- entry create
- entry deactivate

LifeSwitch people/sharing:
- invitations create/list/accept/revoke
- relationships list/upsert
- relationship permissions list/upsert
- permissions granted-to-me
- profiles
- conversations
- direct conversations
- conversation messages

Related non-LifeSwitch hardening:
- thread routes
- card and vantage-card routes
- user export/delete/recent routes
- memory feedback

## Intentional public exemptions

The service-token middleware intentionally leaves selected routes public or semi-public:

    /catalog/
    /lifeswitch/training/workout_template_shares/preview
    /lifeswitch/people/invitations/preview
    /ws/voice

These should be reviewed again if public-preview behavior changes.

## Verification

Representative backend tests passed:

    token missing       -> 401 missing_or_invalid_service_token
    token + no actor    -> 401 missing_actor_user_id
    token + wrong actor -> 403 actor_owner_mismatch
    token + actor match -> 200

Tested representative areas:
- nutrition log/day
- nutrition my_foods
- nutrition meals
- training sessions
- training conditioning_sessions/create
- training workout_template_shares/import
- plan profile
- measurements entries
- people relationships
- people conversations

Final backend audit passed:

    OK: no owner_user_id LifeSwitch routes missing require_actor_matches_owner
    OK: no object-id-only LifeSwitch routes missing derived/actor ownership check
    compile OK
    brains.service active
    git status clean

Frontend smoke test passed:
- LifeSwitch Nutrition works
- LifeSwitch Training works
- LifeSwitch People/messages works
- Verbal Sage chat works

## Relevant commits

    0d9ffd0 Enforce actor ownership on thread routes
    b767962 Enforce actor ownership on card and user routes
    65efa55 Enforce actor ownership on memory feedback
    4fceeab Enforce actor ownership on LifeSwitch nutrition log routes
    79049f1 Enforce derived ownership on LifeSwitch nutrition object routes
    8865929 Enforce actor ownership on remaining LifeSwitch nutrition routes
    66d4760 Enforce actor ownership on LifeSwitch training basics
    f7e382e Enforce actor ownership on LifeSwitch workout templates
    3877a4a Enforce actor ownership on LifeSwitch training sessions
    2fc0fa3 Enforce actor ownership on remaining LifeSwitch training routes
    10c4f08 Enforce actor ownership on LifeSwitch plan and measurements
    cc174ee Enforce actor ownership on LifeSwitch people routes

## Known caveats

1. Frontend BFF route audit is still needed.

Goal:
- list every Next.js API route that calls Brains
- confirm x-vs-service-token forwarding
- confirm x-vs-actor-user-id forwarding where required
- confirm preview/catalog exemptions are intentional
- identify dead or legacy BFF routes

2. OpenAI consolidation is still needed.

Goal:
- remove or park Grok/Brock/provider complexity
- make OpenAI the single LLM path
- simplify model/provider selection

3. Memory/card system audit is still needed.

Goal:
- identify what memory/card/profile systems are actually used by /vantage/query
- remove duplicate or dead systems
- make retrieval/profile injection observable

4. /health is currently protected by the service-token middleware.

This is acceptable unless an external unauthenticated health checker depends on it.

5. A possible nutrition SQL issue was noticed earlier in create_my_food_from_catalog.

This was not part of the security-hardening pass and should be handled separately if the route is active.

## Recommended next step

Proceed to frontend BFF route audit.

Frontend server:

    172.31.43.160

Frontend path:

    /var/www/verbalsage-chat_v2
