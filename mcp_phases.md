# mcp_phases.md — Build Phases: Research Engine + REST/MCP Server (VPS)

Companion to `mcp_plan.md`. Each phase lists: goal, build items, packages, tests, acceptance criteria, and heads-ups. Phases are strictly ordered; do not start N+1 until N's acceptance criteria pass. Repo name: `pm-research-engine`.

**Suggested repo layout (created in Phase 0, filled progressively):**
```text
pm-research-engine/
├── pyproject.toml            # uv-managed
├── .env.example
├── alembic/                  # migrations
├── src/pmre/
│   ├── config.py             # pydantic-settings
│   ├── db/                   # models, session, repositories
│   ├── collectors/           # discovery, clob_ws, snapshotter, btc_feed, resolution
│   ├── features/             # snapshot feature builder, fair_value, regimes
│   ├── analytics/            # calibration, ev, fdr, walkforward, maker_fill, reports
│   ├── registry/             # strategy candidates, gates
│   ├── serving/rest/         # FastAPI facade + ingest
│   ├── serving/mcp/          # MCP tools/resources
│   └── ops/                  # health, alerts, clock
├── tests/                    # unit + integration + fixtures/
│   └── fixtures/raw/         # recorded real payloads (golden files)
└── deploy/                   # systemd units, compose, caddy/nginx, wireguard notes
```

---

## Phase 0 — Foundation (env, DB, config, ops skeleton)

**Goal:** a VPS that can run supervised async services against a migrated database, with alerts.

**Build:**
- Ubuntu 24.04 hardening: UFW default-deny, SSH keys only, fail2ban, chrony (verify `chronyc tracking` offset < 50 ms), unattended-upgrades.
- WireGuard or Tailscale between VPS and local machine (all serving binds to overlay IP later).
- PostgreSQL 16 + TimescaleDB extension; `pmre` database + role; nightly `pg_dump` cron + restic offsite.
- Python 3.11+ via `uv`; repo skeleton; `pydantic-settings` config; structlog JSON logging; alembic initialized; base tables from v1 schema + v2 additions (`mcp_plan.md` §5) as migration 0001.
- `ops/health.py`: heartbeat writer + `system_health_events`; Telegram alert sender (plain `sendMessage` via httpx).
- systemd unit template + one demo service proving restart-on-failure.

**Packages:** `pydantic`, `pydantic-settings`, `structlog`, `httpx`, `sqlalchemy`, `alembic`, `psycopg[binary]`, `asyncpg`, `tenacity`, `orjson`.

**Tests:**
- `pytest` + `testcontainers[postgres]`: migrations apply cleanly up/down; repositories round-trip each table.
- Config test: missing env var fails fast with a clear message.
- Alert test: mocked Telegram endpoint (respx) receives formatted alert.

**Acceptance:** clean VPS reboot → all demo services return; migration 0001 idempotent; alert lands in your Telegram; `chronyc` offset < 50 ms.

**Heads-ups:**
- Install TimescaleDB from Timescale's apt repo (Ubuntu's Postgres alone lacks it); run `CREATE EXTENSION timescaledb` in the migration.
- Decide overlay networking NOW — retrofitting TLS/public exposure later is where security holes appear.

---

## Phase 1 — Market Discovery + Fee Parameters

**Goal:** every BTC-5m market is known before it opens, with token IDs, price-to-beat, tick size, min order size, and fee params stored.

**Build:**
- Slug generator: `btc-updown-5m-<unix_start>` for the next N windows (windows every 300 s, aligned to ET 5-minute boundaries — confirm alignment empirically); Gamma lookup by slug; tag/series scan every 10 min as backstop and drift detector.
- Market/token normalization into `markets` + `market_tokens` with UP/DOWN mapping validation (outcome names → token IDs; log and refuse ambiguous mappings).
- CLOB market-info fetch per market: `feesEnabled`, fee rate/params, tick size, min order size → `markets` + `fee_schedules` history row.
- Price-to-beat capture: inspect Gamma/market metadata for the start reference price field; if it only appears at/after window start, schedule a fetch at t = start + 5 s.
- Raw Gamma JSON archived per v1 path layout (zstd).
- **Session & calendar module** (`pm_sessions`, built as a shared importable package — the bot repo reuses it): tz-native definitions for Tokyo/London/New York + overlaps + `off_session`, `SESSION_MODEL_VERSION`; daily materializer filling `session_calendar` 90 days ahead via `exchange_calendars` (XNYS, XLON, XTKS) with integrity labels (`regular`/`holiday`/`half_day`/`weekend`).

