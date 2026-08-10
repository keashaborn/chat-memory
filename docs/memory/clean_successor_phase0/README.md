# Governed Memory clean-successor Phase 0

Status: design candidate only. Nothing in this directory is installed, configured, enabled, deployed, or authorized to mutate production data.

This checkpoint defines the clean successor before implementation. It is based on:

- seebx backend authority commit `cc12c834d9fc025b0208a06044b6760343fd03e5`, tree `de5466afdba81963a5369ca35cfb850298b270f6`;
- Verbal Sage frontend authority commit `9015eb0efc0cddbda3f9fd812c4f550b0c1f5b3b`, tree `eefeef9d588e9fcc369b4ab83d643d8ddf2f3df0`;
- the user decision to retain active Supabase accounts, preserve attachment capability without migrating current test attachments, perform no historical conversation backfill, reset assistant preferences, preserve only useful consent/security/deletion audit records, and treat all existing Memory claims, cards, vectors, jobs, review artifacts, and compatibility state as disposable.

Files:

- `CLEAN_SUCCESSOR_CONTRACT.md`: target authority, data, API, worker, security, and failure contracts.
- `COMPONENT_DISPOSITION.json`: machine-readable keep/replace/retire/unknown classification.
- `TEST_AND_RELEASE_GATES.md`: minimal current tests and proof required before cutover or retirement.

This phase creates no PostgreSQL database, Qdrant collection, service, timer, provider request, data migration, deletion, or production configuration.
