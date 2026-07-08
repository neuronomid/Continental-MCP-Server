# mcp_plan.md — Polymarket BTC-5m Research Engine + Intelligence Server (VPS)

**Product:** `pm-research-engine` — 24/7 data collection, analytics, and intelligence service for Polymarket BTC 5-minute Up/Down markets.
**Runs on:** Ubuntu VPS (research only — no keys, no trading).
**Consumers:** (a) the trading bot on the local Linux machine, (b) LLM agents (via MCP), (c) you (via reports and Telegram alerts).
**Status:** v2.1 spec — supersedes `polymarket_btc5m_research_engine_mcp_spec.md`. All original constraints preserved (read-only MCP, no bot-status decisions, no CSV-first, paper-first), plus the improvements below. v2.1 adds the session & holiday calendar model (§1.11, §6.0).

---

## 1. What Changed From v1 and Why (read this first)

These are the material upgrades over the original spec. Each one closes a gap that would have either corrupted the research or produced a strategy that dies on contact with real execution.

### 1.1 Fees are now a first-class citizen (critical)
Polymarket charges **dynamic taker fees on short-dated crypto Up/Down markets**. Makers pay zero; taker fees peak when price is near 50¢ and fund a daily maker-rebate program. Peak taker cost on crypto markets is on the order of **$1.80 per 100 shares (~3%+ of notional at mid prices)** — and your target entry zone (dominant side at 0.55–0.65) sits close to the fee peak.

Consequences baked into this design:
- Every market row stores its fee parameters (`feesEnabled` + fee rate fetched from the CLOB market-info endpoint at discovery time — fee schedules are per-market and can change).
- **Every EV metric exists in three variants:** gross, net-taker (spread + slippage + taker fee), net-maker (assumes passive fill, zero fee, optional rebate estimate).
- The analytics engine treats **maker-style entries as a first-class strategy family**, not an afterthought. Fees were introduced specifically to kill taker-bot edge in these markets; if any edge survives, it is far more likely to be reachable as a maker.

### 1.2 Resolution source is Chainlink, not Binance (critical)
BTC 5m Up/Down markets resolve on the **Chainlink BTC/USD Data Stream** (`data.chain.link/streams/btc-usd`), and the rule is **`end >= start` → UP** (ties resolve UP — a tiny structural UP bias at the margin). Binance spot is only a *proxy*; on close calls the oracle print decides, and proxy/oracle basis will mislabel your features.

Consequences:
- Collect the **“price to beat”** (window start reference price) from Polymarket market metadata for every market — this is the actual anchor, in oracle terms.
- Compute `btc_distance_from_start` against the price-to-beat, not against Binance-at-window-open.
- Dual BTC proxy feeds (Binance + Coinbase) with a divergence flag; `was_close_call` computed in bps against the price-to-beat; close calls are quarantined from strategy statistics (analyzed separately).
- Open item (Phase 4): evaluate whether direct Chainlink Data Streams access (subscription API) is feasible; if not, the price-to-beat + resolved outcome + dual proxy is sufficient for research.

### 1.3 WS-first snapshotting (accuracy + API hygiene)
v1 polled REST for the t_270…t_30 snapshots. v2 maintains a **live in-memory order book per active market from the CLOB WebSocket market channel** and samples that book locally at exact offsets. REST `GET /book` becomes a verification/fallback path (one REST snapshot per market at t_240 to cross-check WS book integrity).

Benefits: millisecond-accurate `snapshot_actual_seconds_left`, no REST rate-limit pressure, and the full event stream (book deltas, `last_trade_price`, `price_change`, `tick_size_change`) is archived — which enables the maker fill model (1.5).

### 1.4 Calibration-first analytics (fixes a conceptual flaw)
“Accuracy of the dominant side” is the wrong primary metric: a 0.90-priced favorite winning 90% of the time is high accuracy and **zero edge**. If the market is calibrated, buying the dominant side at ask is guaranteed negative EV equal to spread + fee. The real hypothesis is **miscalibration** (favorite-longshot bias, session/regime-conditional bias, overreaction).

