# Memory V1 deferred reconciliation report-only scheduler V5

Status: audit-only. No automatic reconciliation is permitted.

Server: seebx backend.

Each scheduled owner scan calls the controlled dry-run scanner and appends one
force-RLS audit row containing:

- owner and run UUID;
- hash of the approved owner roster;
- candidate limit and count;
- exact candidate manifests and canonical hash;
- run manifest, worker identity, session, and timestamp.

The worker uses an explicit hash-checked owner roster. It never discovers
owners from memory rows. The current environment has no Supabase service-role
credential, so automatic `auth.users` synchronization is not available.

The audit function cannot invoke review, assessment, reconciliation, Qdrant,
retrieval, or prompts. Exact owner/run replay writes zero rows. The same run UUID
is isolated by owner.
