"""Authenticated, read-only dashboard for the LIVE strategy ledger."""

import json
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from toss_trading.config import Settings
from toss_trading.domain import State
from toss_trading.live.control import read_control
from toss_trading.monitoring import market_view, worker_view
from toss_trading.trading_config import TradingConfig

ASSETS = Path(__file__).with_name("static")


def live_database(directory: str | None) -> sqlite3.Connection:
    if not directory:
        raise ValueError("LIVE dashboard volume is not configured")
    uri = (Path(directory) / "live.db").resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=1)


def read_worker(connection):
    row = connection.execute("SELECT payload FROM paper_worker_status WHERE id = 1").fetchone()
    payload = json.loads(row[0]) if row else {}
    if not isinstance(payload, dict):
        raise ValueError("Invalid LIVE heartbeat")
    return payload


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

    @router.get("/live-status")
    def live_status():
        if not settings.dashboard_live_status_dir:
            return {"worker": "unconfigured", "control": None}
        directory = Path(settings.dashboard_live_status_dir)
        try:
            with closing(live_database(settings.dashboard_live_status_dir)) as connection:
                mode = connection.execute(
                    "SELECT json_extract(config, '$.mode') FROM paper_ledger WHERE id = 1"
                ).fetchone()
                if mode != ("LIVE",):
                    raise ValueError("A LIVE ledger is required")
                worker = worker_view(read_worker(connection), datetime.now(UTC), 5)
            return {
                "worker": worker["status"],
                "control": read_control(directory / "live-control.json"),
            }
        except (sqlite3.Error, ValueError, TypeError, OSError):
            return {"worker": "unavailable", "control": None}

    @router.get("/data")
    def data():
        try:
            with closing(live_database(settings.dashboard_live_status_dir)) as connection:
                connection.execute("BEGIN")
                row = connection.execute(
                    "SELECT config, payload FROM paper_ledger WHERE id = 1"
                ).fetchone()
                if row is None:
                    raise ValueError("LIVE ledger has no state")
                config = TradingConfig.model_validate_json(row[0])
                if config.mode != "LIVE":
                    raise ValueError("A LIVE ledger is required")
                state = State.model_validate_json(row[1])
                recent = [
                    json.loads(record[0])
                    for record in connection.execute(
                        """SELECT payload FROM trading_events
                           WHERE COALESCE(json_extract(payload, '$.event'), '')
                                 != 'LIVE_ACCOUNT_VERIFIED'
                           ORDER BY id DESC LIMIT 100"""
                    )
                ]
                worker_payload = read_worker(connection)
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
            # Marked value before exit costs.
            equity = state.equity()
            unrealized = sum(
                (
                    p.quantity * (state.marks.get(s, p.entry) - p.entry) - p.entry_fees
                    for s, p in state.positions.items()
                ),
                Decimal(0),
            )
            return {
                "mode": "LIVE",
                "reconciled": state.reconciled,
                "account_verified_at": state.live_verified_at,
                "broker_buying_power": str(state.live_buying_power)
                if state.live_verified_at
                else None,
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
                "orders": [
                    {
                        "id": o.id,
                        "symbol": o.symbol,
                        "side": o.side,
                        "quantity": o.quantity,
                        "filled": o.filled,
                        "limit": o.limit if o.side == "BUY" else None,
                        "status": o.status,
                        "submitted": o.submitted,
                    }
                    for o in sorted(state.orders.values(), key=lambda o: o.submitted, reverse=True)[
                        :100
                    ]
                ],
                "trades": state.trades,
                "events": recent,
            }
        except (OSError, ValueError, TypeError, sqlite3.Error):
            raise HTTPException(503, "LIVE 원장을 읽을 수 없습니다.") from None

    return router
