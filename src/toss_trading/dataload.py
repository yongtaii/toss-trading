"""Validate and normalize vendor-neutral one-minute CSVs into the backtest schema.

Input columns: timestamp, open, high, low, close, volume, and symbol unless --symbol
is given. Timestamps carrying an offset are honored; naive ones use --timezone.
No vendor API is called.
"""

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from toss_trading.domain import Bar
from toss_trading.marketdata import NY, offline_session

FULL_SESSION_MINUTES = 390


def load(
    path: str | Path,
    *,
    symbol: str | None = None,
    timezone: str = "America/New_York",
    timestamp_at: str = "end",
    split_gap_fraction: Decimal = Decimal("0.4"),
) -> tuple[list[Bar], dict]:
    """Return regular-session bars (minute-end timestamps) and a validation report."""
    if timestamp_at not in ("start", "end"):
        raise ValueError("timestamp_at must be 'start' or 'end'")
    zone = ZoneInfo(timezone)
    report = {
        "source": str(path),
        "timestamp_at": timestamp_at,
        "timezone_for_naive_timestamps": timezone,
        "rows": 0,
        "invalid_rows": [],
        "duplicates": 0,
        "conflicting_duplicates": 0,
        "out_of_order": 0,
        "outside_regular_session": 0,
        "missing_minutes": {},
        "early_close_sessions": [],
        "suspected_splits": [],
    }
    accepted: dict[tuple[str, datetime], Bar] = {}
    last: dict[str, datetime] = {}
    with Path(path).open(newline="") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            report["rows"] += 1
            try:
                stamp = datetime.fromisoformat(row["timestamp"].strip())
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=zone)
                end = stamp + timedelta(minutes=1) if timestamp_at == "start" else stamp
                bar = Bar(
                    symbol=(symbol or row["symbol"]).strip().upper(),
                    end=end,
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row["volume"],
                )
            except (KeyError, ValueError, ValidationError) as exc:
                report["invalid_rows"].append({"line": line, "error": str(exc).splitlines()[0]})
                continue
            if bar.symbol in last and bar.end < last[bar.symbol]:
                report["out_of_order"] += 1
            last[bar.symbol] = max(bar.end, last.get(bar.symbol, bar.end))
            key = (bar.symbol, bar.end)
            if key in accepted:
                report["duplicates"] += 1
                if accepted[key] != bar:
                    report["conflicting_duplicates"] += 1
                continue
            session = offline_session(bar.end.astimezone(NY).date())
            if session is None or not session.open < bar.end <= session.close:
                report["outside_regular_session"] += 1
                continue
            accepted[key] = bar
    by_day = defaultdict(list)
    for bar in accepted.values():
        by_day[(bar.end.astimezone(NY).date(), bar.symbol)].append(bar)
    early = set()
    for (day, sym), rows in sorted(by_day.items()):
        session = offline_session(day)
        expected = int((session.close - session.open).total_seconds() // 60)
        if expected < FULL_SESSION_MINUTES:
            early.add(day.isoformat())
        if len(rows) < expected:
            report["missing_minutes"].setdefault(day.isoformat(), {})[sym] = expected - len(rows)
    report["early_close_sessions"] = sorted(early)
    closes: dict[str, tuple[str, Decimal]] = {}
    for (day, sym), rows in sorted(by_day.items()):
        rows.sort(key=lambda b: b.end)
        if sym in closes:
            previous_day, previous_close = closes[sym]
            gap = rows[0].open / previous_close - 1
            if abs(gap) > split_gap_fraction:
                report["suspected_splits"].append(
                    {"symbol": sym, "day": day.isoformat(), "after": previous_day, "gap": gap}
                )
        closes[sym] = (day.isoformat(), rows[-1].close)
    report["accepted_bars"] = len(accepted)
    report["incomplete_sessions"] = sorted(report["missing_minutes"])
    report["warnings"] = [
        f"{name}: {count}"
        for name, count in (
            ("invalid rows", len(report["invalid_rows"])),
            ("duplicate rows dropped", report["duplicates"]),
            ("conflicting duplicates (first kept)", report["conflicting_duplicates"]),
            ("out-of-order rows (sorted)", report["out_of_order"]),
            ("outside regular session (separated)", report["outside_regular_session"]),
            ("sessions with missing minutes", len(report["incomplete_sessions"])),
            ("suspected splits (check adjustment)", len(report["suspected_splits"])),
        )
        if count
    ]
    return sorted(accepted.values(), key=lambda b: (b.end, b.symbol)), report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv")
    parser.add_argument("--symbol", help="Symbol for files without a symbol column")
    parser.add_argument("--timezone", default="America/New_York")
    parser.add_argument("--timestamp-at", choices=("start", "end"), default="end")
    parser.add_argument(
        "--drop-incomplete", action="store_true", help="Exclude sessions with missing minutes"
    )
    parser.add_argument("--output", required=True, help="Normalized backtest CSV")
    parser.add_argument("--report", help="Validation report JSON (default: <output>.report.json)")
    args = parser.parse_args()
    bars, report = load(
        args.csv, symbol=args.symbol, timezone=args.timezone, timestamp_at=args.timestamp_at
    )
    if args.drop_incomplete:
        dropped = set(report["incomplete_sessions"])
        bars = [b for b in bars if b.end.astimezone(NY).date().isoformat() not in dropped]
        report["dropped_sessions"] = sorted(dropped)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: an existing reviewed dataset is not silently overwritten.
    with output.open("x", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["symbol", "end", "open", "high", "low", "close", "volume"]
        )
        writer.writeheader()
        writer.writerows(b.model_dump(mode="json") for b in bars)
    report_path = Path(args.report or f"{output}.report.json")
    report_path.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False))
    print(f"Wrote {len(bars)} bars to {output}; report {report_path}")
    for warning in report["warnings"]:
        print("WARNING", warning)


if __name__ == "__main__":
    main()
