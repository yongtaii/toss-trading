"""Download selected-symbol candles from the documented official REST API.

Minute candles keep the session-by-session walk. A longer interval, such as the
daily one needed by the swing candidate, pages over the whole range instead.
The interval token is supplied by the caller from the official documentation
and is never guessed here.
"""

import argparse
import asyncio
import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from toss_trading.adapters.toss import BrokerError, ConfigurationError, TossQueries
from toss_trading.config import Settings
from toss_trading.marketdata import broker_session, fetch_interval, fetch_minutes
from toss_trading.trading_config import load_config


async def download(args) -> None:
    settings = Settings()
    config = load_config(args.config)
    if args.start > args.end:
        raise ValueError("start must precede end")
    output = Path(args.output)
    metadata = output.with_suffix(".meta.json")
    if output.exists() or metadata.exists():
        raise FileExistsError("Preserve existing history; choose a new output path")
    adjusted = not args.unadjusted
    broker = TossQueries(settings)
    rows = []
    try:
        if args.interval == "1m":
            day = args.start
            while day <= args.end:
                session = broker_session(await broker.market_calendar(day), day)
                if session:
                    counts = {}
                    for symbol in config.market_symbols:
                        candles = await fetch_minutes(
                            broker, symbol, session, datetime.now(UTC), adjusted=adjusted
                        )
                        rows.extend(candles)
                        counts[symbol] = len(candles)
                    print(f"History {day}: {counts}", flush=True)
                day += timedelta(days=1)
        else:
            for symbol in config.market_symbols:
                rows.extend(
                    await fetch_interval(
                        broker, symbol, args.start, args.end, args.interval, adjusted=adjusted
                    )
                )
    finally:
        await broker.aclose()
    if not rows:
        raise ValueError("No historical bars returned; check API retention/access")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: an existing reviewed dataset is not silently overwritten.
    with output.open("x", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["symbol", "end", "open", "high", "low", "close", "volume"]
        )
        writer.writeheader()
        writer.writerows(
            b.model_dump(mode="json") for b in sorted(rows, key=lambda b: (b.end, b.symbol))
        )
    with metadata.open("x") as handle:
        json.dump(
            {
                "downloaded_at_utc": datetime.now(UTC).isoformat(),
                "source": "official_toss_candles",
                "start": args.start.isoformat(),
                "end": args.end.isoformat(),
                "interval": args.interval,
                "adjusted": adjusted,
                "symbols": config.market_symbols,
                "rows_by_symbol": dict(Counter(b.symbol for b in rows)),
                "csv_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            },
            handle,
            indent=2,
        )
    print(f"Downloaded {len(rows)} {args.interval} candles to {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--config", default="config/strategy.toml")
    parser.add_argument("--output", required=True)
    # Use the token from the official candle documentation, e.g. the daily one.
    parser.add_argument("--interval", default="1m")
    parser.add_argument(
        "--unadjusted",
        action="store_true",
        help="Request actual historical prices (adjusted=false); use for integer-share backtests",
    )
    args = parser.parse_args()
    try:
        asyncio.run(download(args))
    except BrokerError as exc:
        hints = {
            "tls_verification": "HTTPS 인증서 검증 실패: 운영체제의 신뢰 인증서를 확인하세요.",
            "dns": "API 호스트의 DNS 조회에 실패했습니다. 네트워크/DNS 설정을 확인하세요.",
            "timeout": "API 요청 시간이 초과됐습니다. 연결 상태를 확인하세요.",
            "connection": "API 서버 연결에 실패했습니다. 네트워크 접근을 확인하세요.",
            "proxy": "프록시 연결에 실패했습니다.",
        }
        hint = hints.get(exc.reason, "API 응답 상태 또는 연결 설정을 확인하세요.")
        parser.exit(1, f"다운로드 실패: {exc}\n{hint}\n")
    except ConfigurationError as exc:
        parser.exit(1, f"다운로드 설정 오류: {exc}\n")


if __name__ == "__main__":
    main()
