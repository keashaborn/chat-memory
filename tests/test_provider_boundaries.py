from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES = ROOT / "seebx" / "capabilities"


class ProviderBoundaryTests(unittest.TestCase):
    def test_capabilities_do_not_import_provider_sdks(self) -> None:
        forbidden_roots = {"openai", "zep_cloud"}
        violations: list[str] = []
        for path in sorted(CAPABILITIES.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {item.name.split(".", 1)[0] for item in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots = {node.module.split(".", 1)[0]}
                else:
                    continue
                blocked = sorted(roots & forbidden_roots)
                if blocked:
                    relative = path.relative_to(ROOT)
                    violations.append(f"{relative}:{node.lineno}:{blocked[0]}")
        self.assertEqual(violations, [])
if __name__ == "__main__":
    unittest.main()
