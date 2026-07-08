# Continental-MCP-Server

A 24/7 data-collection, analytics, and intelligence service for **Polymarket BTC
5-minute Up/Down** markets, exposed to LLM agents over the **Model Context
Protocol (MCP)**. It runs on an Ubuntu VPS and is strictly **research-only**: no
API keys, no order placement, no bot-status decisions. Every response is framed
as *evidence* (data with sample sizes, confidence intervals, and freshness), not
trading advice.

Consumers are (a) LLM agents via **MCP**, (b) a trading bot via a read-only REST
data plane + ingest, and (c) humans via reports and Telegram alerts. The Python
package is named `pmre` (pm-research-engine); this repository is the MCP server
that fronts it.

---

## For agents: connect to the MCP server

The server speaks **Streamable HTTP** with **bearer-token auth** and is
**read-only**. Any MCP-capable client (Claude, the OpenAI Agents SDK, OpenAI's
hosted `{"type": "mcp"}` tool, custom clients) can auto-discover every tool via
`tools/list` and call it — you do not hand-wire the tools.

- **Endpoint:** `http://<host>:8090/mcp` (default port `8090`)
- **Transport:** Streamable HTTP
- **Auth:** `Authorization: Bearer <PMRE_MCP_BEARER_TOKEN>` on every request
  (a missing/invalid token returns `401`)
- **Network:** bind to a private WireGuard/Tailscale overlay IP — never expose
  it publicly

### Claude Code / `mcp.json`

```jsonc
{
  "mcpServers": {
    "continental": {
      "type": "http",
      "url": "http://127.0.0.1:8090/mcp",
      "headers": { "Authorization": "Bearer <PMRE_MCP_BEARER_TOKEN>" }
    }
  }
}
```

### OpenAI Agents SDK (Python)

A complete, runnable example lives at [`examples/openai_agent_mcp.py`](examples/openai_agent_mcp.py):

```python
from agents import Agent, Runner
from agents.mcp import MCPServerStreamableHttp

async with MCPServerStreamableHttp(
    name="continental",
    params={
        "url": "http://127.0.0.1:8090/mcp",
        "headers": {"Authorization": "Bearer <PMRE_MCP_BEARER_TOKEN>"},
    },
    cache_tools_list=True,
) as server:
    agent = Agent(name="analyst", model="gpt-4o-mini", mcp_servers=[server])
    result = await Runner.run(agent, "Is the engine healthy? What is the "
                                     "strongest candidate by CI-lower net EV?")
    print(result.final_output)
```

### Response envelope

Every tool response is wrapped in a **freshness envelope** carrying
`data_last_updated_at`, `current_session`, `session_integrity`, and any
`warnings`. Decisions should be made on **CI lower bounds, never point
estimates**.

### Tools (16, all read-only)

| Tool | Returns |
| --- | --- |
| `get_system_health` | Which collectors are alive/silent + recent incidents |
| `get_current_session` | Primary/overlap session, integrity, seconds to next boundary |
| `get_current_btc5m_market` | Active + next market: token ids, price-to-beat, fee params, tick size |
| `get_latest_market_snapshot` | Latest order book: mids, spreads, depth, fee estimate |
| `get_timestamp_performance` | Per-timestamp win rate, net EV (taker/maker), CI-lower, Brier |
| `get_calibration_curve` | Reliability curve (win rate vs price) per 2¢ bin, Wilson CIs, FDR flags |
| `get_session_performance` | Performance per session/overlap (each with its own n and CI) |
| `get_regime_performance` | Performance per volatility regime |
| `get_fee_parameters` | Fee params for a market (feesEnabled, rate, model version) + history |
| `get_fair_value_snapshot` | Fair-value output: `p_fair`, `z_score`, `sigma_1s`, `model_edge` |
| `get_maker_fill_estimates` | P(fill) and time-to-fill for hypothetical maker posts |
| `get_strategy_candidates` | Strategy candidates with status + full evidence |
| `get_champion_strategy` | Current champion strategy and its CI-lower net EV (or null) |
| `get_paper_trade_performance` | Aggregated paper-trade telemetry ingested from the bot |
| `get_analysis_run_summary` | Summary of an analysis run: counts, versions, deterministic hash |
| `get_data_quality_report` | Snapshot counts, stale/crossed/close-call rates over a window |

### Resources

| URI | Contents |
| --- | --- |
| `pmre://methodology` | Calibration-first, CI-lower decisions, BH-FDR, dual-scope sessions |
| `pmre://data-dictionary` | Schema / field dictionary for the served tables |
| `pmre://fee-model` | Dynamic taker fee curve notes |
| `pmre://daily-report` | Latest daily research report (markdown) |

---

## Run the server

```bash
uv sync

# Set the MCP bearer token (and other secrets) — copy the template first.
cp .env.example .env        # then edit PMRE_MCP_BEARER_TOKEN, PMRE_DATABASE_URL, ...

uv run python -m pmre migrate      # create/upgrade the schema
uv run python -m pmre serve-mcp    # MCP server on PMRE_SERVING_HOST:PMRE_MCP_PORT (default :8090)
```

