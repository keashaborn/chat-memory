from __future__ import annotations

import unittest
from pathlib import Path

from scripts.render_release_unit import ReleaseUnitError, render_release_unit


ROOT = Path(__file__).resolve().parents[1]
BACKEND_TEMPLATE = ROOT / "ops/systemd/brains-immutable-release.conf.in"
COMMIT = "a" * 40


class ReleaseUnitRendererTests(unittest.TestCase):
    def test_backend_template_renders_commit_addressed_source_and_runtime(self) -> None:
        rendered = render_release_unit(BACKEND_TEMPLATE.read_text(), COMMIT)
        self.assertNotIn("@COMMIT@", rendered)
        self.assertIn(f"WorkingDirectory=/opt/lifeswitch/releases/{COMMIT}", rendered)
        self.assertIn(f"/opt/lifeswitch/runtimes/{COMMIT}/venv/bin/uvicorn", rendered)
        self.assertIn(f"--app-dir /opt/lifeswitch/releases/{COMMIT}", rendered)
        self.assertIn("ExecStart=\n", rendered)
        self.assertIn("ExecStartPre=\n", rendered)
        self.assertIn("Environment=PYTHONDONTWRITEBYTECODE=1", rendered)

    def test_invalid_commit_fails_closed(self) -> None:
        with self.assertRaisesRegex(ReleaseUnitError, "commit_invalid"):
            render_release_unit("WorkingDirectory=/opt/lifeswitch/releases/@COMMIT@\n", "main")

    def test_missing_commit_placeholder_fails_closed(self) -> None:
        with self.assertRaisesRegex(ReleaseUnitError, "placeholder_missing"):
            render_release_unit("[Service]\n", COMMIT)

    def test_unknown_placeholder_fails_closed(self) -> None:
        with self.assertRaisesRegex(ReleaseUnitError, "unknown_placeholder"):
            render_release_unit(
                "WorkingDirectory=/opt/lifeswitch/releases/@COMMIT@/@OTHER@\n",
                COMMIT,
            )

    def test_template_must_end_with_newline(self) -> None:
        with self.assertRaisesRegex(ReleaseUnitError, "encoding_invalid"):
            render_release_unit(
                "WorkingDirectory=/opt/lifeswitch/releases/@COMMIT@",
                COMMIT,
            )


if __name__ == "__main__":
    unittest.main()
