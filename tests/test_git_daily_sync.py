from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "git_daily_sync.py"
SPEC = importlib.util.spec_from_file_location("git_daily_sync", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
git_daily_sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(git_daily_sync)


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class GitDailySyncAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        root = Path(self.temporary.name)
        self.remote = root / "remote.git"
        self.repository = root / "repository"
        subprocess.run(
            ["git", "init", "--bare", str(self.remote)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "init", str(self.repository)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        git(self.repository, "config", "user.name", "Git Audit Test")
        git(self.repository, "config", "user.email", "git-audit@example.invalid")
        (self.repository / "tracked.txt").write_text("one\n", encoding="utf-8")
        git(self.repository, "add", "tracked.txt")
        git(self.repository, "commit", "-m", "initial")
        git(self.repository, "branch", "-M", "main")
        git(self.repository, "remote", "add", "origin", str(self.remote))
        git(self.repository, "push", "--set-upstream", "origin", "main")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def inspect(self) -> str:
        return git_daily_sync.inspect_mirror(
            repository=self.repository,
            ssh_config=Path(self.temporary.name) / "unused-ssh-config",
        )

    def test_clean_equal_branch_is_mirrored(self) -> None:
        result = self.inspect()
        self.assertIn("git_mirror_ok branch=main", result)
        self.assertIn(git(self.repository, "rev-parse", "HEAD"), result)

    def test_dirty_worktree_fails_closed(self) -> None:
        (self.repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(git_daily_sync.GitDriftError, "git_worktree_dirty"):
            self.inspect()

    def test_committed_but_unpublished_change_is_reported(self) -> None:
        (self.repository / "tracked.txt").write_text("two\n", encoding="utf-8")
        git(self.repository, "add", "tracked.txt")
        git(self.repository, "commit", "-m", "local ahead")
        with self.assertRaisesRegex(
            git_daily_sync.GitDriftError,
            r"git_unpublished_commits count=1 branch=main",
        ):
            self.inspect()

    def test_remote_head_not_present_locally_fails_closed(self) -> None:
        other = Path(self.temporary.name) / "other"
        git(
            Path(self.temporary.name),
            "clone",
            "--branch",
            "main",
            str(self.remote),
            str(other),
        )
        git(other, "config", "user.name", "Git Audit Test")
        git(other, "config", "user.email", "git-audit@example.invalid")
        (other / "remote.txt").write_text("remote\n", encoding="utf-8")
        git(other, "add", "remote.txt")
        git(other, "commit", "-m", "remote ahead")
        git(other, "push", "origin", "main")
        with self.assertRaisesRegex(
            git_daily_sync.GitDriftError,
            "git_remote_head_not_available_locally",
        ):
            self.inspect()


if __name__ == "__main__":
    unittest.main()
