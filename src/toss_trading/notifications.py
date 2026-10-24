"""Explicitly invoked Slack outbox dispatcher. Not called by tests or backtests."""

import json

import httpx
from sqlalchemy import select, update

from toss_trading.persistence import Store, outbox


async def dispatch(store: Store, webhook: str, *, transport=None) -> int:
    url = httpx.URL(webhook)
    if (
        url.scheme != "https"
        or url.host != "hooks.slack.com"
        or not url.path.startswith("/services/")
    ):
        raise ValueError("Expected a Slack HTTPS incoming webhook")
    sent = 0
    with store.engine.connect() as conn:
        rows = (
            conn.execute(select(outbox).where(outbox.c.status == "PENDING").limit(100))
            .mappings()
            .all()
        )
    async with httpx.AsyncClient(
        timeout=10, transport=transport, follow_redirects=False, trust_env=False
    ) as client:
        for row in rows:
            # Claim before sending. Crash/timeout after POST is UNKNOWN, never blindly resent.
            with store.engine.begin() as conn:
                changed = conn.execute(
                    update(outbox)
                    .where(outbox.c.id == row["id"], outbox.c.status == "PENDING")
                    .values(status="SENDING", attempts=row["attempts"] + 1)
                )
                if changed.rowcount != 1:
                    continue
            record = json.loads(row["payload"])
            # Paper account only; never expose broker credentials/account identifiers.
            strategy = json.loads(store.config_json)["strategy_name"]
            text = f"[PAPER {strategy}] " + json.dumps(record, ensure_ascii=False)
            try:
                response = await client.post(webhook, json={"text": text})
                status = (
                    "SENT" if response.status_code == 200 and response.text == "ok" else "FAILED"
                )
            except httpx.RequestError:
                status = "UNKNOWN"
            with store.engine.begin() as conn:
                conn.execute(update(outbox).where(outbox.c.id == row["id"]).values(status=status))
            sent += status == "SENT"
    return sent
