# SeeBx refactor batch 03: response-memory internal naming

Date: 2026-08-20

## Scope

This batch removes retired successor/governed-memory terminology from the
private response execution boundary shared by normal SeeBx chat and LifeSwitch
chat. It does not change production, public request fields, public response
fields, database objects, stored provenance bytes, or hash domains.

The private execution result now exposes `memory_provenance`. The response
composer uses the neutral `memory_answer_binding` stage, and the finalization
module describes the active Zep lifecycle directly.

## Preserved compatibility boundary

The following legacy values remain intentionally unchanged:

- `governed_memory_successor_answer_provenance_v1`;
- `governed_memory.response_provenance.v1`;
- versioned prompt and orchestration fields whose serialized names are included
  in existing hashes or wire contracts;
- `zep_memory_v1`, the active lower-authority memory context identifier.

Those values cannot be renamed as ordinary code cleanup. They require a
separately versioned compatibility migration with dual-read or exact historical
verification. Their presence is therefore not evidence that the retired memory
runtime is still active.

## Changed files

- `seebx/capabilities/conversation/composition.py`
- `seebx/capabilities/conversation/finalization.py`
- `seebx/capabilities/conversation/lifeswitch_composition.py`
- `seebx/capabilities/conversation/router.py`
- `tests/test_conversation_composition.py`
- `tests/test_lifeswitch_conversation_composition.py`

## Verification

The sealed dependency environment passed `pip check`. Focused tests passed
15/15 after adding explicit assertions that both private execution models expose
`memory_provenance` and reject the retired Python field name.

The complete candidate suite passed 1,173/1,173 tests. Python compilation and
`git diff --check` also passed.

The first full-suite attempt used the production service virtual environment
and correctly failed ten unrelated tests because that environment does not
contain the build-only `jsonschema` package. No package was installed and no
production environment was modified. Re-running in the existing sealed
candidate dependency environment passed in full.

## Deployment state
