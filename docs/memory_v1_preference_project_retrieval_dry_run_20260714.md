# Memory V1 preference/project shadow retrieval dry run — 2026-07-14 UTC

## Scope

Server: seebx backend. Branch: `memory_v1_foundation`. Starting commit: `e08936a`.

This phase designed and tested read-only retrieval for the three durable preferences and five durable `verbal-sage` project-knowledge records. It did not modify the live request route, prompts, answers, Qdrant, Postgres data, retrieval traces, or RESSE policy.

## Runtime boundary

`memory_intent` is an explicit input to governed memory selection. It is not derived from or replaced by RESSE `response_mode`.

- Response preferences compile into backend controls. They are never returned as factual or conversational content.
- Life preferences require a matching memory intent, domain, surface policy, sensitivity ceiling, current revision, and fully active evidence.
- Project knowledge requires an exact owner and project key, project-specific memory intent, topic relevance, allowed document state, sensitivity ceiling, current revision, and fully active evidence.
- Historical project records may answer recall or status questions but cannot act as current plans or decisions.
- Project records with any relation are rejected until the conflict, qualification, dependency, derivation, and supersession retrieval contract is designed.
- Selection is token- and record-budgeted. Response controls consume zero content tokens.

The initial candidate generator is deterministic lexical overlap over one owner/project snapshot. It is bounded to 200 preferences and 500 project records and fails closed above either limit. This is sufficient for the locked first batch, not the final large-catalog candidate generator. Indexed full-text or vector candidate generation must be evaluated before broad activation.

## Security and read contract

The loader:

- connects as `brains_app`;
- starts a repeatable-read, read-only transaction;
- derives all visibility from transaction-local `app.user_id`;
- requires enabled and forced RLS on the nine source tables;
- filters by the authenticated owner UUID and exact project key;
- rechecks revision pointers, revision hashes, sensitivity, evidence status, effective time, and relation state;
- has no INSERT, UPDATE, DELETE, Qdrant, prompt, or retrieval-trace path.

Vantage identifiers are absent from the API, queries, ownership model, selection policy, and report.

## Locked inputs

| Input | SHA-256 |
|---|---|
| Durable apply manifest | `83f6ccb0fdea3adec11540d1c3d32a83dffc89dc32d8c1edf740546d85de48f6` |
| Committed durable apply report | `5bd1b1e52eec95f2e9fc9e8ed33a2ffc2025e71774e19d38355b8f530541125d` |
| Retrieval manifest | `bb0a78e87146ecacc1db8843713cc987e301ea3f15214f62258d29a969e4e662` |
| Evaluation cases | `52e66942b94a280ea6b2354c37c960449adadcabbf767b120ff7efe857d99334` |

The retrieval manifest locks every preference/project ID, current revision ID, and content hash. Any changed durable input fails closed.

## Evaluation result

All 15 deterministic cases passed:

- Music recommendation and preference recall selected exactly the two music preferences.
- General music discussion selected no preference.
- The Jerry/DeeDee response preference compiled into a zero-token direct-relevance control; it suppressed an unrelated candidate and allowed a directly relevant recall candidate.
- Fractal Monism memory planning selected the proposed memory feedback-loop roadmap only.
- Legacy memory status selected the historical hybrid-system record only.
- AB-design planning, website-development history, and FM dataset history each selected only their matching record.
- A broad memory-direction status query selected the roadmap plus historical hybrid-state record.
- Vague project, wrong-project, unrelated general, and sensitivity-cap cases selected nothing.

Production remained unchanged:

| Count | Before | After |
|---|---:|---:|
| Durable preferences | 3 | 3 |
| Durable project records | 5 | 5 |
| Projection outbox | 6 | 6 |
| Retrieval traces | 30 | 30 |

A different owner context saw zero preferences, project records, projection rows, or retrieval traces. Full isolated Memory V1 CI passed, including the new deterministic policy test.

Dry-run report SHA-256: `e73953554d0468f2a6f378aad4fc846ab96e3ecb8665c329fc209ec24722b0e2`.

## Stop boundary

No runtime route calls this selector. Nothing was injected into a prompt or exposed to an answer model. The next shared integration phase must define the adapter from request classification to `memory_intent` while keeping it independent from RESSE `response_mode`. Only after joint review should an allowlisted, non-injecting runtime shadow call be authorized.