**Packages:** (Phase 0 set) + `exchange_calendars`.

**Tests:**
- Golden-fixture tests: recorded real Gamma market JSON → parser produces exact expected rows (commit fixtures).
- Slug math property test: any UTC instant → correct current/next window start.
- Ambiguous-outcome fixture → mapping refused + health event written.
- Fee param parse test with `feesEnabled` true/false fixtures.
- Session labeling tests: fixed instants around DST transitions (US spring-forward/fall-back, UK changeover) map to correct sessions and UTC boundaries; NYSE/LSE/JPX holiday fixtures produce `holiday` integrity; half-day fixtures; weekend logic; `SESSION_MODEL_VERSION` stamped on every label.

**Acceptance:** 24 h unattended run discovers ≥ 99% of windows (288/day) before their start; spot-check 10 markets on polymarket.com — token IDs and price-to-beat match the UI; `fee_schedules` populated; `session_calendar` filled 90 days ahead (spot-check: July 4 → NY session `holiday`, a JPX holiday → Tokyo `holiday`).

**Heads-ups:**
- Market boundaries are defined in **ET** (per market titles); store UTC everywhere, derive ET only for labels/sessions.
- Do not assume the slug pattern is eternal — the tag-scan backstop must alert if pattern-derived and scan-derived sets diverge.
- Fee params may differ per market and change over time; always keep the history row.

---## Phase 2 — CLOB WebSocket Engine + Snapshotter

**Goal:** ms-accurate snapshots at t_270…t_30 sampled from live in-memory books; full event stream archived.

**Build:**
- WS client for the market channel: subscribe to both token asset_ids per active market (plus next market pre-subscription); handle `book` (full), `price_change` (delta), `last_trade_price`, `tick_size_change`; sequence/consistency checks; auto-resubscribe with jittered backoff; raw JSONL archive per market-hour.
- In-memory book maintenance per token; integrity checks (crossed book, negative sizes, stale > 10 s without heartbeat/event → `stale_book_flag`).
- Trade prints → `trade_prints` table (hypertable).
- Snapshotter: monotonic-clock scheduler firing at exact offsets from `expected_resolution_time_utc`; reads in-memory books; computes all v1 §11.3 snapshot fields + v2 additions (sim VWAP/slippage for $1/$2/$5/$10, depth features, `taker_fee_est_dominant` via fee curve, signed values, quality flags, session fields from the shared `pm_sessions` module — primary/overlap/integrity/version); writes `snapshots` + `orderbook_levels` (top 10).
- REST `GET /book` cross-check at t_240 for each market: compare vs in-memory book; divergence > tolerance → health event + `stale_book_flag`.

**Packages:** `websockets`, `zstandard`.

**Tests:**
- Book-engine unit tests: apply recorded snapshot+delta sequences → final book equals recorded REST book (golden fixtures).
- Crossed/stale/bad-sum flag tests with synthetic books.
- Scheduler test with `time-machine`: offsets fire within tolerance; `snapshot_actual_seconds_left` recorded truthfully.
- Slippage/VWAP math property tests (hypothesis): VWAP monotone in size; slippage ≥ 0; depth caps respected.
- Reconnect chaos test: kill WS mid-stream → full recovery, gap flagged, no corrupt books.

**Acceptance:** 48 h run: snapshot capture rate ≥ 98% per label; median |target − actual| < 150 ms; REST cross-checks diverge < 1% of markets; raw JSONL replays into identical books.

**Heads-ups:**
- **This is the hardest collector.** Budget the most time here; every downstream analysis inherits its quality.
- Sum of UP+DOWN best asks normally ≳ 1 (that gap is the vig — store `market_spread_proxy`); a sum ≪ 1 usually means one book is stale, not free money.
- Both tokens' books are mirror-ish but NOT redundant — collect both; discrepancies are themselves a data-quality signal.
- Pre-subscribe the next window ~60 s early; the first seconds after open are chaotic and you want book state from the first event.

