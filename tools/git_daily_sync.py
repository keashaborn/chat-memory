#!/usr/bin/env python3
"""Read-only audit of the live repository's GitHub mirror state."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from collections.abc import Mapping, Sequence


REPO = Path("/opt/chat-memory")
SSH_CONFIG = Path("/opt/chat-memory/.ssh/config")
GIT = "/usr/bin/git"


class GitDriftError(RuntimeError):
    """Fail-closed Git mirror finding suitable for systemd/journald."""


def _environment(ssh_config: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_SSH_COMMAND": f"ssh -F {ssh_config}",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C",
            "LC_ALL": "C",
        }
    )
    return environment


def _git(
    repository: Path,
    arguments: Sequence[str],
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [GIT, "--no-optional-locks", "--no-pager", "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        check=False,
        timeout=60,
        env=dict(environment),
    )


def _required_output(
    result: subprocess.CompletedProcess[str],
    failure: str,
) -> str:
    if result.returncode != 0:
        raise GitDriftError(failure)
    value = result.stdout.strip()
    if not value or "\n" in value or "\r" in value:
        raise GitDriftError(failure)
    return value


def inspect_mirror(
    repository: Path = REPO,
    ssh_config: Path = SSH_CONFIG,
) -> str:
    repository = repository.resolve(strict=True)
    environment = _environment(ssh_config)

    status = _git(
        repository,
        ["status", "--porcelain=v1", "--untracked-files=all"],
        environment,
    )
    if status.returncode != 0:
        raise GitDriftError("git_status_unavailable")
    if status.stdout:
        raise GitDriftError("git_worktree_dirty")

    local_head = _required_output(
        _git(repository, ["rev-parse", "--verify", "HEAD"], environment),
        "git_head_unavailable",
    )
    branch = _required_output(
        _git(repository, ["symbolic-ref", "--quiet", "--short", "HEAD"], environment),
        "git_branch_unavailable",
    )
    upstream = _required_output(
        _git(
            repository,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            environment,
        ),
        "git_upstream_unavailable",
    )
    if "/" not in upstream:
        raise GitDriftError("git_upstream_invalid")
    remote, remote_branch = upstream.split("/", 1)
    if not remote or not remote_branch or remote_branch != branch:
        raise GitDriftError("git_upstream_invalid")

    remote_result = _git(
        repository,
        ["ls-remote", "--exit-code", "--heads", remote, f"refs/heads/{remote_branch}"],
        environment,
    )
    if remote_result.returncode != 0:
        raise GitDriftError("git_remote_unavailable")
    remote_lines = [line.split() for line in remote_result.stdout.splitlines() if line]
    if len(remote_lines) != 1 or len(remote_lines[0]) != 2:
        raise GitDriftError("git_remote_response_invalid")
    remote_head, remote_ref = remote_lines[0]
    if remote_ref != f"refs/heads/{remote_branch}" or len(remote_head) != 40:
        raise GitDriftError("git_remote_response_invalid")

    if local_head == remote_head:
        return f"git_mirror_ok branch={branch} head={local_head}"

    remote_object = _git(
        repository,
        ["cat-file", "-e", f"{remote_head}^{{commit}}"],
        environment,
    )
    if remote_object.returncode != 0:
        raise GitDriftError("git_remote_head_not_available_locally")

    remote_is_ancestor = _git(
        repository,
        ["merge-base", "--is-ancestor", remote_head, local_head],
        environment,
    )
    if remote_is_ancestor.returncode == 0:
        count = _required_output(
            _git(repository, ["rev-list", "--count", f"{remote_head}..{local_head}"], environment),
            "git_ahead_count_unavailable",
        )
        if not count.isdigit() or int(count) < 1:
            raise GitDriftError("git_ahead_count_invalid")
        raise GitDriftError(f"git_unpublished_commits count={count} branch={branch}")
    if remote_is_ancestor.returncode not in (0, 1):
        raise GitDriftError("git_history_comparison_failed")

    local_is_ancestor = _git(
        repository,
        ["merge-base", "--is-ancestor", local_head, remote_head],
        environment,
    )
    if local_is_ancestor.returncode == 0:
        raise GitDriftError("git_local_branch_behind_remote")
    if local_is_ancestor.returncode != 1:
        raise GitDriftError("git_history_comparison_failed")
    raise GitDriftError("git_history_diverged")


def main() -> int:
    try:
        print(inspect_mirror())
    except (GitDriftError, OSError, subprocess.SubprocessError) as error:
        print(f"git_mirror_alert: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
