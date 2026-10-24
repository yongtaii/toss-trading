"""Authenticated, broker-independent PAPER dashboard. Never initializes a ledger."""

import json
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError

from toss_trading.config import Settings
from toss_trading.domain import State
from toss_trading.monitoring import market_view, worker_view
from toss_trading.persistence import Store, events, symbol_signals, worker_status
from toss_trading.persistence import ledger as ledger_table
from toss_trading.risk import entry_state_checks
from toss_trading.trading_config import TradingConfig, load_config

ASSETS = Path(__file__).with_name("static")


def symbol_view(
    symbol: str,
    state: State,
    config: TradingConfig,
    saved: dict,
    worker: dict,
    market: dict,
    now: datetime,
) -> dict:
    decision = saved.get("decision") or {}
    indicators = decision.get("indicators") or {}
    at = datetime.fromisoformat(decision["time"]) if decision.get("time") else None
    current_session = bool(
        at
        and state.session
        and state.session.open < at <= state.session.close
        and state.session.contains(now)
    )
    fresh = bool(
        current_session and 0 <= (now - at).total_seconds() <= 300 + config.max_bar_age_seconds
    )
    gates = entry_state_checks(state, symbol, now, state.session, config) if state.session else {}
    mark_at = state.mark_times.get(symbol)
    gates["fresh_symbol_quote"] = bool(
        mark_at and 0 <= (now - mark_at).total_seconds() <= config.max_quote_age_seconds
    )
    gates["worker_running"] = worker["status"] == "running"
    gates["market_data_ready"] = worker.get("market_data_status") == "success"
    threshold = None
    distance = None
    if indicators and config.strategy_name == "vwap_mean_revert":
        threshold = Decimal(indicators["vwap"]) - config.stretch_atr * Decimal(indicators["atr"])
        distance = max(Decimal(0), Decimal(indicators["close"]) - threshold)
    failed_checks = [key for key, ok in decision.get("checks", {}).items() if not ok]
    failed_gates = [key for key, ok in gates.items() if not ok]
    if not market["is_open"]:
        status = "closed"
    elif worker["status"] != "running":
        status = "blocked"
    elif not fresh:
        status = "warming_up" if not indicators else "stale"
    elif failed_gates:
        status = "blocked"
    elif failed_checks or decision.get("signal") != "BUY_SIGNAL":
        status = "waiting"
    else:
        status = "signal"
    return {
        "symbol": symbol,
        "status": status,
        "decision_at": at,
        "decision_fresh": fresh,
        "signal": decision.get("signal"),
        "indicators": indicators,
        "checks": decision.get("checks", {}),
        "gates": gates,
        "failed_checks": failed_checks,
        "failed_gates": failed_gates,
        "entry_below": str(threshold) if threshold is not None else None,
        "distance_to_entry": str(distance) if distance is not None else None,
        "cooldown_until": state.cooldowns.get(symbol),
        "last_entry": saved.get("entry"),
    }


