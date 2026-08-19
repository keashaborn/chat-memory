from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
THREAD_ROUTES_SOURCE = (
    ROOT / "seebx/capabilities/conversation/thread_routes.py"
).read_text(encoding="utf-8")
ADAPTER_SOURCE = (
    ROOT / "seebx/adapters/conversation_threads.py"
).read_text(encoding="utf-8")
MIGRATION = (
    ROOT / "ops/sql/20260725_thread_pin_v1.sql"
).read_text(encoding="utf-8")


def test_pin_contract_is_owner_scoped():
    assert "class PinThreadReq(BaseModel):" in THREAD_ROUTES_SOURCE
    assert '@router.post("/threads/{thread_id}/pin")' in THREAD_ROUTES_SOURCE
    assert "_require_actor_for_thread(" in THREAD_ROUTES_SOURCE
    assert "req, tid, postgres" in THREAD_ROUTES_SOURCE
    assert "WHERE owner_user_id=$2 AND id=$3" in ADAPTER_SOURCE


def test_pin_does_not_change_conversation_recency():
    pin_handler = THREAD_ROUTES_SOURCE.split('@router.post("/threads/{thread_id}/pin")', 1)[1]
    pin_handler = pin_handler.split('@router.post("/threads/{thread_id}/auto-title")', 1)[0]
    assert "set_thread_pinned(" in pin_handler
    assert "SET pinned_at = CASE WHEN $1 THEN now() ELSE NULL END" in ADAPTER_SOURCE
    pin_query = ADAPTER_SOURCE.split("SET_THREAD_PINNED_SQL", 1)[1].split(
        '"""', 2
    )[1]
    assert "updated_at=now()" not in pin_query


def test_list_returns_pin_state_and_orders_pins_first():
    assert "pinned_at IS NOT NULL AS pinned" in ADAPTER_SOURCE
    assert "ORDER BY (pinned_at IS NOT NULL) DESC" in ADAPTER_SOURCE
    assert "list_visible_threads(" in THREAD_ROUTES_SOURCE
    assert '"pinned": bool(row["pinned"])' in THREAD_ROUTES_SOURCE


def test_migration_is_additive_and_indexes_owner_visible_order():
    assert "ADD COLUMN IF NOT EXISTS pinned_at timestamp with time zone" in MIGRATION
    assert "threads_owner_visible_pin_order_idx" in MIGRATION
    assert "owner_user_id" in MIGRATION
    assert "WHERE archived = false" in MIGRATION
