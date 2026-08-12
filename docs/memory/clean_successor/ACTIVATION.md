# Governed Memory Phase 8A promoted-proof activation boundary

The retained Phase 7C disposable revalidation passed. The Phase 8A controller
proof now also passed all 336 synthetic cases plus 21 positive and 5 refusal
PostgreSQL 16 cases, and its external metadata is promoted. The proved
59-artifact package remains byte-for-byte unchanged, including its frozen
proof-pending snapshot. Its Linux backend is hard-disabled. The candidate
remains uninstalled and is not authorized for installation or activation. The
Phase 8A operation did not create a persistent successor store; install, enable,
or start a service, timer, route, or listener; provision a role credential,
membership, or firewall rule; call a provider; or create a pilot marker. It
made no production-state change. This proof is not a new live-state inventory;
current absence must be re-established by a separate read-only audit before any
later operation. The release guard must continue to refuse creation with
`activation_blockers_open` and cleanup with `authorization_missing`.

## Exact future targets

Any later authorized pilot remains bounded to these seebx backend targets:

- HTTP `172.31.32.171:8091`, with intended frontend source
  `172.31.43.160/32`;
- canonical PostgreSQL `127.0.0.1:55432`, database `governed_memory`;
- derived Qdrant `127.0.0.1:6343`, collection
  `governed_memory_9a54cf123493_000001`, alias
  `governed_memory_active`; and
- the existing conversation database through exact SECURITY DEFINER RPCs.

Existing Memory databases, rows, collections, vectors, snapshots, volumes,
preferences, reviews, compatibility state and attachment content cannot seed
the successor.

## Authoritative blockers

The exact ordered blocker set in the immutable at-execution
`runtime_manifest.json` snapshot is:

1. `production_activation_not_authorized`
2. `inactive_installation_package_not_authorized`
3. `semantic_calibration_artifact_unapproved_retrieval_off`
4. `live_supabase_runtime_credentials_not_mounted_or_verified`
5. `previously_exposed_successor_credentials_not_rotated`
6. `database_role_credentials_not_provisioned`
7. `conversation_bridge_catalog_hash_not_provisioned`
8. `source_logging_policy_not_live_verified`
9. `source_logging_parameter_remediation_not_authorized_or_applied`
10. `pg_hba_and_transport_not_verified_for_runtime_logins`
11. `supabase_auth_sessions_rpc_not_installed_or_live_verified`
12. `fresh_isolated_persistent_postgresql_not_created`
13. `fresh_isolated_persistent_qdrant_not_created_or_approved`
14. `persistent_store_restart_supervision_and_boot_recovery_not_implemented_or_verified`
15. `canonical_postgresql_encrypted_backup_and_restore_not_proven`
16. `private_frontend_source_firewall_not_proved`
17. `tls_termination_or_private_transport_not_decided`
18. `production_store_runtime_credentials_and_role_activation_not_authorized_or_executed`
19. `successor_http_service_not_installed`
20. `successor_worker_service_not_installed`
21. `successor_conversation_capture_not_activated`
22. `successor_chat_deletion_route_candidate_not_installed_or_live_verified`
23. `frontend_successor_deletion_request_idempotency_and_confirmation_binding_not_implemented_or_verified`
24. `source_erasure_requester_membership_not_granted_or_verified`
25. `provider_adapter_real_call_validation_not_authorized_or_completed`
26. `embedding_adapter_real_call_validation_not_authorized_or_completed`
27. `projection_reconciliation_and_sequence_safe_qdrant_repair_not_implemented`
28. `legacy_project_memory_thread_dependencies_not_separated`
29. `trusted_web_transcript_composite_owner_thread_lineage_not_installed`
30. `legacy_chat_owner_thread_lineage_not_remediated`
31. `frontend_candidate_71377a_undeployed_visual_qa_pending`
32. `pilot_owner_and_scope_not_authorized`
33. `legacy_memory_owner_scoped_read_write_shadow_quiescence_not_proved`

## Ordered future sequence

Each item is a separate approval checkpoint:

1. retain the completed Phase 7C and externally promoted Phase 8A proofs unless
   their proof-critical bytes change;
2. separately authorize Phase 8B audit/remediation; no installation authority
   follows from that approval;
3. under that bounded authority, fix the PostgreSQL 16 ineffective `\quit 3`
   failure exit at
   `governed-memory-migrations/roles_preflight.pgsql:39`, then run fresh Phase
   7C failure-path revalidation;
4. package and approve the persistent Qdrant digest, stores-only supervisor and
   boot recovery, encrypted PostgreSQL backup/restore, runtime wheel, offline
   dependency wheelhouse, trusted clock with atomic nonce claim, global
   execution lock, external journal-seal anchor, exact live probes, and
   same-filesystem quarantine preflight; keep the Linux backend disabled until
   those gates pass;
5. require an externally signed final commit, tree, package, controller, and
   plan scope before any separate installation authority;
6. in Phase 8B, create only the exact fresh empty dormant successor stores;
   require zero source PostgreSQL connections, catalog reads, application-row
   reads, and writes at every stage; observe every exact target; keep HTTP and
   worker disabled and inactive while treating the stores-only supervisor as a
   separate state; and retain all named roots plus the nonce-bound quarantine
   path after empty rollback; the v2 decision receipt permits canonical
   migrations 0001, 0003, and 0004 only and excludes source PostgreSQL and
   migration 0002, with no provider credentials or API/worker LOGIN roles;
   treat every listed state as a required future observation, not current/live
   evidence;
7. separately authorize Phase 8C, remediate and re-prove source logging, fix the
   ineffective `\quit 3` failure exit at
   `governed-memory-migrations/0002_conversation_bridge/forward.pgsql:20`, run
   fresh Phase 7C failure-path revalidation, then install only the inactive
   source roles and conversation bridge with no runtime memberships;
8. rotate the exposed provider key and mint distinct successor runtime
   credentials only under a later activation authorization;
9. prove pg_hba, private transport/firewall, live Supabase account/session
   authority, chat-only route behavior, and frontend semantics;
10. authorize credential provisioning, enable only exact API/worker LOGIN roles,
   and grant only exact runtime memberships;
11. recompute and seal the full post-membership conversation catalog hash;
12. start the successor alone, prove exact routing and owner isolation, and
    complete authenticated visual QA;
13. authorize one bounded pilot; and
14. only after an observation window and rollback proof, consider legacy
    retirement as separately authorized exact batches.

The inactive install-postflight catalog hash cannot be reused after LOGIN or
membership changes. The container bootstrap administrator remains active until
a separate recovery administrator is tested.

## Deletion and rollback boundary

Deletion remains chat-only. Accounts and structured LifeSwitch data, including
libraries, workouts, weightlifting sessions, food logs, measurements, plans and
tracking records, remain outside the deletion graph.

Cleanup is hard-refused without separate authorization. Never use wildcard or
prefix teardown, SQL `CASCADE`, or caller-supplied counts as authority. Before
any pilot row, rollback may remove only exact empty candidate targets under the
sealed empty-only preconditions. After any pilot row, rollback means quiesce
routes/services and retain stores for recovery; it is not schema teardown.