def dashboard_router(settings: Settings) -> APIRouter:
    security = HTTPBasic()

    def authorize(credentials: Annotated[HTTPBasicCredentials, Depends(security)]) -> None:
        password = settings.dashboard_password
        if not password:
            raise HTTPException(503, "DASHBOARD_PASSWORD is not configured")
        valid_user = secrets.compare_digest(
            credentials.username.encode(), settings.dashboard_username.encode()
        )
        valid_password = secrets.compare_digest(
            credentials.password.encode(), password.get_secret_value().encode()
        )
        if not (valid_user and valid_password):
            raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Basic"})

    router = APIRouter(prefix="/dashboard", dependencies=[Depends(authorize)])

    @router.get("", include_in_schema=False)
    @router.get("/", include_in_schema=False)
    def index():
        return FileResponse(
            ASSETS / "index.html",
            headers={
                "Content-Security-Policy": "default-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; img-src 'self' data:",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/app.js", include_in_schema=False)
    def javascript():
        return FileResponse(ASSETS / "app.js", media_type="text/javascript")

    @router.get("/style.css", include_in_schema=False)
    def stylesheet():
        return FileResponse(ASSETS / "style.css", media_type="text/css")

    @router.get("/data")
    def data(ledger: Literal["primary", "compare"] = "primary"):
        compare = settings.compare_config_path is not None
        if ledger == "compare":
            if not compare:
                raise HTTPException(404, "No comparison PAPER strategy configured")
            path, url = settings.compare_config_path, settings.compare_database_url
        else:
            path, url = settings.strategy_config_path, settings.paper_database_url
        store = None
        try:
            config = load_config(path)
            store = Store(url.get_secret_value(), config, initialize=False)
            # SQLite needs an explicit BEGIN to keep both reads on the same snapshot.
            with store.engine.connect() as conn:
                if conn.dialect.name == "sqlite":
                    conn.exec_driver_sql("BEGIN")
                row = (
                    conn.execute(select(ledger_table).where(ledger_table.c.id == 1))
                    .mappings()
                    .one()
                )
                if row["config"] != store.config_json:
                    raise ValueError("Configuration mismatch")
                state = State.model_validate_json(row["payload"])
                recent = [
                    json.loads(r[0])
                    for r in conn.execute(
                        select(events.c.payload).order_by(events.c.id.desc()).limit(100)
                    )
                ]
                # These additive projections may not exist until an older worker restarts.
                inspector = inspect(conn)
                worker_payload = None
                saved_signals = {}
                if inspector.has_table(worker_status.name):
                    value = conn.execute(
                        select(worker_status.c.payload).where(worker_status.c.id == 1)
                    ).scalar_one_or_none()
                    worker_payload = json.loads(value) if value else None
                if inspector.has_table(symbol_signals.name):
                    saved_signals = {
                        r.symbol: json.loads(r.payload)
                        for r in conn.execute(select(symbol_signals))
                    }
                for record in recent:
                    symbol = record.get("symbol")
                    key = (
                        "decision"
                        if record.get("event") == "DECISION"
                        else "entry"
                        if (
                            record.get("event") in {"ENTRY_REJECTED", "ORDER_SUBMITTED"}
                            and record.get("side") == "BUY"
                        )
                        else None
                    )
                    if symbol and key:
                        saved_signals.setdefault(symbol, {}).setdefault(key, record)
            now = datetime.now(UTC)
            worker = worker_view(worker_payload, now, config.paper_poll_seconds)
            market = market_view(now, worker)
            age = (now - state.last_event).total_seconds() if state.last_event else None
            positions = []
            for symbol, p in state.positions.items():
                mark = state.marks.get(symbol)
                positions.append(
                    {
                        **p.model_dump(mode="json"),
                        "mark": str(mark) if mark is not None else None,
                        "mark_time": state.mark_times.get(symbol),
                        "unrealized": str(p.quantity * (mark - p.entry) - p.entry_fees)
                        if mark is not None
                        else None,
                    }
                )
            # The equity convention matches /v1/paper: marked value before exit costs.
            equity = state.equity()
            unrealized = sum(
                (
                    p.quantity * (state.marks.get(s, p.entry) - p.entry) - p.entry_fees
                    for s, p in state.positions.items()
                ),
                Decimal(0),
            )
            return {
                "mode": "PAPER",
                "ledger": ledger,
                "compare_available": compare,
                "strategy": config.strategy_name,
                "generated_at": now,
                "last_event": state.last_event,
                "engine_status": "unknown"
                if age is None
                else ("recent" if age <= max(180, config.paper_poll_seconds * 4) else "delayed"),
                "session": state.session.model_dump(mode="json") if state.session else None,
                "loss_halted": state.loss_halted,
                "worker": worker,
                "market": market,
                "entry_conditions": [
                    symbol_view(
                        symbol, state, config, saved_signals.get(symbol, {}), worker, market, now
                    )
                    for symbol in config.symbols
                ],
                "summary": {
                    "initial_cash": str(config.initial_cash),
                    "equity": str(equity),
                    "cash": str(state.cash),
                    "realized": str(state.realized),
                    "unrealized": str(unrealized),
                    "fees": str(state.fees),
                    "total_pnl": str(equity - config.initial_cash),
                    "return_pct": str((equity / config.initial_cash - 1) * 100),
                    "reserved": str(sum((o.reserved for o in state.pending()), Decimal(0))),
                },
                "symbols": [
                    {"symbol": s, "mark": state.marks.get(s), "mark_time": state.mark_times.get(s)}
                    for s in config.symbols
                ],
                "positions": positions,
                "orders": [o.model_dump(mode="json") for o in state.pending()],
                "trades": state.trades,
                "events": recent,
            }
        except (OSError, ValueError, SQLAlchemyError):
            raise HTTPException(503, "PAPER 원장이 없거나 설정이 일치하지 않습니다.") from None
        finally:
            if store is not None:
                store.close()

    return router
