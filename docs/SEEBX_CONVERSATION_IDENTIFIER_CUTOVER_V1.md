# SeeBx conversation identifier cutover v1

## Purpose

New conversation responses use product-neutral SeeBx identifiers. Immutable
historical rows and response traces remain readable through exact, bounded
legacy allowlists. This removes retired RESSE implementation names from active
writers without rewriting history.

## Canonical writers

The sole registry is `seebx/contracts/conversation_provenance.py`. New backend
writes use:

- assistant transcript source `backend/seebx:assistant:v2`;
- ordinary runtime and transcript tag `conversation_response_v1`;
- LifeSwitch runtime `lifeswitch_response_v1`;
- shadow trace `conversation_response_shadow_trace_v1`;
- safety assessor `conversation_safety_assessor_v1`;
- response profile `default_response_policy`.

The paired frontend registry writes the same runtime values. Historical trace
decoders retain the exact v0.2, v0.3, and v0.4 runtime values; they do not admit
arbitrary strings.

## Historical read boundary

Transcript and prior-answer provenance readers accept exactly:

- `backend/resse:assistant:v1` for existing rows;
- `backend/seebx:assistant:v2` for new rows.

The attestation hash contract remains `assistant_transcript_attestation_v1`.
Its hash payload never included the source label, so existing attestations are
not altered or reissued.

## Release order

1. Verify the source-view migration against a disposable database restored
   from the approved backup.
2. Apply the expanded historical-reader view before activating canonical
   backend writers.
3. Activate the exact backend and frontend candidate commits together.
4. Verify ordinary and LifeSwitch responses, transcript persistence, prior
   provenance, Zep synchronization, and frontend trace decoding.

Applying the migration, deploying either candidate, or restarting a service is
not authorized by this document.

## Rollback boundary

The expanded database view is backward compatible with old backend code and may
remain installed during a code rollback. The SQL rollback refuses to restore
the old-only view after any canonical-source row exists, preventing new history
from becoming invisible. No rollback deletes or rewrites transcript rows.