Primary analytics are therefore:
- **Reliability curves**: for each price bucket × timestamp (× session × regime), empirical win rate vs. price. Edge per bucket = `win_rate − avg_entry_price`.
- **Brier score / log loss** of market price as a probability forecast, per timestamp.
- **Net EV** per bucket for taker and maker entry styles (per 1.1).
- Wilson CIs on win rates; **decisions are made on the CI lower bound of net EV**, never the point estimate.

### 1.5 Maker fill model (new capability)
Because we archive the full trade-print stream, we can honestly answer: *“If I had posted a limit buy at price p at time t, would it have filled, and when?”* — fill is inferred from subsequent prints at/through p on that token. This produces `P(fill | offset, timestamp, regime)` and time-to-fill distributions, which the bot's paper simulator consumes. Without this, "maker EV" is fantasy.

### 1.6 Fair-value model as an independent benchmark (new capability)
For a ~driftless diffusion, the fair UP probability at time t is approximately `Φ(d / (σ·√τ))` where `d` = log-distance of current oracle-proxy price from the price-to-beat, `σ` = short-horizon realized vol (EWMA of 1s returns), `τ` = seconds remaining. The engine computes `p_fair` for every snapshot and stores `model_edge = market_price − p_fair`. This gives you a *structural* mispricing signal to cross with the *empirical* calibration tables — two independent lenses that must agree before a candidate is trusted.

### 1.7 Multiple-testing control (fixes the silent killer)
The filter grid (9 timestamps × sessions × regimes × price buckets × liquidity buckets) is hundreds of cells. At 95% confidence you will "discover" dozens of fake edges. All bucket-level significance claims pass through **Benjamini–Hochberg FDR (q = 0.10)** within each analysis run, and no candidate is created from a bucket that fails FDR — regardless of how good the point estimate looks. Walk-forward remains the final arbiter.

### 1.8 Dual protocol facade: REST for bots, MCP for agents (architecture fix)
MCP is the right interface for LLM agents; it is the wrong data plane for a deterministic bot running a 5-minute loop (JSON-RPC session overhead, agent-oriented semantics). v2 exposes **one query/service layer with two facades**:
- **FastAPI REST** (bearer token) — consumed by the trading bot: low latency, versioned JSON, plus a narrow **ingest API** so the bot can push paper trades / decision logs *without database credentials*.
- **MCP server** (Streamable HTTP transport, bearer token) — consumed by Claude/agents: same data, tool-shaped, with evidence and freshness metadata on every response.

Both facades are read-only with respect to trading. The ingest API is the only write path, and it only writes bot telemetry tables.

### 1.9 Deterministic market discovery
BTC 5m event slugs follow the pattern `btc-updown-5m-<unix_start>` with one market every 300 seconds. Discovery is therefore *computed* (generate expected slugs for the next N windows, confirm via Gamma) rather than crawled — with a Gamma tag/series scan as backstop for pattern changes. This removes the "missed market" failure mode.

### 1.10 Everything else from v1 is preserved
Storage triad (TimescaleDB + Parquet + raw JSON), the seven bot statuses, three bot modes, strategy candidate registry with champion/challenger, one-month paper minimum, no keys on the VPS, MCP never commands bots. The v1 schema (§11 of the original spec) is adopted with the additive columns listed in §5 below.

### 1.11 Session & holiday calendar model (v2.1)
Three canonical sessions — **Tokyo, London, New York** — defined in their **native time zones** (Asia/Tokyo, Europe/London, America/New_York) so DST is handled automatically, plus derived labels for overlaps (`london_ny_overlap`, `tokyo_london_overlap`) and `off_session` dead hours. The engine always knows the current session, stamps it on every snapshot and on every REST/MCP response, and **every performance metric is always emitted in two scopes: total and per-session** — e.g., t_240 accuracy 43% overall but 34% in the New York session; both numbers ship side by side, each with its own n and CI.

