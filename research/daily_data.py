"""Shared original-price daily data and provenance utilities; no trading strategies."""

import csv
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from toss_trading.marketdata import NY, offline_session

ROOT = Path(__file__).resolve().parents[1]
TRADED = ("SOXL", "SOXS", "TQQQ", "SQQQ")
MARKET = (*TRADED, "QQQ", "SOXX")
MAPPING = {
    "SOXL": ("SOXX", 1, "SEMI"),
    "SOXS": ("SOXX", -1, "SEMI"),
    "TQQQ": ("QQQ", 1, "NASDAQ"),
    "SQQQ": ("QQQ", -1, "NASDAQ"),
}


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def merge(left, right):
    for day in left.keys() & right.keys():
        if left[day] != right[day]:
            raise ValueError(f"Conflicting original-price daily revision: {day}")
    return dict(sorted({**left, **right}.items()))


def read_daily(path):
    meta = json.loads(path.with_suffix(".meta.json").read_text())
    if (
        meta.get("adjusted") is not False
        or meta.get("interval") != "1d"
        or meta.get("csv_sha256") != digest(path)
    ):
        raise ValueError("Official original-price daily data required")
    data = {}
    with path.open() as handle:
        for r in csv.DictReader(handle):
            day = datetime.fromisoformat(r["end"]).astimezone(NY).date()
            if r["symbol"] in data.setdefault(day, {}):
                raise ValueError("Duplicate daily candle")
            data[day][r["symbol"]] = {"open": float(r["open"]), "close": float(r["close"])}
    cursor = min(data)
    while cursor <= max(data):
        if offline_session(cursor) and set(data.get(cursor, {})) != set(MARKET):
            raise ValueError(f"Missing daily data {cursor}")
        cursor += timedelta(days=1)
    return dict(sorted(data.items())), meta