`./run.sh` launches the whole system locally (migrate + calendar + collectors +
REST + MCP + hourly analytics). Modes: `all` (default), `serve` (no collectors),
`demo` (synthetic offline dataset), `collectors`. See the header of `run.sh`.

Relevant environment variables (all `PMRE_`-prefixed; see `.env.example`):

| Variable | Purpose |
| --- | --- |
| `PMRE_MCP_PORT` | MCP server port (default `8090`) |
| `PMRE_MCP_BEARER_TOKEN` | Bearer token agents must present |
| `PMRE_SERVING_HOST` | Bind address — set to the overlay IP |
| `PMRE_DATABASE_URL` | Postgres 16 + TimescaleDB in prod; SQLite for dev/tests |
| `PMRE_ENV` | `dev` or `production` (prod fails fast on any unset secret) |

---

## What the engine does

- **Fees are first-class** — dynamic taker-fee curve `rate·p·(1−p)`; every EV in
  gross / net-taker / net-maker variants; maker entries are a first-class family.
- **Calibration-first analytics** — reliability curves (win rate vs price) with
  Wilson CIs; decisions on **CI-lower net EV**, never point estimates.
- **Multiple-testing control** — Benjamini-Hochberg FDR (q=0.10) on every bucket
  claim; a null dataset produces ~zero "edges" (the project's most important test).
- **Fair-value benchmark** — `p_fair = Φ(z)`, `z = ln(S/S_ptb)/(σ_1s·√τ)`,
  independent of the empirical tables.
- **Session & holiday model** — tz-native Tokyo/London/New York + overlaps;
  holidays are *integrity* labels (regular/holiday/half_day/weekend), not
  closures. Every metric ships at `total` AND per-session scope.
- **Two facades, one service layer** — FastAPI REST (bot) + MCP (agents), both
  read-only, both stamped with freshness + evidence + current session.
- **Storage triad** — PostgreSQL 16 + TimescaleDB (hypertables + compression),
  daily Parquet exports, zstd raw JSONL archive (replayable).

## Layout

```
src/pm_sessions/     shared, versioned session/calendar model (the bot reuses it)
src/pmre/
  config.py          pydantic-settings (fails fast on missing prod secrets)
  db/                SQLAlchemy models (all v1+v2 tables) + engine + timescale
  collectors/        slugs, discovery, clob_ws (book engine), snapshotter,
                     btc_feed, resolution, calendar_job, supervisor
  features/          fair_value, btc_state
  analytics/         stats (Wilson/BH), ev, calibration, regime, maker_fill,
                     walkforward, runner, reports
  registry/          candidates, gates, extractor  (research_only -> ... -> disabled)
  serving/           service (shared), envelope, auth, ingest,
                     rest/ (FastAPI), mcp/ (FastMCP)  <-- MCP server lives here
  ops/               health, alerts, clock, watchdog, backup, systemd_notify
  parquet_export.py  daily exporter + duckdb + replay
  cli.py             `python -m pmre <command>`
tests/               unit + integration + golden fixtures
deploy/              systemd units/timers, compose, Caddy, wireguard, runbooks
```

## REST facade (for the bot, not agents)

`serve-rest` exposes read-only `GET /v1/health|session/current|markets/current|
snapshots/latest|performance/*|candidates*|fills/maker-estimates|
fairvalue/params|fees/parameters|quality/report|analysis/summary` plus ingest
`POST /v1/ingest/paper-trades|bot-decisions|bot-heartbeat`. Bearer auth (read vs
ingest scopes), freshness envelope on every read.

## Development

```bash
uv sync
uv run pytest                      # full suite (SQLite; no Docker needed)
uv run ruff check src tests

# End-to-end on a throwaway DB: seed synthetic data with a planted, persistent
# NY-session edge, run analytics -> walk-forward -> candidate extraction.
export PMRE_DATABASE_URL="sqlite+pysqlite:///./pmre.db"
uv run python -m pmre migrate
uv run python -m pmre pipeline-demo --days 25     # discovers the planted edge
uv run python -m pmre daily-report
```

### Testing on real PostgreSQL + TimescaleDB

```bash
docker run -d --name pg -e POSTGRES_USER=pmre -e POSTGRES_PASSWORD=pmre \
  -e POSTGRES_DB=pmre -p 55432:5432 timescale/timescaledb:latest-pg16
PMRE_TEST_POSTGRES_URL=postgresql+psycopg2://pmre:pmre@127.0.0.1:55432/pmre \
  uv run pytest tests/test_postgres_integration.py
```

## Deployment

`deploy/` ships systemd units/timers (`pmre-mcp.service`, `pmre-rest.service`,
analytics/backup timers), a Docker Compose stack, a `Caddyfile`, and WireGuard
notes. Bind both facades to the overlay IP; nothing is public. See
`deploy/README.md` for install, runbooks, and chaos drills.

## Design docs

`mcp_plan.md` (v2.1 spec) and `mcp_phases.md` (the 10 build phases) document the
full design and status. All phases are implemented and tested.

---

*Research only. Every response is evidence — data with n, confidence intervals,
and freshness — not trading advice.*