Correction to the raw request: BTC-5m markets never close — crypto trades through every holiday, so there is no "market closed" state. What holidays actually change is *participation*: a NY session on a NYSE/US-bank holiday is not a real NY session. Holidays are therefore modeled as **session integrity labels** (`regular` | `holiday` | `half_day` | `weekend`) driven by exchange calendars (NYSE→NY, LSE→London, JPX→Tokyo). Regular-integrity days form the default per-session populations; holiday/half-day/weekend instances are excluded and aggregated into their own buckets — otherwise Thanksgiving silently contaminates the NY statistics. Consumers (bot, agents) never recompute sessions with their own logic; they read the engine's current-session output or the shared versioned module, so labels can't drift between machines.

---

## 2. Honest Framing of the Bet (design implications)

- **Null hypothesis:** these markets are efficiently made by low-latency MMs; dominant-side price ≈ true probability; taker EV = −(spread + fee). The infrastructure exists to *reject or fail to reject* this, cheaply, before real money moves.
- **Where edge could plausibly survive:** (a) maker entries harvesting spread + rebates in regimes where adverse selection is low; (b) conditional miscalibration in thin sessions (weekend/dead hours) or specific vol regimes; (c) underdog value when the crowd overreacts to small early moves (test the *contrarian* side of every bucket — the same tables answer both directions for free).
- **Capacity is small.** Depth within 1–2¢ of top-of-book on these books supports single-digit-dollar clips. This system's ceiling is beer money on BTC-5m specifically; its real value is that the entire engine generalizes to 15m/1h crypto, and the collector/analytics/registry pattern generalizes to every other Polymarket family you already care about (sports, elections).

---

## 3. System Architecture (VPS)

```text
Ubuntu VPS  —  pm-research-engine
│
├── collectors/  (asyncio services)
│   ├── discovery        Gamma slug generator + tag scan + fee-param fetch (CLOB market info)
│   ├── clob_ws          WS market channel → live books, trade prints, raw JSONL archive
│   ├── snapshotter      samples live books at t_270…t_30, REST /book cross-check at t_240
│   ├── btc_feed         Binance spot WS (bookTicker+aggTrade) + Binance perp + Coinbase spot
│   └── resolution       price-to-beat capture, outcome detection, close-call flags
│
├── storage/
│   ├── PostgreSQL 16 + TimescaleDB   (normalized operational data, hypertables + compression)
│   ├── /data/parquet                 (daily partitioned feature/analysis exports; polars/duckdb)
│   └── /data/raw                     (zstd-compressed JSON/JSONL, layout per v1 §6.4)
│
├── analytics/  (systemd timers)
│   ├── hourly    calibration curves, Brier, net-EV (taker/maker), Wilson CIs, BH-FDR
│   ├── daily     regime labeling refresh, candidate review, daily report, Parquet export
│   └── weekly    walk-forward, champion/challenger evaluation, maker fill model refit
│
├── registry/
│   └── strategy candidates: research_only → paper_only → challenger → champion → (tiny_live_allowed) / disabled
│
└── serving/
    ├── FastAPI REST facade   (bot data plane + ingest API, bearer token)
    ├── MCP server            (agent facade, Streamable HTTP, bearer token)
    └── alerts                (Telegram sendMessage for health/critical events)
```

Separation of responsibilities (unchanged from v1):
```text
Research engine + facades = evidence only
Agent                     = interpretation/explanation
Trading bot               = independent decisions + own status
Risk engine (bot-side)    = final authority on exposure
Human                     = mode control + live approval
```

---

## 4. Data Sources

