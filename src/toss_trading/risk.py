from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from toss_trading.domain import ZERO, Quote, Session, State
from toss_trading.trading_config import TradingConfig


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def quote_checks(q: Quote, now: datetime, c: TradingConfig) -> dict[str, bool]:
    age = (now - q.at).total_seconds()
    mid = (q.bid + q.ask) / 2
    spread = (q.ask - q.bid) / mid * 10000
    return {
        "fresh_quote": 0 <= age <= c.max_quote_age_seconds,
        "spread": spread <= c.max_spread_bps,
        "cost": spread / 2 + c.fee_bps + c.slippage_bps <= c.max_cost_bps,
    }


def size_entry(
    state: State,
    symbol: str,
    atr: Decimal,
    q: Quote,
    now: datetime,
    session: Session,
    c: TradingConfig,
) -> tuple[Decimal, dict[str, bool]]:
    equity = state.equity(c.fee_bps)
    pending = state.pending()
    slots = set(state.positions) | {o.symbol for o in pending if o.side == "BUY"}
    remaining = (session.close - now).total_seconds()
    checks = {
        "allowlisted": symbol in c.symbols and q.symbol == symbol,
        "reconciled": state.reconciled,
        "no_overnight_positions": "overnight_positions" not in state.alerts,
        "regular_session": session.contains(now),
        "entry_window": (now - session.open).total_seconds() >= c.entry_after_minutes * 60
        and remaining >= max(c.entry_before_close_minutes, c.no_buy_before_close_minutes) * 60,
        "not_held": symbol not in state.positions,
        "no_pending": not state.pending(symbol),
        "loss_limit": not state.loss_halted
        and state.day_equity > 0
        and equity > state.day_equity * (1 - c.daily_loss_fraction),
        "slots": len(slots) < c.max_positions,
        "entry_count": state.entries.get(symbol, 0) < c.max_entries,
        "cooldown": symbol not in state.cooldowns or now >= state.cooldowns[symbol],
        "atr": atr > 0 and q.ask > atr * c.stop_atr,
        "all_position_marks_fresh": all(
            s in state.mark_times
            and 0 <= (now - state.mark_times[s]).total_seconds() <= c.max_quote_age_seconds
            for s in state.positions
        ),
        **quote_checks(q, now, c),
    }
    if not all(checks.values()):
        return ZERO, checks
    price = q.ask * (1 + c.slippage_bps / 10000)
    cost = price * (1 + c.fee_bps / 10000)
    # Include estimated exit fees and adverse execution in the risk budget.
    risk_per_share = atr * c.stop_atr + price * (2 * c.fee_bps + c.slippage_bps) / 10000
    reserved = sum((o.reserved for o in pending if o.side == "BUY"), ZERO)
    cash = max(ZERO, state.cash - reserved)
    daily_remaining = max(ZERO, equity - state.day_equity * (1 - c.daily_loss_fraction))
    quantity = floor_step(
        min(
            equity * c.risk_fraction / risk_per_share,
            daily_remaining / risk_per_share,
            cash / cost,
            equity * c.symbol_fraction / cost,
            c.max_order_usd / cost,
        ),
        c.quantity_step,
    )
    checks["minimum_quantity"] = quantity >= c.quantity_step
    return max(ZERO, quantity), checks
