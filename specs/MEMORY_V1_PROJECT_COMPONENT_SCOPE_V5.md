# Memory V1 Project Component Scope V5

Status: offline contract; production inactive.

Server boundary: seebx backend. This contract does not change Verbal Sage,
RESSE, Resse-Train, Qdrant, retrieval, prompts, or another owner's records.

## Identity model

`memory.project_space` remains the product-level namespace. For Eric's owner
scope, the single root is `verbal-sage`. Memory V1, RESSE, LifeSwitch, and the
chat/runtime are not peer projects. They are components within that project.

`memory.project_component_v5` records an immutable, owner-scoped component
identity. A nullable `parent_component_id` supports nested components without
copying the project namespace. Component and parent foreign keys include
`owner_user_id` and `project_id`; cross-owner and cross-project links are
structurally impossible.

`memory.project_component_alias_v5` contains exact normalized aliases. An alias
is unique inside one owner/project pair, so a source name cannot silently map to
two components. Registration is controlled through
`memory.apply_owner_project_component_v5`; the application receives no direct
mutation permission.

The initial intended hierarchy is:

```text
verbal-sage
├── chat-runtime
├── memory-v1
├── resse
├── lifeswitch
│   ├── lifeswitch-nutrition
│   └── lifeswitch-training
└── agent-architecture
```

This document does not register those rows. Registration is a later reviewed
production operation.

## Resolution contract

A thread binding establishes only the trusted root project. It never assigns
all observations to one component.

For each project observation:

1. An explicitly named root (`Verbal Sage`) resolves to root scope.
2. An explicitly named registered component or unambiguous registered alias
   resolves to that component within `verbal-sage`.
3. An anonymous `project:current_thread` reference may resolve only to the root
   project. It receives no component merely from nearby words or thread title.
4. A name not present in the trusted owner/project registry remains unresolved.
5. An ambiguous alias is impossible to register. Historical ambiguity or
   insufficient source context is deferred for review.
6. A component name never changes the owner or project selected by the backend.

Examples:

| Source proposition | Root | Component | Result |
|---|---|---|---|
| `Verbal Sage must isolate every account.` | verbal-sage | null | resolved root requirement |
| `Memory V1 should retain evidence.` | verbal-sage | memory-v1 | resolved component requirement |
| `LifeSwitch nutrition needs serving quantities.` | verbal-sage | lifeswitch-nutrition | resolved component requirement |
| `The system should improve that.` | verbal-sage | null | root-only or deferred; never guessed |
| `Another Project should do X.` | null | null | unresolved |

## Memory-lane boundaries

Component scope applies only to `project_knowledge`. It does not reclassify
personal evidence, response preferences, life preferences, or structured
application values.

- `My mother died` remains owner-scoped personal evidence.
- `Keep answers concise` remains a response preference.
- A meal or training set remains canonical structured LifeSwitch data.
- `LifeSwitch should calculate meal totals from servings` is project knowledge
  scoped to `lifeswitch-nutrition`.
- RESSE/FM corpus passages remain corpus knowledge; RESSE implementation or
  response-policy requirements are project knowledge scoped to `resse`.

## Security and lifecycle

- All component tables use forced RLS based on `memory.current_actor_user_id()`.
- Registration functions are security-definer functions owned by the locked
  `memory_v5_extraction_maintainer` role.
- `brains_app` can execute bounded functions and read its visible rows, but it
  cannot insert, update, or delete component rows directly.
- Component, alias, and registration-event tables are append-only.
- Operation IDs are hash-bound. Exact replay is zero-write; conflicting replay
  fails closed.
- A component alias is unique per owner/project.
- Parent links use composite owner/project foreign keys.
- No component registration enables extraction, retrieval, Qdrant projection,
  or prompt influence by itself.

## Activation sequence

1. Install and clone-test the empty component registry.
2. Register the reviewed hierarchy for the target owner.
3. Extend normalized V5 `project_scope` with an optional component key and a
   server-owned resolution source.
4. Teach the bounded worker to read only the trusted registry for the bound
   owner/project.
5. Persist component scope in project-knowledge staging and durable heads.
6. Test root-only, component-specific, ambiguous, cross-owner, replay, and
   mixed-thread cases.
7. Bind reviewed threads and run zero-influence canaries before retrieval.
