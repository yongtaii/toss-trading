"""Raw historical price requests, paging consistency and durable provenance."""

import hashlib
import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_broker import make_broker
from test_trading_integration import candle

from toss_trading import download as module
from toss_trading.marketdata import fetch_interval, fetch_minutes


async def test_broker_raw_prices_are_explicit_and_default_stays_adjusted(settings, monkeypatch):
    requests = []

    def respond(req):
        requests.append(req)
        return httpx.Response(200, json={"result": {"candles": []}})

    broker = make_broker(settings, respond, monkeypatch)
    broker._token, broker._expires = "mock-token", float("inf")
    try:
        await broker.candles("SOXS", adjusted=False)
        await broker.candles("SOXS")
        assert [r.url.params["adjusted"] for r in requests] == ["false", "true"]
    finally:
        await broker.aclose()


async def test_raw_mode_is_forwarded_on_every_minute_page(session):
    one, two = [session.open + timedelta(minutes=i) for i in (1, 2)]
    broker = SimpleNamespace(
        candles=AsyncMock(
            side_effect=[
                {"candles": [candle(two)], "nextBefore": one.isoformat()},
                {"candles": [candle(one)], "nextBefore": None},
            ]
        )
    )
    rows = await fetch_minutes(broker, "SOXS", session, two, adjusted=False)
    assert [r.end for r in rows] == [one, two]
    assert all(call.kwargs["adjusted"] is False for call in broker.candles.call_args_list)


async def test_raw_daily_interval_preserves_requested_adjustment(session):
    broker = SimpleNamespace(
        candles=AsyncMock(
            return_value={
                "candles": [candle(session.close)],
                "nextBefore": None,
            }
        )
    )
    await fetch_interval(
        broker,
        "SOXS",
        date.fromisoformat(session.day),
        date.fromisoformat(session.day),
        "1d",
        adjusted=False,
    )
    assert broker.candles.call_args.kwargs["adjusted"] is False
    assert broker.candles.call_args.kwargs["interval"] == "1d"


async def test_downloader_records_raw_metadata_and_does_not_reissue_for_existing_file(
    tmp_path,
    monkeypatch,
    settings,
    session,
):
    end = session.open + timedelta(minutes=1)
    broker = SimpleNamespace(
        market_calendar=AsyncMock(
            return_value={
                "today": {
                    "date": session.day,
                    "regularMarket": {
                        "startTime": session.open.isoformat(),
                        "endTime": session.close.isoformat(),
                    },
                }
            }
        ),
        candles=AsyncMock(return_value={"candles": [candle(end)], "nextBefore": None}),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(module, "Settings", lambda: settings)
    monkeypatch.setattr(module, "load_config", lambda _: SimpleNamespace(market_symbols=("SOXS",)))
    constructions = []
    monkeypatch.setattr(module, "TossQueries", lambda _: (constructions.append(1), broker)[1])
    output = tmp_path / "raw.csv"
    args = SimpleNamespace(
        start=date.fromisoformat(session.day),
        end=date.fromisoformat(session.day),
        interval="1m",
        config="unused",
        output=output,
        unadjusted=True,
    )
    await module.download(args)
    meta = json.loads(output.with_suffix(".meta.json").read_text())
    assert meta["adjusted"] is False
    assert meta["csv_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert meta["rows_by_symbol"] == {"SOXS": 1}
    broker.aclose.assert_awaited_once()
    with pytest.raises(FileExistsError):
        await module.download(args)
    assert constructions == [1]
