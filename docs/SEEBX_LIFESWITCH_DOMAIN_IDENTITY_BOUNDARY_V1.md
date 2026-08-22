# SeeBx LifeSwitch domain identity boundary v1

Status: candidate security correction; no deployment authority

Evidence date: 2026-08-21 America/Chicago

Candidate base commit: `4240a578968abc58e3f96dc6242281e81cf5a7ed`

Paired frontend candidate: `6826f8e3e6e55f9937dc112f91a1e9bedc8a5fdb`

## Finding

The paired frontend correctly forwarded the original Supabase bearer through
the shared LifeSwitch proxy boundary, but retained backend Measurements,
Nutrition, Plan, and Training routes still consumed
`require_actor_matches_owner`. That helper validates only the asserted actor
header. A valid service token plus a forged matching actor/owner header could
therefore reach domain repositories without the bearer being verified.

The service token remains necessary for frontend-to-backend transport, but it
is not user identity. The actor header remains a useful assertion, but it is
not user identity. Supabase access-token verification or an active owner-bound
voice-session lease is the user-authority boundary.

## Candidate correction

All eleven retained LifeSwitch domain capability modules now import identity
only from `seebx.core.identity`:

- 70 owner-parameter paths await `require_actor`, which verifies Supabase or an
  active voice lease and then binds the verified actor to the requested owner;
- eight database-ID paths await `require_request_actor` before accepting a
  database-derived owner;
- eight repository callbacks compare that already verified actor with the
  database owner through `require_verified_actor_matches_owner`;
- zero raw `require_actor_matches_owner` references remain in Measurements,
  Nutrition, Plans, or Training capability modules.

The database adapters, SQL, transaction boundaries, response contracts, route
registration order, and OpenAPI document are unchanged.

## Verification

- A direct raw-actor-header request with no bearer is rejected with HTTP 401
  before repository access in each of Plan, Nutrition, Training, and
  Measurements.
- The architecture guard parses all eleven modules, prohibits the raw helper,
  and requires every canonical asynchronous identity call to be awaited.
- The core identity tests prove verified-actor/database-owner match and
  cross-owner denial.
- 46 focused identity/domain tests pass.
- The complete backend suite passes 1,354/1,354 tests in the sealed Python 3.12
  runtime.
- Parent and candidate both expose 138 routes and 124 OpenAPI paths with exact
  route SHA-256
  `308c0e6506dbfec277f799ca19c01cbd457afaba8285e57ef5b1558b19f5f446`
  and OpenAPI SHA-256
  `0c98736818a52d53797460c1f207f014d9882af64400363ae1fd69c21f54b46a`.
- On the paired frontend candidate, 75 LifeSwitch proxy header calls use the
  shared request-bound forwarding helper. Four bearer/wiring tests and ten
  four-macro scoring tests pass.

## Remaining gate

This proves code-level credential consumption and raw-header rejection. It
does not yet prove a real signed Supabase session through the paired frontend,
backend, and disposable database; application route behavior against the
restored database; enabled People delegation; deployment; cutover; or rollback.
Those remain separate gates. Production is unchanged.