---

## Phase 3 — BTC Feed (proxies) + Fair-Value Features

**Goal:** continuous BTC state so every snapshot carries distance/vol/momentum features and `p_fair`.

**Build:**
- Binance spot WS (`bookTicker`, `aggTrade`, `kline_1s`) → in-memory rolling state + `btc_ticks` (1 s bars, hypertable). Binance perp WS (`markPrice@1s`, `aggTrade`) → basis/funding. Coinbase `BTC-USD` matches/ticker → proxy #2 + `btc_source_divergence_bps`.
- Feature module: returns over 5/15/30/60 s, EWMA σ_1s, realized vol 30 s/60 s/5 m, distance from **price-to-beat** (level + bps), high/low since start, trend/reversal flags, volume/trade intensity.
- Fair value: `p_fair = Φ(z)`, `z = ln(S/S_ptb) / (σ_1s·√τ)`; store `p_fair`, `z_score`, `model_edge = dominant-signed market mid − signed p_fair` on every snapshot.
- Snapshotter integration: snapshot writes now join live BTC state (same process or shared state via Redis-less in-proc bus — keep it one process group if possible).

**Packages:** `numpy`, `scipy`.

**Tests:**
- Deterministic feature tests on synthetic tick sequences (known σ, known returns).
- `p_fair` sanity: z=0 → 0.5; large |z| → →0/1; τ→0 behavior clamped.
- Divergence flag test: feeds disagree by > X bps → flag set.
- Restart test: process restart mid-window → features degrade gracefully with `feature_quality` flag, no fabricated values.

**Acceptance:** 24 h run: ≥ 99.5% of snapshots carry complete BTC features; `p_fair` distribution sane (calibration eyeball vs outcomes on the first few hundred markets); divergence flag rate < 0.5% in calm conditions.

**Heads-ups:**
- Use the **price-to-beat** as the distance anchor, never Binance-at-open — this is the whole point of §1.2.
- EWMA σ must be floored (BTC can go dead-quiet; division by ~0 makes z explode); floor and flag.
- Binance WS forcibly disconnects every 24 h — schedule proactive reconnects; never let both proxy feeds reconnect simultaneously.

---

## Phase 4 — Resolution Collector + Oracle Reconciliation

**Goal:** every market gets a final outcome, a close-call classification, and (if feasible) oracle-grade end price.

**Build:**
- Outcome detection: poll Gamma/CLOB market state after `market_end_time` until resolved; parse winning outcome → `market_resolutions`; label `market_tokens.is_winner`, snapshot correctness fields.
- Close-call: `|proxy_end − price_to_beat|` in bps; `was_close_call = margin < threshold` (start at 2 bps; tune later). `tie_rule_applied` when end ≥ start decides UP at ~0 margin.
- **Spike (timeboxed 1 day):** Chainlink Data Streams access — evaluate subscription/API for BTC/USD stream reads. If viable → `oracle_end_price` + true margins; if not → document and rely on fallback.
- Resolution latency + failure alerts (unresolved > 10 min after end).

**Tests:**
- Fixture tests for resolved-market payloads (UP win, DOWN win, tie-ish).
- Close-call classifier unit tests around the threshold.
- Correctness back-labeling test: given resolution, all 9 snapshots get correct `was_correct` per dominant-side definitions (mid/ask/last-trade variants).

**Acceptance:** 3 consecutive days with 100% of ended markets resolved and labeled; close-call rate reported; snapshot correctness columns fully populated.

**Heads-ups:**
- Resolution is `end ≥ start → UP` — encode the tie rule exactly; it's a real (tiny) asymmetry.
- Never infer outcome from final Polymarket price alone (a 0.99 book can still be wrong on oracle ticks); use the platform's resolved outcome as truth.
- Close-call markets are **quarantined from strategy stats** but kept for a dedicated close-call study — they're where proxy error concentrates.

---

## Phase 5 — Feature Tables + Parquet Export

**Goal:** analysis-ready datasets, reproducible from raw.

