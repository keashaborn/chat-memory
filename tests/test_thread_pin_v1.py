from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
ADAPTER_SOURCE = (
    ROOT / "seebx/adapters/conversation_threads.py"
).read_text(encoding="utf-8")
MIGRATION = (
    ROOT / "ops/sql/20260725_thread_pin_v1.sql"
).read_text(encoding="utf-8")


def test_pin_contract_is_owner_scoped():
    assert "class PinThreadReq(BaseModel):" in APP_SOURCE
    assert '@app.post("/threads/{thread_id}/pin")' in APP_SOURCE
    assert "_require_actor_for_thread(req, tid)" in APP_SOURCE
    assert "WHERE owner_user_id=$2 AND id=$3" in ADAPTER_SOURCE


def test_pin_does_not_change_conversation_recency():
    pin_handler = APP_SOURCE.split('@app.post("/threads/{thread_id}/pin")', 1)[1]
    pin_handler = pin_handler.split('@app.post("/threads/{thread_id}/auto-title")', 1)[0]
    assert "set_thread_pinned(" in pin_handler
    assert "SET pinned_at = CASE WHEN $1 THEN now() ELSE NULL END" in ADAPTER_SOURCE
    pin_query = ADAPTER_SOURCE.split("SET_THREAD_PINNED_SQL", 1)[1].split(
        '"""', 2
    )[1]
    assert "updated_at=now()" not in pin_query


def test_list_returns_pin_state_and_orders_pins_first():
    assert "pinned_at IS NOT NULL AS pinned" in ADAPTER_SOURCE
    assert "ORDER BY (pinned_at IS NOT NULL) DESC" in ADAPTER_SOURCE
    assert "list_visible_threads(" in APP_SOURCE
    assert '"pinned": bool(r["pinned"])' in APP_SOURCE


def test_migration_is_additive_and_indexes_owner_visible_order():
    assert "ADD COLUMN IF NOT EXISTS pinned_at timestamp with time zone" in MIGRATION
    assert "threads_owner_visible_pin_order_idx" in MIGRATION
    assert "owner_user_id" in MIGRATION
    assert "WHERE archived = false" in MIGRATION
