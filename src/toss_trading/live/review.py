"""Offline diagnosis of historical validation; never changes strategy parameters or sends orders."""

import argparse
import json
import shutil
import tarfile
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

RESULT_FILES = frozenset(
    {
        "history.csv",
        "paper-strategy.toml",
        "status.json",
        "summary.json",
        "training.json",
        "validation.json",
    }
)


def extract_results(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as handle:
        members = handle.getmembers()
        if (
            len(members) != len(RESULT_FILES)
            or {m.name for m in members} != RESULT_FILES
            or any(not m.isfile() or m.size > 100_000_000 for m in members)
        ):
            raise ValueError("Unexpected or unsafe validation archive")
        destination.mkdir(parents=True, exist_ok=False)
        for member in members:
            # Use fixed allowed basenames; do not trust tar paths, links or permissions.
            with (
                handle.extractfile(member) as source,
                (destination / member.name).open("xb") as target,
            ):
                shutil.copyfileobj(source, target)


def diagnosis(metrics: dict) -> dict:
    groups = defaultdict(lambda: {"sell_fills": 0, "net_pnl": Decimal(0)})
    for trade in metrics.get("trade_log", []):
        item = groups[trade["reason"]]
        item["sell_fills"] += 1
        item["net_pnl"] += Decimal(trade["pnl"])
    return {
        "return_percent": Decimal(metrics["total_return"]) * 100,
        "net_profit_usd": metrics["net_profit_after_fees"],
        "win_rate_percent": Decimal(metrics["win_rate"]) * 100,
        "profit_factor": metrics["profit_factor"],
        "trades": metrics["total_trades"],
        "mdd_percent": Decimal(metrics["mdd"]) * 100,
        "costs_reported": metrics.get("costs"),
        "by_symbol": metrics.get("by_symbol"),
        "by_entry_time_30m": metrics.get("by_entry_time_30m"),
        "by_exit_reason_sell_fills": dict(groups),
        "unclosed_positions": metrics.get("unclosed_positions"),
        "unresolved_orders": metrics.get("unresolved_orders"),
        "sample_warning": metrics.get("sample_warning"),
    }


def review(directory: Path) -> dict:
    summary = json.loads((directory / "summary.json").read_text())
    result = {
        "source": summary["source"],
        "requested_sessions": summary["requested_sessions"],
        "complete_sessions": summary["complete_sessions"],
        "excluded_sessions": summary["excluded_sessions"],
        "split_us_date": summary["split_us_date"],
        "initial_cash": summary["initial_cash"],
        "parameter_search": False,
        "live_activation_performed": False,
        "training": diagnosis(json.loads((directory / "training.json").read_text())),
        "validation": diagnosis(json.loads((directory / "validation.json").read_text())),
    }
    (directory / "loss-diagnosis.json").write_text(json.dumps(result, indent=2, default=str))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(review(args.directory), indent=2, default=str))


if __name__ == "__main__":
    main()