**Build:**
- Daily exporter: `snapshots_features` (snapshot ⨝ resolution ⨝ market ⨝ fees), `btc_1s_bars`, `trade_prints`, `resolutions` → `/data/parquet/dt=.../*.parquet` (zstd), with export manifest + row counts.
- `feature_version` stamping; replay tool: rebuild any day's features from raw JSONL and diff against DB (bit-for-bit on numeric tolerance).
- duckdb views file for ad-hoc analysis.

**Packages:** `polars`, `pyarrow`, `duckdb`.

**Tests:** export/rowcount reconciliation test; replay-diff test on one recorded day; schema evolution test (new column doesn't break old readers).

**Acceptance:** yesterday's full day exports in < 5 min; replay diff clean; duckdb can answer "accuracy by label for last 7d" in one query.

**Heads-ups:** partition by date only (cardinality is small); never mutate exported partitions — re-export whole day on correction and bump manifest version.

---

## Phase 6 — Analytics Engine (calibration, EV, FDR, walk-forward, maker fills)

**Goal:** the science. Hourly/daily/weekly jobs producing decision-grade tables.

**Build:**
- Hourly job per `mcp_plan.md` §6.1: calibration bins, Wilson CIs, Brier/log-loss, net-EV taker/maker, BH-FDR pass flags → `calibration_bins`, `timestamp_performance`, `analysis_runs`. Every metric emitted at `scope=total` AND per session/overlap on the regular-integrity population; holiday/half-day/weekend instances aggregated as separate buckets (per `mcp_plan.md` §6.0).
- Fee engine module: exact fee curve from docs/market info (`fee ≈ rate·shares·price·(1−price)` — **verify against live market info + a real quote before trusting**), versioned `FEE_MODEL_VERSION`.
- Maker fill model: for hypothetical posts at {join-bid, mid−1tick, mid} at each label, scan `trade_prints` forward → `p_fill`, time-to-fill quantiles by label/regime → `maker_fill_estimates` (weekly refit, daily refresh).
- Regime labeler: quantile rules over σ, intensity, spread (start rules-based; versioned).
- Walk-forward evaluator (weekly): 14d train / 3d validate rolling; per-candidate stability report.
- Daily report generator (markdown → DB + Telegram push).

**Packages:** `statsmodels` (Wilson via `proportion_confint`, BH via `multipletests`), `scipy`.

**Tests:**
- Wilson CI + BH-FDR against hand-computed references.
- Fee math property tests: symmetric around 0.5; zero at 0/1; matches captured real fill examples once live data exists.
- Maker fill model test on synthetic print streams with known fill truth.
- Determinism test: same inputs + same versions → identical run output (hash summary_json).
- Leakage test: walk-forward windows share zero market_ids.
- Session-scope test: seeded data with a planted NY-only edge → detected in the NY bucket, diluted-or-absent at total scope; holiday-labeled rows verifiably excluded from the regular NY bucket and present in the holiday bucket.

**Acceptance:** hourly job < 2 min runtime; a seeded synthetic dataset with a planted 5¢ bucket edge is detected (and survives FDR) while a null dataset produces zero FDR-passing bins across 100 simulated runs (≤ expected false rate).

**Heads-ups:**
- The null-dataset test is the most important test in the whole project. If your pipeline "finds edges" in noise, everything downstream is theater.
- Report **CI-lower net EV** everywhere the point estimate appears; UI/agents must never see one without the other.
- Keep the contrarian direction in every table (edge for buying the *underdog* = (1−win_rate) − (1−price) mirror); it's free and it's a live hypothesis.

---

## Phase 7 — Strategy Candidate Registry

**Goal:** versioned candidates with statuses and enforced gates (v1 §13/§14 + v2 gates).

**Build:**
- Candidate extractor (daily): FDR-passing, walk-forward-passing bin families → `strategy_candidates` (`research_only`), full evidence attached (`entry_style`, `direction`, filters, n, CIs, net EVs, liquidity stats).
- Gate evaluator: computes pass/fail per promotion gate (`mcp_plan.md` §6.4); **status changes above `research_only` require a manual CLI/flag action** — the engine recommends, you promote.
- Champion/challenger comparator over paper telemetry (ingested from bot).
- Candidate lifecycle audit log.

**Tests:** gate logic table-driven tests (every gate boundary); no-auto-promotion test (engine may never write `paper_only`+ without the manual flag); audit completeness test.

**Acceptance:** end-to-end: planted-edge synthetic data → candidate appears with correct evidence → manual promote to `paper_only` works → disable works and sticks.

**Heads-ups:** candidate IDs are immutable; parameter changes create a new version, never mutate. Your future self debugging a paper anomaly will thank you.

---

## Phase 8 — REST Facade + Ingest API

**Goal:** the bot's data plane.

**Build:**
- FastAPI app with endpoints per `mcp_plan.md` §7.1; bearer auth (separate read vs ingest token scopes, hashed in `ingest_tokens`); every response includes freshness metadata; orjson responses; simple per-token rate limit.
- Ingest handlers → `paper_trades`, `bot_decision_logs`, heartbeats; idempotency via client-supplied UUIDs.
- OpenAPI schema published; typed client stub generated for the bot repo.

**Packages:** `fastapi`, `uvicorn`.

**Tests:** auth matrix (no token / wrong scope / valid); idempotent ingest (duplicate UUID = 200, no dup row); freshness fields correctness; contract tests pinned to OpenAPI schema (bot repo consumes the same schema).

**Acceptance:** p95 read latency < 50 ms on the overlay network; ingest survives 10k-record backfill; unauthorized requests logged + alerted on repeat.

**Heads-ups:** bind to WireGuard/Tailscale IP only. The ingest API is your only write surface — keep its schema boring and strict (pydantic, reject unknown fields).

---

## Phase 9 — MCP Server

**Goal:** agent-facing intelligence per v1 §15 + v2 tools.

**Build:**
- FastMCP app (official `mcp` python SDK), Streamable HTTP transport, bearer auth; tools + resources per `mcp_plan.md` §7.2, all delegating to the same service layer as REST.
- Tool docstrings written for LLM consumption (inputs, units, caveats, "this is evidence, not advice" framing).
- Response envelope: freshness, window, n, CI, warnings, `current_session` + `session_integrity` — enforced by a shared decorator. Performance tools return total + per-session scopes side by side by default.

**Packages:** `mcp`.

**Tests:** MCP client integration test (SDK client calls every tool against a seeded DB); envelope-enforcement test (a tool missing freshness fields fails CI); read-only test (no tool can reach a write path — assert at the service layer).

**Acceptance:** Claude Desktop / an agent connects over the overlay network, lists tools, and answers "what's the current champion and its CI-lower net EV?" correctly from seeded data.

**Heads-ups:** Use the **latest** MCP spec revision at build time (transport/auth details have evolved across 2025 revisions); pin SDK version. Keep tool count modest and composable — agents do better with 12 well-described tools than 40 thin ones.

---

## Phase 10 — Ops Hardening + Failure Drills

**Goal:** it survives without you.

**Build:**
- Full systemd unit set + timers (v1 §21.2 + rest/mcp services); watchdog integration (`WatchdogSec` + `sd_notify`).
- Disk/retention monitors; backup restore runbook (actually restore once); log rotation.
- Chaos drills: kill Postgres, kill each WS, fill disk to 85%, skew clock — each must alert + recover or fail safe.
- Ops README: runbooks for the top 10 failure modes.

**Acceptance:** 7-day unattended soak with ≥ 98% snapshot capture, zero silent failures (every incident produced a Telegram alert), successful backup restore on a scratch database.

**Heads-up:** this phase is what makes the difference between "one month of paper data" and "one month of holes you discover in week 5." Do not skip the drills.

---

## Cross-Phase Requirements

- **CI:** GitHub Actions — lint (ruff), typecheck (mypy/pyright), full test suite with testcontainers Postgres, golden-fixture contract tests. No merge on red.
- **Fixtures policy:** every external payload shape gets a recorded fixture the day you first see it; parser changes require fixture-diff review.
- **Versioning discipline:** `collector_version`, `feature_version`, `fee_model_version`, `regime_model_version` stamped on every relevant row. Analyses are only comparable within versions.
- **Definition of done for the whole system:** Phase 10 soak passed **and** the null-dataset FDR test passing in CI. Then the bot (see `bot_phases.md`) starts its 30-day paper campaign.
