# Operations runbook — pm-research-engine

Research-only service. **No keys, no orders, no bot commands.** REST + MCP bind to
the private overlay IP (see `wireguard.md`). Every incident produces a durable
`system_health_events` row **and** a Telegram alert — there are no silent failures.

## Install (systemd)
```bash
sudo useradd -r -s /usr/sbin/nologin pmre
sudo git clone <repo> /opt/pm-research-engine && cd /opt/pm-research-engine
uv sync                                   # builds .venv
sudo cp .env.example .env && sudo edit .env   # fill secrets; PMRE_ENV=production
sudo -u pmre .venv/bin/python -m pmre migrate
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pmre@btc_feed pmre@clob_ws pmre@snapshotter \
     pmre@discovery pmre@resolution pmre-rest pmre-mcp
sudo systemctl enable --now pmre-analytics-hourly.timer pmre-analytics-daily.timer \
     pmre-analytics-weekly.timer pmre-backup.timer
```

## Health & verification
- `python -m pmre watchdog` — one-shot disk/health check.
- `GET /v1/health` (public) — collector liveness + recent incidents.
- `journalctl -u pmre@snapshotter -f` — live logs (structlog JSON).
- MCP: connect an agent over the overlay, list tools, ask "current champion + CI-lower net EV".

## Top-10 failure runbooks
1. **Postgres down** → collectors alert `critical`, systemd restarts them; `systemctl restart postgresql`; collectors reconnect. Verify: `/v1/health` returns `ok`.
2. **CLOB WS drop / reconnect storm** → auto-resubscribe with jittered backoff; books flagged `seq_gap` until a fresh full book arrives. Alert on >6 reconnects/h. No action unless storm persists (check Polymarket status).
3. **Snapshot miss rate > 2%/h** → watchdog warns. Check `clob_ws` heartbeat and clock drift; inspect `snapshots.stale_book_flag` rate.
4. **Clock drift > 250 ms** → snapshots refused + `critical`. `chronyc makestep`; verify `chronyc tracking` offset < 50 ms.
5. **Disk > 80% / 90%** → warn / critical. Prune `/data/raw` oldest JSONL (reproducible, offsite-excluded); confirm parquet + pg_dump retained.
6. **Analysis job failure** → `analysis_runs.status != ok` + alert. Re-run `python -m pmre analytics-hourly`; check versions match (fee/regime/feature).
7. **Resolution stuck (>10 min after end)** → `resolution` alert lists market ids. Check Gamma resolution status; markets stay in denominator for quality reporting.
8. **BTC feed divergence spike** → `btc_source_divergence_bps` flagged; one proxy likely stale. Never let both proxies reconnect simultaneously.
9. **Fee schedule drift** → new `fee_schedules` row + `FEE_MODEL_VERSION` bump; old EV numbers are only comparable within a version.
10. **Backup restore drill** → `deploy/backup.sh` nightly; quarterly, restore the latest dump into a scratch DB and run `pytest` smoke. A backup you never restored is not a backup.

## Chaos drills (Phase 10 acceptance)
Run each and confirm a Telegram alert + recovery/fail-safe:
`systemctl stop postgresql` · `systemctl kill pmre@clob_ws` · fill disk to 85% ·
`date -s` clock skew. The 7-day soak passes at ≥ 98% snapshot capture with zero
silent failures.

## Definition of done
Phase-10 soak passed **and** the null-dataset FDR test green in CI. Then the bot
begins its 30-day paper campaign.
