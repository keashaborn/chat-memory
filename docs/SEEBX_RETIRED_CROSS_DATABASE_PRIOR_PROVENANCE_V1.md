
# Retired cross-database prior-answer provenance path v1

Status: retired in candidate; no production change authority

The former `read_prior_answer_lifeswitch_provenance_v1` view attempted to join
`lifeswitch_chat` objects from the isolated LifeSwitch database with `memory`
objects from the platform database. Those schemas do not coexist in either
database, so the migration was not deployable as written.

The executable view migrations, PostgreSQL reader adapter, and clone-only tests
are removed from the active candidate. Conversation composition retains an
explicit `OFF` value so stored manifests and prompt contracts remain stable.
The current owner-scoped final-answer provenance receipt remains as the separate
`lifeswitch_answer_provenance_receipt_v1` governed migration.

Any future prior-answer retrieval must use an explicit application-layer join
between separately owner-bound adapters, with independent database credentials,
hash-bound inputs, RLS proof, and a new versioned contract. The retired SQL must
not be restored or activated.