| Source | Transport | What we take | Notes |
|---|---|---|---|
| Gamma API (`gamma-api.polymarket.com`) | REST | market/event metadata, outcomes, token IDs, slugs, `enableOrderBook`, timing fields, price-to-beat if present in metadata | discovery via computed slugs `btc-updown-5m-<unix_start>` + tag scan backstop |
| CLOB API (`clob.polymarket.com`) | REST | `/book`, `/price`, `/midpoint`, `/spread`, market info incl. **fee params** (`feesEnabled`, rates), tick size, min order size | REST is fallback/verification; fee params fetched per market at discovery |
| CLOB WebSocket market channel | WSS | full book snapshots + deltas, `price_change`, `last_trade_price` (prints), `tick_size_change` | primary snapshot source; raw JSONL archived per market-hour |
| Binance spot | WSS | `btcusdt@bookTicker`, `@aggTrade`, `@kline_1s` | proxy feed #1; 1s bar features |
| Binance USDⓈ-M perp | WSS | `btcusdt@markPrice@1s`, `@aggTrade` | basis, funding |
| Coinbase Exchange | WSS | `BTC-USD` ticker/matches | proxy feed #2; divergence flag vs Binance |
| Chainlink BTC/USD Data Stream | TBD | resolution-grade price | Phase-4 spike: subscription feasibility; fallback = price-to-beat + resolved outcome + dual proxy |
| Polymarket market page / metadata | REST | resolved outcome, resolution timestamps | outcome cross-checked against Gamma `outcomePrices` after close |

Official docs to re-read at build time (APIs drift):
- https://docs.polymarket.com/api-reference/introduction
- https://docs.polymarket.com/market-data/overview
- https://docs.polymarket.com/api-reference/market-data/get-order-book
- https://docs.polymarket.com/market-data/websocket/overview
- https://docs.polymarket.com/market-data/websocket/market-channel
- https://docs.polymarket.com/trading/orderbook
- **https://docs.polymarket.com/trading/fees** (fee curve, maker rebates, `feesEnabled`, per-market fee query)
- https://modelcontextprotocol.io/docs/getting-started/intro
- https://modelcontextprotocol.io/specification (tools + resources, latest revision)
- https://github.com/modelcontextprotocol/python-sdk

---

## 5. Storage Design

Adopt the v1 schema (§11 of original spec) in full. Additive changes:

