import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from toss_trading.adapters.toss import BrokerError, ConfigurationError, TossQueries
from toss_trading.config import Settings
from toss_trading.persistence import Store, events
from toss_trading.ports import BrokerQueries
from toss_trading.trading_config import load_config


def create_app(settings: Settings | None = None, broker: BrokerQueries | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = TossQueries(settings) if broker is None else None
        app.state.broker = owned or broker
        try:
            yield
        finally:
            if owned:
                await owned.aclose()

    app = FastAPI(title="Toss Trading — Read Only", version="0.1.0", lifespan=lifespan)

    async def authorize(x_api_key: Annotated[str | None, Header()] = None) -> None:
        expected = settings.operations_api_key
        if not expected or not expected.get_secret_value():
            raise HTTPException(503, "OPERATIONS_API_KEY is not configured")
        if not x_api_key or not secrets.compare_digest(
            x_api_key.encode(),
            expected.get_secret_value().encode(),
        ):
            raise HTTPException(401, "Invalid API key")

    def queries(request: Request) -> BrokerQueries:
        return request.app.state.broker

    broker_dep = Annotated[BrokerQueries, Depends(queries)]
    auth = [Depends(authorize)]

    @app.exception_handler(ConfigurationError)
    async def configuration_error(request: Request, exc: ConfigurationError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(BrokerError)
    async def broker_error(request: Request, exc: BrokerError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "detail": "Broker query failed",
                "upstream_status": exc.status,
            },
        )

    @app.exception_handler(ValueError)
    async def invalid_input(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "Invalid query parameters"})

    @app.middleware("http")
    async def private_response(request: Request, call_next: Any):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "stage": 1, "live_trading_enabled": False}

    @app.get("/v1/prices", dependencies=auth)
    async def prices(broker: broker_dep, symbols: str = Query(min_length=1, max_length=6600)):
        return {"result": await broker.prices(symbols.split(","))}

    @app.get("/v1/accounts", dependencies=auth)
    async def accounts(broker: broker_dep):
        return {"result": await broker.accounts()}

    @app.get("/v1/holdings", dependencies=auth)
    async def holdings(broker: broker_dep, symbol: str | None = None):
        return {"result": await broker.holdings(symbol)}

    @app.get("/v1/buying-power", dependencies=auth)
    async def buying_power(broker: broker_dep):
        return {"result": await broker.buying_power()}

    @app.get("/v1/orders", dependencies=auth)
    async def orders(
        broker: broker_dep,
        status: Literal["OPEN", "CLOSED"] = "OPEN",
        symbol: str | None = None,
        cursor: str | None = Query(default=None, max_length=4096),
        limit: int = Query(default=20, ge=1, le=100),
        from_date: Annotated[date | None, Query(alias="from")] = None,
        to_date: Annotated[date | None, Query(alias="to")] = None,
    ):
        return {
            "result": await broker.orders(
                status=status,
                symbol=symbol,
                cursor=cursor,
                limit=limit,
                from_date=from_date,
                to_date=to_date,
            )
        }

    @app.get("/v1/orders/{order_id}", dependencies=auth)
    async def order(broker: broker_dep, order_id: str):
        return {"result": await broker.order(order_id)}

    def paper_store() -> Store:
        try:
            return Store(
                settings.paper_database_url.get_secret_value(),
                load_config(settings.strategy_config_path),
                initialize=False,
            )
        except (OSError, ValueError):
            raise HTTPException(503, "Paper configuration or ledger unavailable") from None

    @app.get("/v1/paper", dependencies=auth)
    def paper_state():
        store = paper_store()
        try:
            _, state = store.load()
            return {
                "mode": "PAPER",
                "equity": str(state.equity()),
                "state": state.model_dump(mode="json"),
            }
        except (SQLAlchemyError, ValueError):
            raise HTTPException(503, "Paper ledger unavailable or configuration mismatch") from None
        finally:
            store.close()

    @app.get("/v1/paper/events", dependencies=auth)
    def paper_events(limit: int = Query(default=50, ge=1, le=500)):
        store = paper_store()
        try:
            with store.engine.connect() as conn:
                rows = conn.execute(
                    select(events.c.payload).order_by(events.c.id.desc()).limit(limit)
                )
                return {"result": [json.loads(row[0]) for row in rows]}
        except SQLAlchemyError:
            raise HTTPException(503, "Paper ledger unavailable") from None
        finally:
            store.close()

    return app
