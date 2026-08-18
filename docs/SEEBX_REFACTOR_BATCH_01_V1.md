# SeeBx refactor batch 01 v1

## Remove the dead Qdrant surface from the deployed application

Status: isolated candidate implemented; not pushed or deployed

Source authority: `49f9e60cf4321c8e42c359845c1a62a8c987614d`

Risk: low runtime risk; no data mutation

Rollback: Git revert of the candidate commit; no database rollback required

Candidate commit: `10b5cb4a` on
`codex/seebx-qdrant-runtime-retirement-20260817`

## Why this is first

This is the smallest high-confidence separation between the active SeeBx
runtime and a retired architecture:

- `app.py` imports `QdrantClient` and `make_qdrant_client`;
- it creates a lazy `get_qdrant()` singleton and reads `QDRANT_URL`;
- no deployed-root caller invokes `get_qdrant()`;
- the live process receives `QDRANT_URL`, but no Qdrant service/container is
  running;
- `/healthz` advertises a default vector collection and Qdrant URL even though
  the active response/search/memory flow does not use them;
- Zep is the active conversational-memory provider.

Removing this false runtime surface makes health and dependency evidence more
truthful without touching historical data or dormant code.

## Exact code scope

### `app.py`

Remove:

- `from qdrant_client import QdrantClient`;
- `from rag_engine.qdrant_compat import make_qdrant_client`;
- the global `qdrant_client`;
- `DEFAULT_COLLECTION`, `EMBED_MODEL`, and `QDRANT_URL`, which are otherwise
  used only by the misleading health block;
- the complete `get_qdrant()` function;
- `general_rag_declared` from `/healthz`;
- `qdrant_access` from the nested retired-governed-memory health object;
- Qdrant references in the `/readyz` docstring.

Do not change chat, Zep, search, domain, voice, deletion, persistence, or owner
behavior in this batch.

### Tests

Update `tests/test_zep_runtime_retirement_v1.py` to require:

- Zep remains the declared memory provider;
- `app.py` contains no `qdrant_client`, `make_qdrant_client`, `get_qdrant`,
  `QDRANT_URL`, `qdrant_url`, or `qdrant_access` runtime surface.

Do not modify `tests/memory/test_exclusive_cutover.py` in this batch. Its
pre-change baseline has six failures and one error against the already-deployed
Zep runtime, including assumptions about removed successor globals and startup
behavior. It is historical-test debt and requires a separate disposition; it
must not be rewritten merely to make this batch green.

Use the updated Zep runtime-retirement test plus a clean-process import probe to
prove that importing `app` no longer imports `qdrant_client` or
`rag_engine.qdrant_compat`.

## Explicitly out of scope

- no database, schema, table, row, Qdrant collection, volume, snapshot, or
  container deletion;
- no systemd environment or secret changes;
- no removal of `qdrant-client` from `requirements-ci.txt`, because dormant
  historical tests still import it;
- no deletion of `rag_engine/qdrant_compat.py`, Vantage, governed-memory,
  `memory_v1`, scripts, tests, docs, or migrations;
- no deployment, restart, push, or production checkout edit;
- no response/search behavior change.

## Implementation procedure

1. Reverify production authority HEAD, clean state, and active leases.
2. Create or reuse a registered isolated worktree at the authority commit.
3. Acquire an exact lease for `app.py` and the named test files with `commit`
   authority only.
4. Record the focused-test baseline before editing, including pre-existing
   failures. Recorded result: current Zep retirement suite passed 3/3; the
   historical exclusive-cutover suite had six failures and one error.
5. Apply the bounded patch.
6. Run syntax/AST checks and focused retirement/runtime tests.
7. Import `app` in a clean process and prove Qdrant modules are absent from
   `sys.modules`.
8. Run the applicable full non-disposable test suite; classify any unrelated
   historical failures rather than masking them.
9. Inspect the exact diff and confirm only authorized files changed.
10. Commit once with the lease-bound identity, then release the stale lease.

## Candidate verification

Required evidence before considering deployment:

```text
git diff --check
python -m compileall app.py
focused Zep/runtime retirement tests pass
clean-process import of app succeeds
qdrant_client absent from imported modules
static scan finds no Qdrant runtime symbol in app.py
no unexpected changed files
```

The candidate health contract must remain owner-neutral and must report only
actually configured active capabilities.

## Candidate result

- Candidate commit: `10b5cb4a`.
- Exact diff: two files, 14 insertions, 40 deletions.
- Current Zep retirement contract: 3/3 tests pass after the change.
- AST parsing: passed for `app.py` and the updated test.
- Clean-process import from the candidate: passed.
- Imported Qdrant modules: zero.
- `app.get_qdrant`: absent.
- Direct candidate health result contains no Qdrant text.
- Direct candidate readiness returned PostgreSQL ready.
- Candidate and production worktrees remained clean.
- Production remained at `49f9e60c`; no deployment, restart, configuration, or
  data change occurred.

## Separate activation gate

Deployment requires new authorization and a deployment lease. Before restart:

- capture current service health and rollback commit;
- verify the candidate on a non-production port with the production-equivalent
  nonsecret configuration;
- verify `/healthz`, `/readyz`, response, thread/history, search, nutrition,
  training, voice, and Zep paths;
- ensure no frontend consumer requires the removed health fields.

After a successful deployment and observation period, a later configuration
batch may remove `QDRANT_URL` from systemd. Dependency/package removal waits
until the dormant Qdrant test and tooling archive is separately completed.

## Completion criteria

Batch 01 is complete only when the deployed `brains.service` process imports no
Qdrant module, receives no behavior from Qdrant settings, exposes no Qdrant
health surface, passes the bounded production checks, and retains a verified
Git rollback. No claim about schema, dependency, or historical-code retirement
is made by this batch.
