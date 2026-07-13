# Memory V1 controlled evidence apply path

Status: deployed and applied on 2026-07-13. The batch is immutable and exact
replay is read-only.

## Locked production batch

- Owner: `1240822d-ac9a-4096-95aa-e2b24d36ef50`
- Source: 140 `public.chat_log` rows with role `frontend/chat:user`
- Source terminal cursor: `2026-07-13T11:24:55.273202Z`,
  `084366e0-e4a3-43ea-9ee3-91931a1d5dfd`
- Source snapshot SHA-256:
  `dd7f3c9983e47aa7e9d214ef7ec8e6463dd3fbda6248c778271144afb46c5c33`
- Reviewed report SHA-256:
  `d122028cc07abc8d3d122ba9779f04514ec75c6be1996cf2256854189c1f642a`
- Plan fingerprint:
  `201a828c35b6d1717c5b36061960cb32a8732bfa39ca59fe19e3c1d1ab404fad`
- Planned evidence: 164 rows: 102 atomic and 62 reviewed compound children
- Batch ID: `15064e5d-8cd3-5611-9cbf-db177d24a3a0`

New chat rows after the terminal cursor are outside this batch. A deletion,
edit, or backdated insertion inside the locked cohort changes its count or hash
and fails closed.

## Authorization boundary

`ops/manifests/memory_v1_evidence_apply_20260713.json` is the sole production
authorization manifest. It locks the owner, source snapshot, artifact manifest,
reviewed report, plan fingerprint, exact counts, batch identity, and allowed
writes. Authorization commit `63627e9` changed only `apply_authorized` to true.
The authorized manifest SHA-256 is
`0a67fdf84723609b999b95e88c4d2437bda19163fb9a7f0cc36d715d0f954f57`.

An apply requires all three controls:

1. a reviewed manifest change to `"apply_authorized": true`;
2. the CLI `--apply` flag;
3. exact confirmation `APPLY_REVIEWED_164_EVIDENCE_ROWS`.

Changing the manifest changes its SHA-256. The committed batch records that
exact authorization-manifest hash.

## Production result

The pre-apply restore point is
`/home/ubuntu/brains/snapshots/memory_pre_reviewed_evidence_apply_20260713T213746Z.dump`
(72,484,006 bytes, mode `600`, 909 restore-catalog entries, SHA-256
`d82d2eaf0905773e4571ed5c7a519bc9d64ba03764c1f3e3f5a0e642b040291c`).

Batch `15064e5d-8cd3-5611-9cbf-db177d24a3a0` committed 164 inserts and zero
reuses. Evidence increased from 9 to 173. Candidates remained 7, claims 6,
preferences 0, projection outbox 6, and lifecycle events 0. All 164 rows are
active; 152 are high sensitivity and 12 restricted. Retrieval, prompt use, and
candidate creation remain disabled on every row.

The exact replay returned `verified_replay` with zero writes. A different actor
UUID saw zero batch headers and zero batch rows. Brains remained active and
Qdrant `memory_claim_v1` remained green with six points.

## Transaction

`rag_engine/memory_v1_evidence_apply.py` runs one repeatable-read transaction:

1. Acquire an owner-and-batch advisory transaction lock.
2. Read only the locked source cohort under the trusted database session role.
3. Switch to effective role `brains_app`, set transaction-local `app.user_id`,
   and require forced owner RLS plus SELECT/INSERT-only grants.
4. Rebuild triage, atomic spans, compound resolutions, and the evidence plan.
5. Match the source hash, artifact-manifest hash, reviewed-report hash, plan
   fingerprint, summary, and every reviewed row.
6. Require all 164 identities to be absent when no batch ledger exists.
7. Insert all evidence with one `INSERT ... jsonb_to_recordset ... ON CONFLICT
   DO NOTHING RETURNING` statement and require exactly 164 returned IDs.
8. Reselect all 164 rows under owner RLS and compare every immutable field.
9. Insert one batch header and 164 row-ledger entries in the same transaction.
10. Verify evidence increased by exactly 164 while candidates, claims,
    preferences, projection outbox, and lifecycle events did not change.
11. Commit. Any failed check rolls back evidence and audit rows together.

No Qdrant client, candidate extractor, claim promotion, preference writer, or
prompt builder is imported or called.

## Replay

If the batch header already exists, the path performs no writes. It verifies the
authorization hash, header, all 164 ledger rows, and every immutable evidence
field. Partial evidence without a matching batch ledger fails closed.

## Database audit schema

`memory.evidence_ingest_batch` and `memory.evidence_ingest_batch_row` are forced
RLS, owner-scoped, and SELECT/INSERT-only for `brains_app`. UPDATE and DELETE are
blocked by both grants and append-only triggers. Row entries contain evidence
IDs and hashes, not raw content. The guarded rollback refuses nonempty audit
tables.
