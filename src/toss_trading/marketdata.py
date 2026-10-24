from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars

from toss_trading.adapters.toss import TossQueries
from toss_trading.domain import Bar, Quote, Session

NY = ZoneInfo("America/New_York")


@lru_cache(maxsize=16)
def offline_session(day: date) -> Session | None:
    # Historical calendar only. Online decisions require the broker's current calendar.
    cal = exchange_calendars.get_calendar("XNYS", start=str(day.year - 1), end=str(day.year + 1))
    if not cal.is_session(day.isoformat()):
        return None
    return Session(
        day=day.isoformat(),
        open=cal.session_open(day.isoformat()).to_pydatetime(),
        close=cal.session_close(day.isoformat()).to_pydatetime(),
    )


def broker_session(payload: dict, day: date) -> Session | None:
    today = payload["today"]
    if today["date"] != day.isoformat():
        raise ValueError("Calendar does not match requested US local date")
    regular = today["regularMarket"]
    if regular is None:
        return None
    session = Session(day=today["date"], open=regular["startTime"], close=regular["endTime"])
    if session.open.astimezone(NY).date() != day:
        raise ValueError("Market session date mismatch")
    return session


def parse_quote(symbol: str, payload: dict) -> Quote:
    if payload["currency"] != "USD" or not payload.get("timestamp"):
        raise ValueError("USD timestamped orderbook required")
    bid = max(payload["bids"], key=lambda x: Decimal(x["price"]))
    ask = min(payload["asks"], key=lambda x: Decimal(x["price"]))
    return Quote(
        symbol=symbol,
        at=payload["timestamp"],
        bid=bid["price"],
        ask=ask["price"],
        bid_size=bid["volume"],
        ask_size=ask["volume"],
    )


def parse_candle(symbol: str, payload: dict) -> Bar:
    if payload["currency"] != "USD":
        raise ValueError("USD candle required")
    return Bar(
        symbol=symbol,
        end=payload["timestamp"],
        open=payload["openPrice"],
        high=payload["highPrice"],
        low=payload["lowPrice"],
        close=payload["closePrice"],
        volume=payload["volume"],
    )


async def fetch_minutes(
    broker: TossQueries, symbol: str, session: Session, through: datetime
) -> list[Bar]:
    """Walk the documented inclusive cursor; preserve gaps instead of fabricating bars."""
    before = min(through, session.close).isoformat()
    result: dict[datetime, Bar] = {}
    seen = set()
    for _ in range(10):  # A regular session needs at most two 200-bar pages without extended hours.
        if before in seen:
            raise ValueError("Candle pagination did not advance")
        seen.add(before)
        page = await broker.candles(symbol, before=before)
        items = [parse_candle(symbol, b) for b in page["candles"]]
        for bar in items:
            if session.open < bar.end <= min(through, session.close):
                if bar.end in result and result[bar.end] != bar:
                    raise ValueError("Conflicting candle revision")
                result[bar.end] = bar
        if not items or min(b.end for b in items) <= session.open:
            break
        cursor = page.get("nextBefore")
        if cursor is None:
            break
        # Official nextBefore is forwarded unchanged; deduplicate inclusive boundary.
        before = cursor
    else:
        raise ValueError("Session candle pagination exceeded safety bound")
    return sorted(result.values(), key=lambda b: b.end)


def minute_cutoff(now: datetime) -> datetime:
    # One second publication delay: do not treat the current forming candle as completed.
    return (now.astimezone(UTC) - timedelta(seconds=1)).replace(second=0, microsecond=0)
