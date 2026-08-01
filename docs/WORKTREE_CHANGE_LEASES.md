# Chat-memory worktree ownership and change leases

## Decision

Use one event-sourced registry and one immutable-by-identity control installation outside every Git worktree. Every registered worktree is classified before it can receive a lease. Every lease binds the Unix owner, task and thread identifiers, server, repository, exact worktree identity, branch/ref, starting commit, authority commit/tree, worktree inventory, path scopes, change types, acquisition/expiry, and current revision.

The authoritative audit is a sequence of canonical JSON events. Events are created under one process lock, hash-chain to the previous event, and are published with a hard-link no-replace commit after a mode-0600 staging file is fully written and fsynced. Replay rejects gaps, duplicates, unknown files/fields, noncanonical JSON, hash changes, clock rollback, stale revisions, unsafe paths, or permission/identity substitution.

Operational code is installed once under `/var/lib/chat-memory-change-leases-v1/control`, not inside a branch or worktree. Git `core.hooksPath` is the exact absolute path `/var/lib/chat-memory-change-leases-v1/control/hooks`. A canonical install manifest binds the device, inode, owner, mode, size, and SHA-256 of the lease tool, guard adapter, pre-commit hook, and pre-push hook, plus the identities of the private control directories. Each guard call revalidates that manifest before invoking the lease tool.

## Collision and checkpoint policy

- The same worktree or branch/ref is always exclusive.
- `push`, `deploy`, `handoff`, and `production-write` are repository-global exclusive operations.
- Commit-only leases may coexist only on different worktrees/branches with disjoint repository-relative path scopes.
- An expired but unreleased lease continues to block collisions. It is not silently abandoned. The owner may release it; after the configured grace period a manager may append a recovery. A manager may append an immediate revocation with a reason.
- Request IDs are idempotent. Replaying an identical request returns its prior event; reusing a request ID with different intent fails.
- A lease binds one starting HEAD. Once a commit changes HEAD, that lease no longer passes any guard. Release it and acquire a fresh operation lease before another commit, push, deploy, or handoff. Version 1 has no automatic checkpoint transition.

## Existing-worktree and chat adoption

Activation begins with a short change freeze and a fresh read-only inventory. All existing registrations receive an initial classification event. Clean worktrees remain `unclaimed_pending_review` until the manager confirms availability. Dirty worktrees require a task/thread owner and exact status hash before `claimed_active`. Missing registrations remain `missing_registered`; owner-mismatched paths remain `owner_mismatch`; neither state is treated as abandoned or deleted. Detached worktrees require an explicit `claimed_active` owner.

Repository `AGENTS.md` supplies automatic discovery only when a worktree's checked-out commit contains that file. Future worktrees based on the authority commit inherit it. Existing open chats and older worktrees that do not contain it require one manager adoption message with task ID, thread ID, registered worktree, and the absolute `discover` command. No chat may infer ownership from earlier conversation or merely from a branch name.

## Controlled paths and limitations

This activation enforces ordinary Git commit and push through shared absolute hooks. It also provides one guard command for the manager's sanctioned deploy/handoff workflow. It does **not** modify the inactive daily Git sync or any of the 144 observed legacy production/activation entrypoints. Those paths remain outside technical enforcement; operators must not invoke them. The inactive sync would encounter Git hooks if enabled and attempting commit/push, but it is not separately patched or activated.

The current Unix model is not a security boundary between chats: they and the configured manager role share UID 1000. A same-account process can claim another task ID, use `git --no-verify`, invoke a legacy production script, change Git configuration, or replace and recompute a user-owned audit/control manifest. Codex handoff is an app-side action and cannot be intercepted by a repository hook. Full tamper resistance requires a separately privileged broker, app-issued thread-bound credentials, off-host audit-hash anchoring, and migration of each sanctioned mutation entrypoint. This version prevents ordinary concurrent-operation mistakes; it does not resist a malicious same-account process.

