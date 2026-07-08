"""Daily report generator (markdown → DB resource + Telegram push)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from ..db.models import AnalysisRun, CalibrationBin, StrategyCandidate, SystemHealthEvent


def generate_daily_report(session_factory, run_id: str | None = None, as_of: dt.datetime | None = None) -> str:
    as_of = as_of or dt.datetime.now(dt.UTC)
    lines: list[str] = [f"# PMRE Daily Report — {as_of.date().isoformat()}", ""]

    with session_factory() as s:
        if run_id is None:
            run = s.execute(
                select(AnalysisRun).order_by(AnalysisRun.started_at.desc()).limit(1)
            ).scalar_one_or_none()
        else:
            run = s.execute(select(AnalysisRun).where(AnalysisRun.run_id == run_id)).scalar_one_or_none()

        lines.append("## Latest analysis run")
        if run is not None:
            lines.append(f"- run_id: `{run.run_id}` ({run.kind})")
            summ = run.summary_json or {}
            lines.append(f"- observations: {summ.get('n_obs', 'n/a')}")
            lines.append(f"- calibration bins: {summ.get('n_bins', 'n/a')} "
                         f"(FDR-passing: {summ.get('n_fdr_pass_bins', 0)})")
            lines.append(f"- fee_model_version: {run.fee_model_version}")
        else:
            lines.append("- (no runs yet)")
        lines.append("")

        lines.append("## Top calibration deviations (FDR-passing)")
        bins = []
        if run is not None:
            bins = s.execute(
                select(CalibrationBin)
                .where(CalibrationBin.run_id == run.run_id, CalibrationBin.fdr_pass.is_(True))
                .order_by(CalibrationBin.edge.desc())
                .limit(10)
            ).scalars().all()
        if bins:
            lines.append("| label | bin | scope | n | win_rate | avg_price | edge |")
            lines.append("|---|---|---|---|---|---|---|")
            for b in bins:
                lines.append(
                    f"| {b.label} | {b.price_bin_lo:.2f}-{b.price_bin_hi:.2f} | {b.scope} | "
                    f"{b.n} | {b.win_rate:.3f} | {b.avg_entry_price:.3f} | {b.edge:+.3f} |"
                )
        else:
            lines.append("- none passed FDR this run (this is the expected default).")
        lines.append("")

        cands = s.execute(select(StrategyCandidate)).scalars().all()
        lines.append(f"## Candidates: {len(cands)}")
        for c in cands[:20]:
            lines.append(f"- `{c.candidate_id}` [{c.status}] {c.label} {c.scope} "
                         f"net_ev_ci_lo={c.net_ev_ci_lower_95}")
        lines.append("")

        since = as_of - dt.timedelta(days=1)
        criticals = s.execute(
            select(SystemHealthEvent).where(
                SystemHealthEvent.severity.in_(["warning", "critical"]),
                SystemHealthEvent.ts >= since,
            )
        ).scalars().all()
        lines.append(f"## Health events (24h): {len(criticals)}")
        for e in criticals[:15]:
            lines.append(f"- [{e.severity}] {e.service}: {e.message}")

    return "\n".join(lines)
