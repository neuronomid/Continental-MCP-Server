"""Shared, read-only research service (consumed by BOTH the REST and MCP facades).

Every method returns plain ``data`` (no envelope) plus, where relevant, an
``evidence`` block (n, CI, window). No method here can reach a trade path — this
is the single choke-point the "read-only" test asserts against.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from pm_sessions import current_session, seconds_to_next_boundary

from ..config import Settings
from ..db.models import (
    AnalysisRun,
    CalibrationBin,
    FeeSchedule,
    Market,
    MarketToken,
    Snapshot,
    StrategyCandidate,
    SystemHealthEvent,
    TimestampPerformance,
)


class ResearchService:
    def __init__(self, session_factory, settings: Settings | None = None):
        self.session_factory = session_factory
        self.settings = settings or Settings()

    # --- freshness ---------------------------------------------------------
    def last_update_ts(self) -> dt.datetime | None:
        with self.session_factory() as s:
            snap = s.execute(
                select(func.max(Snapshot.captured_at))
            ).scalar_one_or_none()
            hb = s.execute(select(func.max(SystemHealthEvent.ts))).scalar_one_or_none()
        candidates = [t for t in (snap, hb) if t is not None]
        return max(candidates) if candidates else None

    # --- health / session -------------------------------------------------
    def get_system_health(self, max_silence_s: float = 180.0) -> dict:
        now = dt.datetime.now(dt.UTC)
        with self.session_factory() as s:
            rows = s.execute(
                select(
                    SystemHealthEvent.service,
                    func.max(SystemHealthEvent.ts),
                ).group_by(SystemHealthEvent.service)
            ).all()
            recent_critical = s.execute(
                select(SystemHealthEvent)
                .where(SystemHealthEvent.severity.in_(["warning", "critical"]))
                .order_by(SystemHealthEvent.ts.desc())
                .limit(10)
            ).scalars().all()
        services = {}
        for service, last_ts in rows:
            silent = last_ts is None or (now - last_ts).total_seconds() > max_silence_s
            services[service] = {
                "last_seen": last_ts.isoformat() if last_ts else None,
                "silent": silent,
            }
        return {
            "status": "degraded" if any(v["silent"] for v in services.values()) else "ok",
            "services": services,
            "recent_incidents": [
                {"service": e.service, "severity": e.severity, "message": e.message,
                 "ts": e.ts.isoformat()}
                for e in recent_critical
            ],
        }

    def get_current_session(self) -> dict:
        now = dt.datetime.now(dt.UTC)
        lbl = current_session()
        secs, nxt = seconds_to_next_boundary(now)
        d = lbl.as_dict()
        d["seconds_to_next_boundary"] = secs
        d["next_primary"] = nxt
        return d

    # --- markets ----------------------------------------------------------
    def get_current_btc5m_market(self, now: dt.datetime | None = None) -> dict | None:
        now = now or dt.datetime.now(dt.UTC)
        with self.session_factory() as s:
            active = s.execute(
                select(Market)
                .where(Market.expected_resolution_time_utc > now)
                .order_by(Market.expected_resolution_time_utc.asc())
                .limit(2)
            ).scalars().all()
            out = []
            for m in active:
                tokens = s.execute(
                    select(MarketToken).where(MarketToken.market_id == m.id)
                ).scalars().all()
                out.append(self._market_dict(m, tokens))
        if not out:
            return None
        return {"active": out[0], "next": out[1] if len(out) > 1 else None}

    @staticmethod
    def _market_dict(m: Market, tokens) -> dict:
        return {
            "market_id": m.id,
            "slug": m.slug,
            "condition_id": m.condition_id,
            "price_to_beat": m.price_to_beat,
            "price_to_beat_source": m.price_to_beat_source,
            "fees_enabled": m.fees_enabled,
            "fee_rate_bps": m.fee_rate_bps,
            "tick_size": m.tick_size,
            "min_order_size": m.min_order_size,
            "window_start_utc": m.window_start_utc.isoformat() if m.window_start_utc else None,
            "expected_resolution_time_utc": (
                m.expected_resolution_time_utc.isoformat() if m.expected_resolution_time_utc else None
            ),
            "tokens": [{"token_id": t.token_id, "outcome": t.outcome} for t in tokens],
        }

    def get_latest_market_snapshot(self, market_id: int | None = None, label: str | None = None) -> dict | None:
        with self.session_factory() as s:
            q = select(Snapshot)
            if market_id is not None:
                q = q.where(Snapshot.market_id == market_id)
            if label is not None:
                q = q.where(Snapshot.label == label)
            snap = s.execute(q.order_by(Snapshot.captured_at.desc()).limit(1)).scalar_one_or_none()
            if snap is None:
                return None
            return self._snapshot_dict(snap)

    @staticmethod
    def _snapshot_dict(snap: Snapshot) -> dict:
        return {
            "market_id": snap.market_id, "label": snap.label,
            "captured_at": snap.captured_at.isoformat(),
            "snapshot_actual_seconds_left": snap.snapshot_actual_seconds_left,
            "dominant_side": snap.dominant_side, "dominant_mid": snap.dominant_mid,
            "dominant_ask": snap.dominant_ask, "market_spread_proxy": snap.market_spread_proxy,
            "max_usd_buy_within_2c": snap.max_usd_buy_within_2c,
            "taker_fee_est_dominant": snap.taker_fee_est_dominant,
            "p_fair": snap.p_fair, "model_edge": snap.model_edge, "z_score": snap.z_score,
            "sigma_1s": snap.sigma_1s, "session_primary": snap.session_primary,
            "session_integrity": snap.session_integrity,
            "stale_book_flag": snap.stale_book_flag, "crossed_book_flag": snap.crossed_book_flag,
        }

    def get_fee_parameters(self, market_id: int) -> dict | None:
        with self.session_factory() as s:
            m = s.get(Market, market_id)
            if m is None:
                return None
            latest = s.execute(
                select(FeeSchedule).where(FeeSchedule.market_id == market_id)
                .order_by(FeeSchedule.captured_at.desc()).limit(1)
            ).scalar_one_or_none()
            return {
                "market_id": market_id,
                "fees_enabled": m.fees_enabled,
                "fee_rate_bps": m.fee_rate_bps,
                "fee_model_version": self.settings.fee_model_version,
                "history_latest": {
                    "captured_at": latest.captured_at.isoformat(),
                    "fee_rate_bps": latest.fee_rate_bps,
                } if latest else None,
            }

    def get_fair_value_snapshot(self, market_id: int, label: str | None = None) -> dict | None:
        snap = self.get_latest_market_snapshot(market_id, label)
        if snap is None:
            return None
        return {
            "market_id": market_id, "label": snap["label"],
            "p_fair": snap["p_fair"], "model_edge": snap["model_edge"],
            "z_score": snap["z_score"], "sigma_1s": snap["sigma_1s"],
            "dominant_mid": snap["dominant_mid"],
        }

    # --- performance (latest run) -----------------------------------------
    def _latest_run(self, s, kind: str | None = None) -> AnalysisRun | None:
        q = select(AnalysisRun).order_by(AnalysisRun.started_at.desc()).limit(1)
        if kind:
            q = select(AnalysisRun).where(AnalysisRun.kind == kind).order_by(
                AnalysisRun.started_at.desc()).limit(1)
        return s.execute(q).scalar_one_or_none()

    def get_timestamp_performance(
        self, entry_style: str | None = None, scope: str | None = None,
        label: str | None = None, run_id: str | None = None,
    ) -> dict:
        with self.session_factory() as s:
            run = (s.execute(select(AnalysisRun).where(AnalysisRun.run_id == run_id)).scalar_one_or_none()
                   if run_id else self._latest_run(s))
            if run is None:
                return {"run_id": None, "rows": []}
            q = select(TimestampPerformance).where(TimestampPerformance.run_id == run.run_id)
            if entry_style:
                q = q.where(TimestampPerformance.entry_style == entry_style)
            if scope:
                q = q.where(TimestampPerformance.scope == scope)
            if label:
                q = q.where(TimestampPerformance.label == label)
            rows = s.execute(q).scalars().all()
            return {
                "run_id": run.run_id,
                "rows": [self._perf_dict(r) for r in rows],
            }

    @staticmethod
    def _perf_dict(r: TimestampPerformance) -> dict:
        return {
            "label": r.label, "scope": r.scope,
            "session_integrity_filter": r.session_integrity_filter,
            "entry_style": r.entry_style, "direction": r.direction, "regime": r.regime,
            "n": r.n, "win_rate": r.win_rate, "avg_price": r.avg_price,
            "edge_vs_price": r.edge_vs_price, "net_ev_taker": r.net_ev_taker,
            "net_ev_maker": r.net_ev_maker, "net_ev_ci_lower_95": r.net_ev_ci_lower_95,
            "brier": r.brier, "log_loss": r.log_loss, "fdr_pass": r.fdr_pass,
        }

    def get_calibration_curve(self, label: str | None = None, scope: str = "total",
                              run_id: str | None = None) -> dict:
        with self.session_factory() as s:
            run = (s.execute(select(AnalysisRun).where(AnalysisRun.run_id == run_id)).scalar_one_or_none()
                   if run_id else self._latest_run(s))
            if run is None:
                return {"run_id": None, "bins": []}
            q = select(CalibrationBin).where(
                CalibrationBin.run_id == run.run_id, CalibrationBin.scope == scope
            )
            if label:
                q = q.where(CalibrationBin.label == label)
            rows = s.execute(q.order_by(CalibrationBin.price_bin_lo.asc())).scalars().all()
            return {
                "run_id": run.run_id,
                "bins": [
                    {"label": b.label, "price_bin_lo": b.price_bin_lo, "price_bin_hi": b.price_bin_hi,
                     "scope": b.scope, "session_integrity_filter": b.session_integrity_filter,
                     "n": b.n, "win_rate": b.win_rate, "avg_entry_price": b.avg_entry_price,
                     "wilson_lo": b.wilson_lo, "wilson_hi": b.wilson_hi, "edge": b.edge,
                     "p_value": b.p_value, "fdr_pass": b.fdr_pass}
                    for b in rows
                ],
            }

    def get_session_performance(self, label: str | None = None) -> dict:
        return self._performance_by_prefix("session:", label)

    def get_regime_performance(self, label: str | None = None) -> dict:
        with self.session_factory() as s:
            run = self._latest_run(s)
            if run is None:
                return {"run_id": None, "rows": []}
            q = select(TimestampPerformance).where(
                TimestampPerformance.run_id == run.run_id,
                TimestampPerformance.regime.isnot(None),
            )
            if label:
                q = q.where(TimestampPerformance.label == label)
            rows = s.execute(q).scalars().all()
            return {"run_id": run.run_id, "rows": [self._perf_dict(r) for r in rows]}

    def _performance_by_prefix(self, prefix: str, label: str | None) -> dict:
        with self.session_factory() as s:
            run = self._latest_run(s)
            if run is None:
                return {"run_id": None, "rows": []}
            q = select(TimestampPerformance).where(
                TimestampPerformance.run_id == run.run_id,
                TimestampPerformance.scope.like(f"{prefix}%"),
            )
            if label:
                q = q.where(TimestampPerformance.label == label)
            rows = s.execute(q).scalars().all()
            return {"run_id": run.run_id, "rows": [self._perf_dict(r) for r in rows]}

    def get_maker_fill_estimates(self, label: str | None = None, offset: int | None = None) -> dict:
        from ..db.models import MakerFillEstimate

        with self.session_factory() as s:
            q = select(MakerFillEstimate)
            if label:
                q = q.where(MakerFillEstimate.snapshot_label == label)
            if offset is not None:
                q = q.where(MakerFillEstimate.offset_ticks == offset)
            rows = s.execute(q).scalars().all()
            return {"rows": [
                {"snapshot_label": r.snapshot_label, "post_style": r.post_style,
                 "regime": r.regime, "p_fill": r.p_fill, "median_ttf_s": r.median_ttf_s,
                 "sample_size": r.sample_size}
                for r in rows
            ]}

    # --- registry ----------------------------------------------------------
    def get_strategy_candidates(self, status: str | None = None) -> dict:
        with self.session_factory() as s:
            q = select(StrategyCandidate)
            if status:
                q = q.where(StrategyCandidate.status == status)
            rows = s.execute(q).scalars().all()
            return {"candidates": [self._candidate_dict(c) for c in rows]}

    def get_champion_strategy(self) -> dict | None:
        with self.session_factory() as s:
            c = s.execute(
                select(StrategyCandidate).where(StrategyCandidate.status == "champion").limit(1)
            ).scalar_one_or_none()
            return self._candidate_dict(c) if c else None

    @staticmethod
    def _candidate_dict(c: StrategyCandidate) -> dict:
        return {
            "candidate_id": c.candidate_id, "version": c.version, "status": c.status,
            "label": c.label, "scope": c.scope, "entry_style": c.entry_style,
            "direction": c.direction, "price_bin_lo": c.price_bin_lo, "price_bin_hi": c.price_bin_hi,
            "regime": c.regime, "n": c.n, "win_rate": c.win_rate,
            "net_ev": c.net_ev, "net_ev_ci_lower_95": c.net_ev_ci_lower_95, "edge": c.edge,
            "median_liquidity_usd": c.median_liquidity_usd, "fdr_pass": c.fdr_pass,
            "walk_forward_pass": c.walk_forward_pass, "fee_model_version": c.fee_model_version,
        }

    def get_paper_trade_performance(self, candidate_id: str | None = None) -> dict:
        from ..db.models import PaperTrade

        with self.session_factory() as s:
            q = select(PaperTrade)
            if candidate_id:
                q = q.where(PaperTrade.candidate_id == candidate_id)
            trades = s.execute(q).scalars().all()
        n = len(trades)
        pnl = sum(t.pnl or 0.0 for t in trades)
        wins = sum(1 for t in trades if (t.pnl or 0.0) > 0)
        return {
            "candidate_id": candidate_id, "n_trades": n, "net_pnl": pnl,
            "win_rate": (wins / n) if n else None,
        }

    def get_analysis_run_summary(self, run_id: str | None = None) -> dict | None:
        with self.session_factory() as s:
            run = (s.execute(select(AnalysisRun).where(AnalysisRun.run_id == run_id)).scalar_one_or_none()
                   if run_id else self._latest_run(s))
            if run is None:
                return None
            return {
                "run_id": run.run_id, "kind": run.kind,
                "started_at": run.started_at.isoformat(),
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "n_markets": run.n_markets, "summary": run.summary_json,
                "summary_hash": run.summary_hash, "status": run.status,
                "fee_model_version": run.fee_model_version,
            }

    def get_data_quality_report(self, window_hours: int = 24) -> dict:
        since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=window_hours)
        with self.session_factory() as s:
            total = s.execute(
                select(func.count()).select_from(Snapshot).where(Snapshot.captured_at >= since)
            ).scalar_one()
            stale = s.execute(
                select(func.count()).select_from(Snapshot).where(
                    Snapshot.captured_at >= since, Snapshot.stale_book_flag.is_(True))
            ).scalar_one()
            crossed = s.execute(
                select(func.count()).select_from(Snapshot).where(
                    Snapshot.captured_at >= since, Snapshot.crossed_book_flag.is_(True))
            ).scalar_one()
            close_calls = s.execute(
                select(func.count()).select_from(Snapshot).where(
                    Snapshot.captured_at >= since, Snapshot.was_close_call.is_(True))
            ).scalar_one()
        return {
            "window_hours": window_hours, "snapshots": total,
            "stale_book": stale, "crossed_book": crossed, "close_calls": close_calls,
            "stale_rate": (stale / total) if total else None,
        }
