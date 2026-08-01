# Production change ownership

This repository uses the central change-lease registry. Conversational memory, branch names, and worktree age are not proof of ownership.

Before reading or changing implementation state, identify the task and worktree with the standard interface on seebx:

`/usr/bin/python3.12 /var/lib/chat-memory-change-leases-v1/control/bin/chat_memory_lease.py --config /etc/chat-memory/change-leases-v1.json discover --task-id "${CODEX_TASK_ID:-$CODEX_THREAD_ID}" --thread-id "$CODEX_THREAD_ID" --worktree "$PWD"`

Before any file edit, acquire a lease for the exact registered worktree, branch, path scopes, and change types. Export the returned lease ID as `CHAT_MEMORY_LEASE_ID`. Renew before expiry and release after the bounded task. A lease never authorizes deployment, restart, database/Qdrant work, publication, deletion, or cleanup unless the lease includes that exact change type and the user separately authorized the action.

The shared absolute commit and push hooks deny by default when the lease or runtime thread identity is missing, expired, mismatched, drifted, or out of scope. Codex currently exposes `CODEX_THREAD_ID`; when no distinct `CODEX_TASK_ID` is provided, the adapter records that same runtime identity in both fields. The sanctioned manager deploy/handoff workflow must call `/var/lib/chat-memory-change-leases-v1/control/bin/chat_memory_lease_guard.py` before mutation. Legacy deploy/handoff scripts are not patched by this activation and must not be called directly.

A lease is bound to the worktree's starting HEAD. After one commit, release that lease and acquire a fresh lease before another commit, push, deploy, or handoff. There is no automatic checkpoint transition in this version.

Existing worktrees are pending classification until the manager records an owner or confirms availability. Missing, dirty, detached, owner-mismatched, or old worktrees are not abandoned by inference. Only the manager may classify, recover an expired lease after its grace period, or revoke a live lease; every action is appended to the hash-chained audit.

This file is automatic discovery only in a worktree whose checked-out commit contains it. Existing chats and worktrees on older commits require the manager's one-time adoption message containing the exact task/thread/worktree coordinates and the standard command above.

All chats currently share Unix UID 1000, including the configured manager role. The role and audit are cooperative operational controls, not a malicious-actor security boundary. Stop on registry/control-install validation failure, ownership ambiguity, production drift, scope collision, path substitution, or any request for destructive production action not separately authorized.
