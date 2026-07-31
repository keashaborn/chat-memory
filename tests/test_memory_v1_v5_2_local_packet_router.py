from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock
import uuid


asyncpg_module = types.ModuleType("asyncpg")
asyncpg_module.connect = None
sys.modules.setdefault("asyncpg", asyncpg_module)
scripts_package = types.ModuleType("scripts")
scripts_package.__path__ = []
sys.modules.setdefault("scripts", scripts_package)
AUTHENTICATED_OWNERS_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "memory_v1_authenticated_owners.py"
)
AUTHENTICATED_OWNERS_SPEC = importlib.util.spec_from_file_location(
    "scripts.memory_v1_authenticated_owners",
    AUTHENTICATED_OWNERS_SCRIPT,
)
assert (
    AUTHENTICATED_OWNERS_SPEC is not None
    and AUTHENTICATED_OWNERS_SPEC.loader is not None
)
authenticated_owners_module = importlib.util.module_from_spec(
    AUTHENTICATED_OWNERS_SPEC
)
AUTHENTICATED_OWNERS_SPEC.loader.exec_module(authenticated_owners_module)
sys.modules.setdefault(
    "scripts.memory_v1_authenticated_owners",
    authenticated_owners_module,
)
disposition_module = types.ModuleType(
    "scripts.memory_v1_v5_local_packet_disposition"
)
disposition_module.loopback_dsn = lambda value: value
disposition_module.sha256_text = lambda value: hashlib.sha256(
    value.encode("utf-8")
).hexdigest()
disposition_module.stable_json = lambda value: json.dumps(
    value, sort_keys=True, separators=(",", ":")
)
sys.modules.setdefault(
    "scripts.memory_v1_v5_local_packet_disposition",
    disposition_module,
)

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "memory_v1_v5_2_local_packet_router.py"
)
SPEC = importlib.util.spec_from_file_location("v5_2_router", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Connection:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.fetch_query = ""
        self.fetch_args: tuple[object, ...] = ()

    def transaction(self, **_kwargs: object) -> _Transaction:
        return _Transaction()

    async def execute(self, *_args: object) -> None:
        return None

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.fetch_query = query
        self.fetch_args = args
        return [self.row]


class _FallbackConnection(_Connection):
    def __init__(self, row: dict[str, object]) -> None:
        super().__init__(row)
        self.fetch_queries: list[str] = []

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.fetch_queries.append(query)
        self.fetch_args = args
        if len(self.fetch_queries) == 1:
            return []
        return [self.row]


class _FinalizeConnection(_Connection):
    def __init__(self) -> None:
        super().__init__({})
        self.fetchrow_queries: list[str] = []

    async def fetchrow(self, query: str, *_args: object) -> dict[str, object]:
        self.fetchrow_queries.append(query)
        return {
            "apply_outcome": (
                "applied" if len(self.fetchrow_queries) == 1 else "replayed"
            )
        }


class V52LocalPacketRouterTest(unittest.TestCase):
    @mock.patch.object(MODULE.subprocess, "run")
    def test_repository_validation_is_independent_of_process_cwd(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = mock.Mock(returncode=0)

        self.assertTrue(MODULE.repository_commit_valid("a" * 40))
        run.assert_called_once_with(
            [
                "git",
                "-C",
                str(MODULE.REPOSITORY_ROOT),
                "merge-base",
                "--is-ancestor",
                "a" * 40,
                "HEAD",
            ],
            capture_output=True,
            check=False,
        )

    def test_authenticated_owner_rotation_prevents_fixed_uuid_priority(self) -> None:
        owners = [
            uuid.UUID("11111111-1111-4111-8111-111111111111"),
            uuid.UUID("22222222-2222-4222-8222-222222222222"),
            uuid.UUID("33333333-3333-4333-8333-333333333333"),
        ]
        self.assertEqual(MODULE.rotate_owners(owners, 0), owners)
        self.assertEqual(
            MODULE.rotate_owners(owners, 1),
            [owners[1], owners[2], owners[0]],
        )
        self.assertEqual(
            MODULE.rotate_owners(owners, 4),
            [owners[1], owners[2], owners[0]],
        )
        self.assertEqual(MODULE.rotate_owners([], 5), [])

    def test_router_uses_authenticated_owner_registry(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "await resolve_authenticated_owners(dsn, args.owner_user_id)",
            source,
        )
        self.assertNotIn("canonical_owners(", source)

    def test_exact_packet_selection_never_falls_back(self) -> None:
        wanted = uuid.UUID("8bf28952-67a5-4a11-8cab-718d451fca4c")
        other = uuid.UUID("3a4e8e8b-a4d2-4574-bcba-712e2fa6ac01")
        rows = [{"packet_id": other}, {"packet_id": wanted}]
        self.assertEqual(MODULE.select_plans(rows, wanted), [rows[1]])
        self.assertEqual(MODULE.select_plans(rows, uuid.uuid4()), [])
        self.assertEqual(MODULE.select_plans(rows, None), rows)

    def test_terminal_allowlist_is_closed(self) -> None:
        self.assertEqual(
            MODULE.TERMINAL_REASON_CODES,
            {
                "structured_domain",
                "question_only",
                "transient_state",
                "insufficient_evidence",
            },
        )
        self.assertNotIn("ambiguous_transcription", MODULE.TERMINAL_REASON_CODES)
        self.assertEqual(
            MODULE.REVIEW_UNRESOLVED_REQUIRED_CODES,
            {"entity_resolution_unresolved", "unregistered_predicate"},
        )

    def test_stable_ids_are_deterministic_and_route_bound(self) -> None:
        owner = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
        packet = uuid.UUID("8bf28952-67a5-4a11-8cab-718d451fca4c")
        first = MODULE.stable_ids(
            owner=owner,
            packet_id=packet,
            routing_basis_sha256="1" * 64,
        )
        second = MODULE.stable_ids(
            owner=owner,
            packet_id=packet,
            routing_basis_sha256="1" * 64,
        )
        changed = MODULE.stable_ids(
            owner=owner,
            packet_id=packet,
            routing_basis_sha256="2" * 64,
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    @mock.patch.object(MODULE, "repository_commit_valid", return_value=True)
    def test_v5_2_artifact_pair_validates(self, _: mock.Mock) -> None:
        owner = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
        packet = uuid.UUID("8bf28952-67a5-4a11-8cab-718d451fca4c")
        review_id = uuid.UUID("584aa45c-7483-4534-a388-a9ba4de0e0bd")
        request_id = uuid.UUID("df50c4f1-9d7f-46b5-80e6-c9e5719040d5")
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root).resolve()
            root.chmod(0o700)
            report_path = root / "review.json"
            bundle_path = root / "stage.json"
            report = {
                "contract_version": MODULE.REVIEW_CONTRACT,
                "mode": "owner_scoped_local_packet_review_zero_write",
                "owner_user_id": str(owner),
                "packet_id": str(packet),
                "review_id": str(review_id),
                "repository_commit": "a" * 40,
                "review_disposition": "manual_review_required",
                "derived_packet_sha256": "b" * 64,
                "resolution_packet_sha256": "c" * 64,
                "resolution_summary": {
                    "auto_link_eligible": 1,
                    "manual_review_required": 0,
                    "deferred": 0,
                    "rejected": 0,
                },
                "blocking_codes": [],
                "zero_write_proof": {
                    "database_writes": 0,
                    "qdrant_writes": 0,
                    "external_model_calls": 0,
                },
            }
            report_path.write_text(
                json.dumps(report, sort_keys=True) + "\n", encoding="utf-8"
            )
            report_path.chmod(0o600)
            report_sha = MODULE.sha256_file(report_path)
            bundle = {
                "contract_version": MODULE.BUNDLE_CONTRACT,
                "mode": "preflight_only_zero_write",
                "owner_user_id": str(owner),
                "case_id": f"local-packet-{packet}",
                "request_id": str(request_id),
                "database_writes": 0,
                "qdrant_writes": 0,
                "external_model_calls": 0,
                "authorized_stage": False,
                "source_report": {
                    "path": str(report_path),
                    "sha256": report_sha,
                },
                "extraction_packet_sha256": report["derived_packet_sha256"],
                "resolution_packet_sha256": report["resolution_packet_sha256"],
                "resolution_summary": report["resolution_summary"],
            }
            bundle_path.write_text(
                json.dumps(bundle, sort_keys=True) + "\n", encoding="utf-8"
            )
            bundle_path.chmod(0o600)
            self.assertEqual(
                stat.S_IMODE(report_path.stat().st_mode),
                0o600,
            )
            validated = MODULE.validate_artifacts(
                root=root,
                report_path=report_path,
                bundle_path=bundle_path,
                owner=owner,
                packet_id=packet,
            )
            self.assertEqual(validated["review_id"], review_id)
            self.assertEqual(validated["request_id"], request_id)
            self.assertEqual(validated["blocking_code_count"], 0)

    @mock.patch.object(MODULE, "repository_commit_valid", return_value=True)
    def test_legacy_contract_is_rejected(self, _: mock.Mock) -> None:
        owner = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
        packet = uuid.UUID("8bf28952-67a5-4a11-8cab-718d451fca4c")
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root).resolve()
            root.chmod(0o700)
            report_path = root / "review.json"
            bundle_path = root / "stage.json"
            report_path.write_text(
                json.dumps(
                    {
                        "contract_version": "memory_v1_v5_local_packet_review_v1"
                    }
                ),
                encoding="utf-8",
            )
            bundle_path.write_text("{}", encoding="utf-8")
            report_path.chmod(0o600)
            bundle_path.chmod(0o600)
            with self.assertRaisesRegex(
                RuntimeError, "V5.2 review report contract is invalid"
            ):
                MODULE.validate_artifacts(
                    root=root,
                    report_path=report_path,
                    bundle_path=bundle_path,
                    owner=owner,
                    packet_id=packet,
                )


class V52LocalPacketPlannerTest(unittest.IsolatedAsyncioTestCase):
    async def test_exact_packet_uses_unpaginated_owner_planner(self) -> None:
        packet = uuid.UUID("8bf28952-67a5-4a11-8cab-718d451fca4c")
        owner = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
        connection = _Connection({"packet_id": packet})

        result = await MODULE.plan_owner(connection, owner, packet)

        self.assertEqual(
            result,
            {
                "packet_id": packet,
                "planner_lane": MODULE.STANDARD_PLANNER_LANE,
            },
        )
        self.assertIn(
            "plan_owner_v5_2_exact_packet_route_v1",
            connection.fetch_query,
        )
        self.assertEqual(connection.fetch_args, (packet,))

    async def test_exact_packet_falls_back_only_to_exact_zero_atom_planner(
        self,
    ) -> None:
        packet = uuid.UUID("8bf28952-67a5-4a11-8cab-718d451fca4c")
        owner = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
        connection = _FallbackConnection({"packet_id": packet})

        result = await MODULE.plan_owner(connection, owner, packet)

        self.assertEqual(
            result,
            {
                "packet_id": packet,
                "planner_lane": MODULE.ZERO_ATOM_PLANNER_LANE,
            },
        )
        self.assertEqual(len(connection.fetch_queries), 2)
        self.assertIn(
            "plan_owner_v5_2_exact_packet_route_v1",
            connection.fetch_queries[0],
        )
        self.assertIn(
            "plan_owner_v5_2_zero_atom_deferral_route_v1",
            connection.fetch_queries[1],
        )
        self.assertEqual(connection.fetch_args, (packet,))

    async def test_unresolved_zero_atom_uses_restricted_finalizer(self) -> None:
        owner = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
        packet = uuid.UUID("8bf28952-67a5-4a11-8cab-718d451fca4c")
        connection = _FinalizeConnection()
        target = {
            "packet_id": packet,
            "planner_lane": MODULE.ZERO_ATOM_PLANNER_LANE,
            "packet_storage_sha256": "1" * 64,
            "routing_basis_sha256": "2" * 64,
            "reason_code": "deferral_only_review_unresolved_v5_2",
            "source_deferral_reason_codes": [
                "entity_resolution_unresolved"
            ],
        }

        applied, replayed = await MODULE.finalize_terminal(
            connection, owner, target
        )

        self.assertEqual(applied["apply_outcome"], "applied")
        self.assertEqual(replayed["apply_outcome"], "replayed")
        self.assertEqual(len(connection.fetchrow_queries), 2)
        self.assertTrue(
            all(
                "finalize_owner_v5_2_zero_atom_deferral_route_v1" in query
                for query in connection.fetchrow_queries
            )
        )

    async def test_terminal_zero_atom_uses_restricted_finalizer(self) -> None:
        owner = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
        packet = uuid.UUID("8bf28952-67a5-4a11-8cab-718d451fca4c")
        connection = _FinalizeConnection()
        target = {
            "packet_id": packet,
            "planner_lane": MODULE.ZERO_ATOM_PLANNER_LANE,
            "packet_storage_sha256": "1" * 64,
            "routing_basis_sha256": "2" * 64,
            "reason_code": "deferral_only_no_stage_v5_2",
            "source_deferral_reason_codes": [
                "insufficient_evidence",
                "structured_domain",
            ],
        }

        applied, replayed = await MODULE.finalize_terminal(
            connection, owner, target
        )

        self.assertEqual(applied["apply_outcome"], "applied")
        self.assertEqual(replayed["apply_outcome"], "replayed")
        self.assertTrue(
            all(
                "finalize_owner_v5_2_zero_atom_deferral_route_v1" in query
                for query in connection.fetchrow_queries
            )
        )

    async def test_standard_terminal_uses_standard_finalizer(self) -> None:
        owner = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
        packet = uuid.UUID("8bf28952-67a5-4a11-8cab-718d451fca4c")
        connection = _FinalizeConnection()
        target = {
            "packet_id": packet,
            "planner_lane": MODULE.STANDARD_PLANNER_LANE,
            "packet_storage_sha256": "1" * 64,
            "routing_basis_sha256": "2" * 64,
            "reason_code": "deferral_only_no_stage_v5_2",
            "source_deferral_reason_codes": ["insufficient_evidence"],
        }

        await MODULE.finalize_terminal(connection, owner, target)

        self.assertTrue(
            all(
                "finalize_owner_v5_2_terminal_route_v1" in query
                for query in connection.fetchrow_queries
            )
        )

    async def test_terminal_route_rejects_missing_planner_lane(self) -> None:
        owner = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
        packet = uuid.UUID("8bf28952-67a5-4a11-8cab-718d451fca4c")
        target = {
            "packet_id": packet,
            "packet_storage_sha256": "1" * 64,
            "routing_basis_sha256": "2" * 64,
            "reason_code": "deferral_only_no_stage_v5_2",
            "source_deferral_reason_codes": ["insufficient_evidence"],
        }

        with self.assertRaisesRegex(RuntimeError, "invalid route lane"):
            await MODULE.finalize_terminal(_FinalizeConnection(), owner, target)


if __name__ == "__main__":
    unittest.main()