## Bounded activation plan

1. Revalidate the exact production commit/tree/ref/worktree identity, clean state, index immutability, and zero Git locks/conflicting processes. Record the prior `core.hooksPath` state and freeze Git/worktree changes. Require all exact activation targets to be absent: `/etc/chat-memory`, `/etc/chat-memory/change-leases-v1.json`, `/var/lib/chat-memory-change-leases-v1`, `/var/tmp/chat-memory-change-leases-v1-activation-v1`, and the two new tracked discovery destinations. No-clobber stage the four reviewed control files plus `AGENTS.md` and `docs/WORKTREE_CHANGE_LEASES.md` under the private mode-0700 staging path, verify their blueprint hashes/modes, and record their identities. Staging creates no registry or audit state.
2. No-clobber create `/etc/chat-memory` owned by UID 1000, mode 0700. Write only the canonical `/etc/chat-memory/change-leases-v1.json` with `O_EXCL`/no-follow, mode 0600, fsync the file and directory, and record both identities plus the file hash. Do not create `/var/lib/chat-memory-change-leases-v1` directly.
3. Invoke `initialize` exactly once from the verified staged `chat_memory_lease.py` with the approved manager task/thread identity. `EventRegistry.initialize` must itself create `/var/lib/chat-memory-change-leases-v1`, `events`, and `staging` mode 0700 plus `registry.lock` mode 0600. It self-removes only its own partial components if initialization fails. Verify and record all four created identities before continuing.
4. Under the initialized registry root, no-clobber create `control`, `control/bin`, and `control/hooks` mode 0700. Install `chat_memory_lease.py`, `chat_memory_lease_guard.py`, `pre-commit`, and `pre-push` mode 0500. Generate the measured canonical `control/control-install-manifest.json` mode 0600, fsync every file and directory, and validate every recorded identity/hash/mode through the installed guard validator.
5. Invoke `seed-pending` from the installed control tool exactly once. It appends one idempotent classification event for each of the 216 registrations and does not alter worktrees, refs, indexes, branches, or files. After the first event is durable, the registry/audit is retained on every later failure.
6. Manager-classify the authority worktree, acquire one bootstrap commit lease, add only tracked `AGENTS.md` and `docs/WORKTREE_CHANGE_LEASES.md`, then set repository-local `core.hooksPath` to the absolute shared hook directory. Verify both hooks invoke the external guard from an old synthetic worktree before committing. Commit the two discovery files only; do not push.
7. Release the now-stale bootstrap lease. Require a fresh lease for any later push, deploy, handoff, or commit. Send one adoption message to every existing open chat/older worktree and keep ambiguous registrations pending.
8. Verify audit replay, hook denial without identity, scoped acceptance with a synthetic lease, unchanged production services/data, and exact closing repository identity expected from the single discovery commit. Remove only the exact recorded temporary staging files/directory after identity/hash revalidation. Stop; deployment, restart, database/Qdrant changes, pushing, pruning, and worktree cleanup remain out of scope.

The activation deliberately leaves sync/deploy/handoff/legacy scripts unmodified. Only the absolute hooks and the manager guard workflow are controlled in this boundary.

## Rollback

Before the first audit event, a failed activation removes only run-recorded staging, control, registry, and configuration paths in reverse creation order after exact identity/hash/mode revalidation. It never removes a preexisting, substituted, concurrent, nonempty, or unrecorded path. Registry initialization already cleans its own partial components on initialization failure. If any event exists, retain the entire registry/audit and its configuration/control installation for diagnosis; remove only the exact temporary staging files whose identities/hashes still match.

After the discovery commit, acquire a fresh rollback lease and use a new `git revert`; do not reset or rewrite. Restore the prior hook configuration, then remove only exact still-matching control/config artifacts. If identity, hash, ownership, mode, or production state differs, stop for review. No rollback step deletes, prunes, repairs, or cleans a registered worktree.
