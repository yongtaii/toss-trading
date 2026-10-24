"""Transactional paper ledger, optimistic revision lock, and notification outbox."""

import json
from datetime import UTC, datetime

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, create_engine, select, update
from sqlalchemy.exc import IntegrityError

from toss_trading.domain import State
from toss_trading.trading_config import TradingConfig

metadata = MetaData()
ledger = Table(
    "paper_ledger",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("config", Text, nullable=False),
    Column("payload", Text, nullable=False),
)
events = Table(
    "trading_events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("payload", Text, nullable=False),
)
outbox = Table(
    "slack_outbox",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("payload", Text, nullable=False),
    Column("status", String(20), nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
)


class ConcurrentUpdate(RuntimeError):
    pass


class Store:
    def __init__(self, url: str, config: TradingConfig, *, initialize: bool = True):
        self.engine = create_engine(url, hide_parameters=True)
        self.config_json = config.model_dump_json()
        if not initialize:
            return
        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            if conn.execute(select(ledger.c.id)).first() is None:
                try:
                    with conn.begin_nested():
                        conn.execute(
                            ledger.insert().values(
                                id=1,
                                revision=0,
                                config=self.config_json,
                                payload=State(cash=config.initial_cash).model_dump_json(),
                            )
                        )
                except IntegrityError:
                    pass
        self.load()

    def load(self) -> tuple[int, State]:
        with self.engine.connect() as conn:
            row = conn.execute(select(ledger).where(ledger.c.id == 1)).mappings().one()
        if row["config"] != self.config_json:
            raise ValueError(
                "Configuration differs from ledger; use a new ledger or reviewed migration"
            )
        return row["revision"], State.model_validate_json(row["payload"])

    def commit(self, revision: int, state: State, records: list[dict]) -> None:
        with self.engine.begin() as conn:
            changed = conn.execute(
                update(ledger)
                .where(ledger.c.id == 1, ledger.c.revision == revision)
                .values(revision=revision + 1, payload=state.model_dump_json())
            )
            if changed.rowcount != 1:
                raise ConcurrentUpdate(
                    "Concurrent worker: state was not committed; reload before retry"
                )
            for record in records:
                payload = json.dumps(record, default=str, sort_keys=True)
                conn.execute(events.insert().values(payload=payload))
                if record.get("notify"):
                    conn.execute(
                        outbox.insert().values(payload=payload, status="PENDING", attempts=0)
                    )

    def record_error(self, reason: str) -> None:
        rev, state = self.load()
        self.commit(
            rev,
            state,
            [{"event": "API_ERROR", "time": datetime.now(UTC), "reason": reason, "notify": True}],
        )

    def close(self) -> None:
        self.engine.dispose()
