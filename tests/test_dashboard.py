import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toss_trading.api import create_app
from toss_trading.config import Settings
from toss_trading.domain import Order, Position
from toss_trading.live.control import write_control
from toss_trading.persistence import Store, ledger

PASSWORD = "dashboard-test-password-only"
CONFIG = Path(__file__).parents[1] / "config" / "strategy.toml"


@pytest.fixture
def live_store(tmp_path, config):
    live = Store(f"sqlite:///{tmp_path}/live.db", config.model_copy(update={"mode": "LIVE"}))
    yield live
    live.close()


def dashboard_settings(store, **kwargs):
    return Settings(
        _env_file=None,
        dashboard_password=PASSWORD,
        paper_database_url=str(store.engine.url),
        strategy_config_path=str(CONFIG),
        **kwargs,
    )


def live_dashboard_settings(live_store, **kwargs):
    return Settings(
        _env_file=None,
        dashboard_password=PASSWORD,
        dashboard_live_status_dir=str(Path(live_store.engine.url.database).parent),
        **kwargs,
    )


@pytest.mark.parametrize("path", ["/dashboard/", "/dashboard/data", "/dashboard/app.js"])
def test_dashboard_requires_separate_credentials(live_store, path):
    settings = live_dashboard_settings(
        live_store, operations_api_key="operations-key-32-characters-long"
    )
    with TestClient(create_app(settings)) as client:
        assert client.get(path).status_code == 401
        assert client.get(path, auth=("admin", "wrong-password")).status_code == 401
        assert (
            client.get(path, headers={"x-api-key": "operations-key-32-characters-long"}).status_code
            == 401
        )
        response = client.get(path, auth=("admin", PASSWORD))
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert PASSWORD not in response.text
        assert client.get("/v1/accounts", auth=("admin", PASSWORD)).status_code == 401


def test_dashboard_metrics_pending_orders_and_no_broker_calls(live_store):
    now = datetime.now(UTC)
    rev, state = live_store.load()
    state.cash = Decimal("8999")
    state.last_event = now
    state.live_verified_at = now
    state.reconciled = True
    state.live_buying_power = Decimal("12000")
    state.marks = {"SOXL": Decimal("110")}
    state.mark_times = {"SOXL": now}
    state.positions["SOXL"] = Position(
        symbol="SOXL",
        quantity=10,
        entry=100,
        entry_fees=1,
        stop=90,
        target=130,
        opened=now,
        initial_risk=100,
    )
    order = Order(
        id="pending",
        symbol="TQQQ",
        side="BUY",
        quantity=2,
        submitted=now,
        limit=50,
        atr=2,
        reason="ENTRY",
        reserved=101,
    )
    state.orders[order.id] = order
    live_store.commit(
        rev,
        state,
        [{"event": "BUY_FILL", "symbol": "SOXL", "quantity": "10", "price": "100", "time": now}]
        + [{"event": "LIVE_ACCOUNT_VERIFIED"}] * 110,
    )
    with TestClient(create_app(live_dashboard_settings(live_store))) as client:
        data = client.get("/dashboard/data", auth=("admin", PASSWORD)).json()
        assert data["summary"]["equity"] == "10099"
        assert data["summary"]["unrealized"] == "99"
        assert data["summary"]["total_pnl"] == "99"
        assert data["summary"]["reserved"] == "101"
        assert data["positions"][0]["unrealized"] == "99"
        assert data["engine_status"] == "recent"
        assert data["orders"][0]["id"] == "pending"
        assert data["events"][0]["event"] == "BUY_FILL"
        assert data["broker_buying_power"] == "12000"
        assert data["reconciled"] is True
        assert "bars" not in data
        assert data["mode"] == "LIVE"
    # Read requests must not mutate the ledger.
    assert live_store.load()[0] == rev + 1


def test_dashboard_uninitialized_and_delayed_state(live_store):
    settings = live_dashboard_settings(live_store)
    with TestClient(create_app(settings)) as client:
        assert (
            client.get("/dashboard/data", auth=("admin", PASSWORD)).json()["engine_status"]
            == "unknown"
        )
        rev, state = live_store.load()
        state.last_event = datetime.now(UTC) - timedelta(hours=1)
        live_store.commit(rev, state, [])
        assert (
            client.get("/dashboard/data", auth=("admin", PASSWORD)).json()["engine_status"]
            == "delayed"
        )


def test_dashboard_rejects_paper_ledger_even_at_live_path(config, tmp_path):
    paper = Store(f"sqlite:///{tmp_path}/live.db", config)
    try:
        with TestClient(create_app(live_dashboard_settings(paper))) as client:
            assert client.get("/dashboard/data", auth=("admin", PASSWORD)).status_code == 503
    finally:
        paper.close()


def test_dashboard_missing_ledger_fails_without_creating_tables(tmp_path):
    settings = Settings(
        _env_file=None,
        dashboard_password=PASSWORD,
        dashboard_live_status_dir=str(tmp_path),
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/dashboard/data", auth=("admin", PASSWORD))
        assert response.status_code == 503
        assert str(tmp_path) not in response.text
        assert not (tmp_path / "live.db").exists()


def test_dashboard_reads_ledger_created_before_additive_config_fields(live_store):
    old_config = json.loads(live_store.config_json)
    del old_config["trail_atr"]
    with live_store.engine.begin() as conn:
        conn.execute(ledger.update().where(ledger.c.id == 1).values(config=json.dumps(old_config)))

    with pytest.raises(ValueError, match="Configuration differs"):
        live_store.load()
    with TestClient(create_app(live_dashboard_settings(live_store))) as client:
        assert client.get("/dashboard/data", auth=("admin", PASSWORD)).status_code == 200


def test_dashboard_password_missing_is_disabled(store):
    settings = dashboard_settings(store)
    settings.dashboard_password = None
    with TestClient(create_app(settings)) as client:
        assert client.get("/dashboard/", auth=("admin", PASSWORD)).status_code == 503


def test_live_badge_requires_fresh_authenticated_worker_state(live_store, tmp_path):
    live = live_store
    try:
        now = datetime.now(UTC)
        live.write_worker_status({"heartbeat_at": now.isoformat()})
        write_control(tmp_path / "live-control.json", "run")
        settings = live_dashboard_settings(live_store)
        with TestClient(create_app(settings)) as client:
            path = "/dashboard/live-status"
            assert client.get(path).status_code == 401
            assert client.get(path, auth=("admin", PASSWORD)).json() == {
                "worker": "running",
                "control": "run",
            }
            live.write_worker_status({"heartbeat_at": (now - timedelta(minutes=2)).isoformat()})
            assert client.get(path, auth=("admin", PASSWORD)).json()["worker"] == "delayed"
    finally:
        live.close()
