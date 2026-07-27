#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTIVATOR = (
    ROOT / "tools" / "memory_v1_v5_2_continuous_drain_activate.sh"
)


def require_once(text: str, value: str) -> None:
    assert text.count(value) == 1, value


def main() -> int:
    text = ACTIVATOR.read_text(encoding="utf-8")
    for required in (
        "MEMORY_V1_V5_2_CONTINUOUS_DRAIN_ACTIVATE",
        "memory_v1_v5_continuous_drain_clone_benchmark_v1",
        "retry_jobs_repaired==4",
        "phase=fresh_backup",
        "phase=zero_data_change_verification",
        'cmp -s "$before" "$after"',
        '[[ "$qdrant_after" == "$qdrant_before" ]]',
        "restore_timers",
        "restore_units",
        'systemctl start --no-block "$service"',
        "--max-jobs 100",
        "--max-attempts 2",
        "--rolling-window-seconds 3600",
        "--max-reserved-jobs 100",
        "--failure-threshold 10",
        "account_scope_single_owner:true",
        "external_model_calls:0",
        "prompt_policy_changed:false",
    ):
        assert required in text, required
    require_once(text, "owner=1240822d-ac9a-4096-95aa-e2b24d36ef50")
    upper = text.upper()
    for forbidden in (
        "ALTER TABLE",
        "CREATE TABLE",
        "DROP TABLE",
        "TRUNCATE ",
        "DELETE FROM MEMORY.",
        "UPDATE MEMORY.",
        "INSERT INTO MEMORY.",
    ):
        assert forbidden not in upper, forbidden
    print("memory_v1_v5_2_continuous_drain_activation_contract_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
