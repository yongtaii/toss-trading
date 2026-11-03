"""Read-only connection preflight; never returns credentials or full account records."""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from toss_trading.adapters.toss import BrokerError, ConfigurationError, TossQueries
from toss_trading.config import Settings
from toss_trading.live.config import LiveConfig, load_live_config
from toss_trading.live.ledger import money, usd_positions
from toss_trading.marketdata import NY, broker_session, fetch_minutes, parse_quote, previous_session
from toss_trading.tls import ssl_context
from toss_trading.trading_config import load_config


async def diagnose(settings: Settings, live: LiveConfig | None = None) -> dict:
    c = live.strategy() if live else load_config()
    checks = {
        "credentials_present": bool(settings.toss_client_id and settings.toss_client_secret),
        "account_configured": settings.toss_account_seq is not None,
        "strategy_is_selected": False,  # Retained weekly strategies are offline only.
        "live_account_matches": live is None
        or live.account_seq in (None, settings.toss_account_seq),
    }
    report = {
        "at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "checks": checks,
        "ready_for_live": False,
    }
    # Probe publicly accessible HTTPS before issuing any account token.
    try:
        async with httpx.AsyncClient(
            timeout=10, trust_env=False, follow_redirects=False, verify=ssl_context()
        ) as client:
            response = await client.get(
                "https://openapi.tossinvest.com/openapi-docs/latest/openapi.json"
            )
            response.raise_for_status()
        checks["https_connectivity"] = True
    except httpx.HTTPError as exc:
        checks["https_connectivity"] = False
        report["connection_error"] = type(exc).__name__
        report["next_step"] = "Run preflight on the API-allowed host with outbound HTTPS/DNS access"
        return report
    if not (
        checks["credentials_present"]
        and checks["strategy_is_selected"]
        and checks["live_account_matches"]
    ):
        return report
    broker = TossQueries(settings)
    try:
        accounts = await broker.accounts()
        if settings.toss_account_seq is None:
            report["available_account_seqs"] = [a["accountSeq"] for a in accounts]
            report["next_step"] = "Set TOSS_ACCOUNT_SEQ to the selected accountSeq (not accountNo)"
            return report
        checks["account_exists"] = settings.toss_account_seq in {a["accountSeq"] for a in accounts}
        holdings = await broker.holdings()
        power = await broker.buying_power()
        opened = await broker.orders(status="OPEN")
        checks["usd_buying_power"] = power["currency"] == "USD"
        if live:
            checks["allocated_cash_available"] = (
                power["currency"] == "USD" and money(power["cashBuyingPower"]) >= live.capital_usd
            )
        actual = usd_positions(holdings)
        protected = live.protected_positions if live else {}
        report["initial_account_flat"] = not actual
        checks["initial_strategy_positions_flat"] = not (set(actual) & set(c.symbols))
        checks["protected_positions_match"] = all(
            actual.get(s) == quantity for s, quantity in protected.items()
        )
        checks["no_unknown_usd_positions"] = set(actual) <= set(protected) | set(c.symbols)
        report["existing_usd_positions"] = [
            {"symbol": item["symbol"], "quantity": str(money(item["quantity"]))}
            for item in holdings["items"]
            if item["currency"] == "USD" and money(item["quantity"]) != 0
        ]
        checks["no_open_orders"] = not opened["orders"] and not opened.get("hasNext")
        commissions = await broker.commissions()
        us_rates = [
            money(item["commissionRate"]) for item in commissions if item["marketCountry"] == "US"
        ]
        checks["commission_covered_by_config"] = bool(us_rates) and (
            c.fee_bps >= max(us_rates) * 10000
        )
        now = datetime.now(UTC)
        day = now.astimezone(NY).date()
        session = broker_session(await broker.market_calendar(day), day)
        checks["calendar_query"] = True
        historical = previous_session(day)
        if historical:
            for symbol in c.market_symbols:
                bars = await fetch_minutes(broker, symbol, historical, historical.close)
                expected = int((historical.close - historical.open).total_seconds() // 60)
                checks[f"{symbol}_history_complete"] = len(bars) == expected and all(
                    b.end == historical.open + timedelta(minutes=i) for i, b in enumerate(bars, 1)
                )
        report["regular_session_open"] = bool(session and session.contains(now))
        if report["regular_session_open"]:
            for symbol in c.market_symbols:
                q = parse_quote(symbol, await broker.orderbook(symbol))
                checks[f"{symbol}_quote_fresh"] = (
                    0 <= (datetime.now(UTC) - q.at).total_seconds() <= c.max_quote_age_seconds
                )
        report["read_only_preflight_passed"] = all(checks.values())
        report["next_step"] = (
            "Historical backtest and monitored PAPER evidence required before activation"
        )
    except (BrokerError, ConfigurationError, ValueError, KeyError, TypeError) as exc:
        report["query_error"] = type(exc).__name__
        if isinstance(exc, BrokerError):
            report["upstream_status"] = exc.status
    finally:
        await broker.aclose()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-config")
    parser.add_argument("--output", default="data/live-preflight.json")
    args = parser.parse_args()
    live = load_live_config(args.live_config) if args.live_config else None
    report = asyncio.run(diagnose(Settings(), live))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
