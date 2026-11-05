---
name: tt-run-local
description: Run or troubleshoot the Toss Trading API and dashboard locally for development, or provide their local URLs and startup command.
---

# Toss Trading local development

Use the Toss Trading repository root so Uvicorn loads the project's `.env` and resolves relative config and data paths. Use the current checkout when available.

- If the user asks for a URL, check whether the local Toss Trading API is already running and give the URL for its port. If it is stopped, label the URL accordingly and provide the startup command.
- If the user asks to start the app, reuse a healthy local Toss Trading instance or launch one using a tool session that can remain active. Start with port 8001 and choose another free loopback port if another app owns it. Leave other services running.
- If setup is needed, use `uv sync --frozen --python 3.12`. Preserve any existing `.env`. Refer to `.env.example` for settings and keep secret values out of output.

```sh
LIVE_TRADING_ENABLED=false uv run uvicorn toss_trading.api:create_app \
  --factory --host 127.0.0.1 --port 8001 --reload
```

For a startup request, verify `/health` returns `status: ok` and `live_trading_enabled: false` before reporting success. Give the actual URL for `/dashboard/` and, if useful, `/docs`. The dashboard uses `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD`. Broker query routes require the relevant Toss credentials and `x-api-key`.

Keep this workflow scoped to the local API. Start it with LIVE disabled and use `uv run pytest` if the user asks for automated tests without broker or Slack connections.
