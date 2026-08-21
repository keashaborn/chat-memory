
# Legacy attestation reconciliation v1

Status: candidate only; production database unchanged

The legacy `memory.assistant_transcript_attestation_v1` table contains 190 rows.
Exactly 32 still match a live owner/thread-bound `public.chat_log` row and can be
inserted into `chat_integrity.assistant_transcript_attestation_v1`. The other 158
rows are orphaned from current chat history and must be preserved in an
AES-256-GCM encrypted, mode-0600, hash-bound quarantine before reconciliation.

Frozen classification hashes:

- source 190: `c07967305707d83e258bd441939359b472b95bb414eeb739a7edd99a5f657831`;
- eligible 32: `4b3917cac82fc5a4522a2bb8e0a9296651daedc3d94fb6b02840d9b5f6b64678`;
- quarantine 158: `38f20b7df5cbd1d3a8d413825eb76a6a7a682f0e78b491e1aa09a315113d90e1`;
- canonical destination before reconciliation 101:
  `40cb618be4a0d2b87f7ee8f3ad71ffe1e5f9e68fe73aefb0299577f4517e6c14`.

The candidate reconciliation migration is insert-only and refuses any existing
answer-id conflict. It does not delete source rows or schemas. The old schema
retirement package now always fails closed and contains no `DROP SCHEMA`.

Activation requires a fresh encrypted full backup, disposable restore receipt,
successful encrypted-quarantine receipt, exact migration dry run in the restored
database, and a complete outside-schema catalog comparison proving that a future
schema retirement cannot remove an external object. Those actions require fresh
production authorization.
