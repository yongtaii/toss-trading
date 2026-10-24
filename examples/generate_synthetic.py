"""Generate deterministic ENGINE TEST DATA; these prices are NOT market history."""

import csv
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from toss_trading.marketdata import offline_session


def generate(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "end", "open", "high", "low", "close", "volume"])
        for day in (date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)):
            session = offline_session(day)
            for minute in range(1, 391):
                for index, symbol in enumerate(("SOXL", "SOXS", "TQQQ", "SQQQ")):
                    base = Decimal(20 + index * 10)
                    # Trend, high-volume breakouts, then reversal to exercise exits.
                    phase = min(minute, 250)
                    price = base + Decimal(phase) * Decimal("0.005")
                    if minute > 250:
                        price -= Decimal(minute - 250) * Decimal("0.008")
                    volume = 10000 if (minute - 1) // 5 in (22, 30, 40) else 1000
                    writer.writerow(
                        [
                            symbol,
                            (session.open + timedelta(minutes=minute)).isoformat(),
                            price,
                            price + Decimal("0.02"),
                            price - Decimal("0.04"),
                            price + Decimal("0.01"),
                            volume,
                        ]
                    )


if __name__ == "__main__":
    generate(Path("data/synthetic.csv"))
