from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER = (
    ROOT / "scripts" / "memory_v1_queue_reconciliation_v1.py"
).read_text()
MIGRATION = (
    ROOT
    / "ops"
    / "sql"
    / "20260730_memory_v1_queue_reconciliation_v1.sql"
).read_text()


def test_worker_is_private_and_owner_scoped() -> None:
    assert "resolve_authenticated_owners" in WORKER
    assert "set_config('app.user_id',$1,true)" in WORKER
    assert "external_model_calls" in WORKER
    assert '"external_model_calls": 0' in WORKER
    assert '"qdrant_writes": 0' in WORKER
    assert '"prompt_influence": 0' in WORKER
    assert "openai" not in WORKER.lower()
    assert "requests." not in WORKER
    assert "httpx." not in WORKER


def test_apply_is_plan_locked_and_replay_checked() -> None:
    assert "--expected-plan-sha256" in WORKER
    assert "MEMORY_V1_QUEUE_RECONCILIATION_APPLY" in WORKER
    assert WORKER.count("apply_outcome\"] != \"replayed\"") == 2
    assert "transaction(isolation=\"serializable\")" in WORKER


def test_migration_is_fail_closed_and_append_only() -> None:
    assert "requires brains_app" in MIGRATION
    assert "FORCE ROW LEVEL SECURITY" in MIGRATION
    assert "guard_v5_local_inference_append_only" in MIGRATION
    assert "superseded_by_terminal_source_envelope" in MIGRATION
    assert "plan_owner_v5_local_orphan_v1" in MIGRATION
    assert "GRANT EXECUTE" in MIGRATION
    assert "TO brains_app" in MIGRATION
