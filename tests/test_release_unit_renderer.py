from __future__ import annotations

import unittest
from pathlib import Path

from scripts.render_release_unit import ReleaseUnitError, render_release_unit


ROOT = Path(__file__).resolve().parents[1]
BACKEND_TEMPLATE = ROOT / "ops/systemd/brains-immutable-release.conf.in"
COMMIT = "a" * 40
ZEP_BINDINGS = {
    "ZEP_SYNC_MODE": "on",
    "ZEP_SYNC_OWNER_IDS": "",
    "ZEP_TIMEOUT_SECONDS": "2.5",
    "ZEP_PROMPT_MODE": "on",
    "ZEP_PROMPT_OWNER_IDS": "",
}


class ReleaseUnitRendererTests(unittest.TestCase):
    def test_backend_template_renders_commit_addressed_source_and_runtime(self) -> None:
        rendered = render_release_unit(BACKEND_TEMPLATE.read_text(), COMMIT, ZEP_BINDINGS)
        self.assertNotIn("@COMMIT@", rendered)
        self.assertIn(f"WorkingDirectory=/opt/lifeswitch/releases/{COMMIT}", rendered)
        self.assertIn(f"/opt/lifeswitch/runtimes/{COMMIT}/venv/bin/uvicorn", rendered)
        self.assertIn(f"--app-dir /opt/lifeswitch/releases/{COMMIT}", rendered)
        self.assertIn("ExecStart=\n", rendered)
        self.assertIn("ExecStartPre=\n", rendered)
        self.assertIn("Environment=PYTHONDONTWRITEBYTECODE=1", rendered)

    def test_backend_template_is_filesystem_and_privilege_hardened(self) -> None:
        rendered = render_release_unit(BACKEND_TEMPLATE.read_text(), COMMIT, ZEP_BINDINGS)
        for directive in (
            "UMask=0077",
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "PrivateDevices=yes",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "ProtectKernelTunables=yes",
            "ProtectKernelModules=yes",
            "ProtectKernelLogs=yes",
            "ProtectControlGroups=yes",
            "ProtectClock=yes",
            "ProtectHostname=yes",
            "LockPersonality=yes",
            "RestrictSUIDSGID=yes",
            "RestrictRealtime=yes",
            "RestrictNamespaces=yes",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "SystemCallArchitectures=native",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "StandardOutput=journal",
            "StandardError=journal",
        ):
            self.assertIn(f"{directive}\n", rendered)
        self.assertNotIn("ReadWritePaths=/opt/lifeswitch/releases", rendered)

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