### 5.1 New/changed columns
- `markets`: `fees_enabled BOOL`, `fee_rate_bps NUMERIC`, `fee_params_json`, `tick_size NUMERIC`, `min_order_size NUMERIC`, `price_to_beat NUMERIC`, `price_to_beat_source TEXT`, `slug_derived_start_utc`.
- `snapshots`: `p_fair NUMERIC`, `model_edge NUMERIC`, `sigma_1s NUMERIC`, `z_score NUMERIC`, `taker_fee_est_dominant NUMERIC`, `net_ev_inputs_version TEXT`, `btc_source_divergence_bps NUMERIC`, `up_tick_size NUMERIC`, `session_integrity TEXT` (`regular`|`holiday`|`half_day`|`weekend`), `session_model_version TEXT` (v1's `session_primary`/`session_overlap` retained).
- `market_resolutions`: `price_to_beat NUMERIC`, `oracle_end_price NUMERIC NULL`, `proxy_end_price NUMERIC`, `proxy_oracle_divergence_bps NUMERIC NULL`, `tie_rule_applied BOOL` (end == start → UP).
- `timestamp_performance`: add `entry_style` (`taker_ask` | `maker_mid` | `maker_join_bid`), `net_ev_taker`, `net_ev_maker`, `net_ev_ci_lower_95`, `edge_vs_price` (win_rate − avg_price), `brier`, `fdr_pass BOOL`, `fill_prob_maker`, `median_time_to_fill_s`, `scope` (`total` | `session:<name>` | `overlap:<name>`), `session_integrity_filter` (default `regular`).
- `strategy_candidates`: add `entry_style`, `direction` (`dominant` | `contrarian`), `fee_model_version`, `fdr_pass`, `model_edge_gate NUMERIC NULL`.

### 5.2 New tables
```text
trade_prints         -- one row per WS last_trade_price event: market_id, token_id, price, size, side, ts
maker_fill_estimates -- analysis output: snapshot_label, offset_ticks, regime, p_fill, median_ttf_s, sample_size, run_id
calibration_bins     -- analysis output: run_id, snapshot_label, price_bin, filters..., n, win_rate, wilson_lo, wilson_hi, edge, fdr_pass
fee_schedules        -- per market_id: captured fee params + captured_at (fees can change; keep history)
session_calendar     -- one row per date × session: boundaries_utc, integrity (regular/holiday/half_day/weekend),
                        source_calendar (XNYS/XLON/XTKS), session_model_version; materialized 90 days ahead
ingest_tokens        -- hashed bearer tokens for the ingest API, scope, created/revoked
```

### 5.3 Timescale/retention policy
- Hypertables: `btc_ticks`, `trade_prints`, `orderbook_levels`, `system_health_events`. Compression after 7 days; no automatic drop in v1.
- Raw JSONL: zstd-compressed hourly files; budget estimate ~0.5–2 GB/day → 80 GB disk gives ~2–4 months headroom before pruning decisions; monitor via health checks.
- Parquet: daily partitions `dt=YYYY-MM-DD/` for `snapshots_features`, `resolutions`, `btc_1s_bars`, `trade_prints`; exported by the daily job; queried with duckdb/polars.
- Backups: nightly `pg_dump` + rsync/restic of `/data/parquet` manifests offsite (raw JSONL excluded from offsite by default — too big; it is reproducible risk you accept).

---

## 6. Analytics Design

### 6.0 Session & calendar model (foundation for everything below)
- **Definitions (config, versioned `SESSION_MODEL_VERSION`):** Tokyo 09:00–15:00 JST · London 08:00–16:30 UK · New York 09:30–16:00 ET (defaults — tune after the first weeks of data show where behavior actually shifts). Derived labels: `london_ny_overlap`, `tokyo_london_overlap`, `off_session` (gaps). Because definitions are tz-local, DST moves UTC boundaries automatically; Tokyo has no DST.
- **Calendar materialization (daily job):** `session_calendar` filled 90 days ahead via exchange calendars (NYSE, LSE, JPX) incl. half-day schedules; each date × session gets an integrity label (`regular`/`holiday`/`half_day`/`weekend`).
- **Stamping:** every snapshot stores `session_primary`, `session_overlap`, `session_integrity`, `session_model_version`. The current session is computable at any instant.
- **Dual-scope rule:** every bucketed metric (calibration bins, timestamp performance, EV, Brier) is emitted at `scope=total` **and** per session/overlap. Default population is `regular` integrity; holiday/half-day/weekend instances form separate buckets. Session buckets inherit the v1 sample-size floors (≥ 200 for timestamp × session; smaller = research-only).
- **Consumer contract:** bot and agents read the current session from the engine (or the shared versioned module) — never their own ad-hoc clock math. A `SESSION_MODEL_VERSION` mismatch between producer and consumer is an error condition, not a warning.

### 6.1 Hourly run
1. Ingest window close: only markets with resolution + quality flags OK (drop `stale_book_flag`, `crossed_book_flag`, `bad_sum_flag`, close-calls quarantined).
2. Build calibration bins per timestamp × price bucket (2¢ bins from 0.50–0.98) with session/regime/liquidity filters — always emitted at total scope AND per session/overlap on the regular-integrity population, per §6.0. Compute n, win rate, Wilson CI, `edge = win_rate − avg_entry_price_variant` for entry price variants (mid, ask, maker-mid).
3. Compute net EV per bin per entry style:
   - `net_ev_taker = win_rate·(1−ask_vwap) − (1−win_rate)·ask_vwap − taker_fee(ask_vwap)` with the fee curve `fee ≈ rate · price · (1−price)` per share (verify exact formula from fee docs / market info at build time).
   - `net_ev_maker = p_fill · [win_rate·(1−limit_px) − (1−win_rate)·limit_px] ` using `maker_fill_estimates`; unfilled = 0 EV (opportunity cost noted, not charged).
4. Brier score and log loss of market mid as forecast, per timestamp.
5. BH-FDR across all tested bins in the run; write `calibration_bins` + `timestamp_performance`.
6. Fair-value residual analysis: distribution of `model_edge` by regime; correlation of `model_edge` with subsequent outcome (does the market or the model win?).

### 6.2 Daily run
- Regime labeler refresh (k-means or quantile rules over σ_1s, trade intensity, spread; **versioned** `regime_model_version`).
- Candidate extraction: any bin family that passes ALL gates → create/refresh `strategy_candidates` (status `research_only`).
- Daily report (markdown, stored as MCP resource + pushed to Telegram): data quality, top calibration deviations, candidate changes, paper performance.
- Parquet export.

### 6.3 Weekly run
- Walk-forward: train 14d → validate 3d, rolled; a candidate must be positive (CI-lower net EV > 0) in ≥ 2 consecutive validation windows.
- Champion/challenger scoring on paper-trade telemetry (from ingest).
- Maker fill model refit.

### 6.4 Promotion gates (enforced by the registry, reported via MCP; promotion to paper is a config act by you, never automatic to live)
```text
research_only → paper_only:
  n ≥ 500 (bin family), FDR pass, walk-forward pass ×2,
  CI-lower net EV (chosen entry style) > 0 after fees,
  liquidity: median max_usd_buy_within_2c ≥ planned clip size
paper_only → challenger/champion:
  ≥ 200 paper trades, paper net PnL > 0,
  realized slippage/fees within 20% of model, no monotonic decay across weeks
champion → tiny_live_allowed (future, manual):
  ≥ 30 days paper, human review, bot-side risk caps configured
any → disabled:
  CI-lower net EV < 0 over trailing 14d, or data-quality regression, or manual
```

---

## 7. Serving Layer

### 7.1 FastAPI REST facade (bot data plane)
Read endpoints (bearer token, JSON, all responses carry `generated_at`, `data_last_updated_at`, `staleness_s`, `warnings[]`):
```text
GET /v1/health
GET /v1/session/current              -- primary session, overlaps, integrity, seconds to next boundary, SESSION_MODEL_VERSION
GET /v1/markets/current              -- active + next BTC5m market, token IDs, price_to_beat, fee params, tick size
GET /v1/snapshots/latest?market_id=&label=
GET /v1/performance/timestamps?window=&entry_style=&filters...
GET /v1/performance/calibration?label=&window=&filters...
GET /v1/performance/sessions | /regimes
GET /v1/candidates?status=
GET /v1/candidates/champion
GET /v1/fills/maker-estimates?label=&offset=
GET /v1/fairvalue/params             -- current sigma model params/version
GET /v1/quality/report?window=
```
Ingest endpoints (separate token scope; writes only bot-telemetry tables):
```text
POST /v1/ingest/paper-trades
POST /v1/ingest/bot-decisions
POST /v1/ingest/bot-heartbeat
```

### 7.2 MCP server (agent facade)
- Python MCP SDK (FastMCP), **Streamable HTTP** transport, bearer token; bind behind nginx/caddy with TLS, or WireGuard/Tailscale between VPS and local machine (preferred: private overlay network, nothing public).
- Tools (superset of v1 §15.2): `get_system_health`, `get_current_session` (new — primary/overlaps/integrity/next boundary), `get_current_btc5m_market`, `get_latest_market_snapshot`, `get_timestamp_performance`, `get_calibration_curve` (new), `get_session_performance`, `get_regime_performance`, `get_fee_parameters` (new), `get_fair_value_snapshot` (new), `get_maker_fill_estimates` (new), `get_strategy_candidates`, `get_champion_strategy`, `get_paper_trade_performance`, `get_analysis_run_summary`, `get_data_quality_report`.
- Resources: schema/data dictionary, methodology notes, latest daily report, candidate registry, fee model notes.
- Hard rules (unchanged): no trade endpoints, no keys, no bot-status output, every response stamped with freshness + evidence (n, CI, window). **Every response envelope also carries `current_session` + `session_integrity`**, and every performance tool returns total scope and per-session scope side by side (each with its own n/CI) unless a single scope is explicitly requested.

---

## 8. Operations

- **Time sync:** chrony on VPS *and* local machine; refuse snapshots if measured drift > 250 ms (health event).
- **Process supervision:** systemd units per collector + timers for analytics (v1 §21.2 list retained, plus `research-engine-rest.service`); Docker Compose acceptable alternative.
- **Config:** `.env` per v1 §21.3, plus `FEE_MODEL_VERSION`, `REGIME_MODEL_VERSION`, `REST_BEARER_TOKENS`, `MCP_BEARER_TOKEN`, `INGEST_BEARER_TOKEN`, `ALERT_TELEGRAM_BOT_TOKEN`, `ALERT_TELEGRAM_CHAT_ID`.
- **Monitoring:** heartbeat rows per service (60s) → `system_health_events`; watchdog timer alerts to Telegram on: collector silent > 3 min, WS reconnect storm, snapshot miss rate > 2%/h, disk > 80%, analysis failure, clock drift.
- **Logging:** structlog JSON to journald; logrotate for raw archive manifests.
- **Security:** UFW default-deny; only WireGuard/Tailscale + SSH; REST/MCP bound to overlay IP; no Polymarket credentials of any kind on the VPS.

---

## 9. Resources & Requirements

### 9.1 VPS sizing
- Ubuntu 24.04 LTS, 2–4 vCPU, 8 GB RAM (4 GB floor; Timescale + analytics prefer 8), 160 GB NVMe preferred (80 GB floor with pruning), 1 static IP. Hetzner/OVH/Contabo class is fine (~$10–25/mo).

### 9.2 Core packages (Python 3.11+, managed with `uv`)
```text
Runtime: httpx, websockets, pydantic v2, pydantic-settings, tenacity, orjson,
         zstandard, structlog, apscheduler (in-process schedules; systemd timers for analytics)
DB:      psycopg[binary] / asyncpg, SQLAlchemy 2.x, alembic
Analytics: polars, duckdb, pyarrow, numpy, scipy, statsmodels, exchange_calendars (NYSE/LSE/JPX holiday + half-day calendars)
Serving: fastapi, uvicorn, mcp (official python-sdk / FastMCP)
Testing: pytest, pytest-asyncio, respx, time-machine, hypothesis, testcontainers[postgres], coverage
```

### 9.3 Team/effort assumption
Solo + coding agent. Realistic calendar: **2–3 weeks to first full data day; ~5–6 weeks to MCP live** (phases in `mcp_phases.md`), then the clock on the 30-day paper window starts.

---

## 10. Risks & Open Questions

1. **Chainlink Data Streams access** — may require paid subscription; fallback path (price-to-beat + outcome + dual proxy) is designed in. Decision point at Phase 4.
2. **Fee schedule drift** — Polymarket has adjusted the fee formula since rollout; `fee_schedules` history table + `FEE_MODEL_VERSION` exists precisely so old EV numbers aren't silently wrong.
3. **API/WS shape drift** — raw JSONL archive + versioned parsers make re-normalization possible; contract tests against recorded fixtures run in CI.
4. **Market discontinuation** — 5m markets are a product experiment; if killed, the engine retargets 15m/1h with a config change (design keeps `market_type` generic).
5. **Survivorship in your own pipeline** — markets whose snapshots failed must still enter denominator counts for quality reporting; never analyze only "clean" markets without recording how many were dropped and why.
6. **Rate limits / bans** — WS-first design keeps REST usage minimal; identify with a UA string; exponential backoff with jitter everywhere.
