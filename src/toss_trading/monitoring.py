"""Bounded worker telemetry, independent of trading state and notification events."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError

from toss_trading.domain import Session
from toss_trading.marketdata import NY, offline_session
from toss_trading.persistence import Store


class WorkerMonitor:
    def __init__(self, stores: list[Store]):
        self.stores = stores
        self.payload = {
            "started_at": datetime.now(UTC).isoformat(),
            "phase": "starting",
            "stopped_at": None,
            "last_calendar_success": None,
            "last_market_data_success": None,
            "last_error": None,
            "errors": [],
            "market_data_status": "unknown",
        }

    def phase(self, name: str) -> None:
        self.payload["phase"] = name

    def calendar(self, day, session: Session | None) -> None:
        self.payload.update(
            last_calendar_success=datetime.now(UTC).isoformat(),
            calendar_day=day.isoformat(),
            calendar_session=session.model_dump(mode="json") if session else None,
        )

    def finish_poll(self, errors: list[str], *, market_data: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.payload.update(
            phase="idle",
            errors=list(errors),
            market_data_status=market_data,
            last_poll_finished=now,
        )
        if errors:
            self.payload["last_error"] = {"time": now, "reasons": list(errors)}
        if market_data == "success":
            self.payload["last_market_data_success"] = now
        self.publish()

    def publish(self) -> None:
        self.payload["heartbeat_at"] = datetime.now(UTC).isoformat()
        for store in self.stores:
            try:
                store.write_worker_status(self.payload)
            except SQLAlchemyError:
                # Telemetry failure must not disable risk/exit processing.
                print("Worker heartbeat persistence failed")

    async def heartbeat(self, interval_seconds: float = 5) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            self.publish()

    def stopped(self) -> None:
        self.payload.update(phase="stopped", stopped_at=datetime.now(UTC).isoformat())
        self.publish()


def worker_view(payload: dict | None, now: datetime, poll_seconds: int) -> dict:
    result = dict(payload or {})
    heartbeat = result.get("heartbeat_at")
    age = (now - datetime.fromisoformat(heartbeat)).total_seconds() if heartbeat else None
    result["status"] = (
        "unknown"
        if age is None
        else "stopped"
        if result.get("stopped_at")
        else "running"
        if 0 <= age <= max(30, poll_seconds * 3)
        else "delayed"
    )
    result["heartbeat_age_seconds"] = age
    return result


def market_view(now: datetime, worker: dict) -> dict:
    """Broker's current-day calendar when available, otherwise labelled local estimates."""
    day = now.astimezone(NY).date()
    confirmed = worker.get("calendar_day") == day.isoformat()
    if confirmed:
        saved = worker.get("calendar_session")
        session = Session.model_validate(saved) if saved else None
    else:
        session = offline_session(day)
    next_open = session.open if session and session.open > now else None
    next_source = "broker" if confirmed and next_open else "local_estimate"
    if next_open is None:
        for offset in range(1, 15):
            future = offline_session(day + timedelta(days=offset))
            if future:
                next_open = future.open
                break
    return {
        "is_open": bool(session and session.contains(now)),
        "source": "broker" if confirmed else "local_estimate",
        "next_open": next_open,
        "next_open_source": next_source,
        "close": session.close if session and session.contains(now) else None,
        "session_day": day.isoformat(),
    }
