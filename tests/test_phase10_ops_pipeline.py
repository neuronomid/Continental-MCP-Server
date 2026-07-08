"""Phase 10 — watchdog/chaos alerts, backup restore, and the end-to-end pipeline."""

from __future__ import annotations

from sqlalchemy import select

from pmre.config import Settings
from pmre.db.engine import Database
from pmre.db.models import Market, StrategyCandidate, SystemHealthEvent
from pmre.demo import build_demo_dataset, run_full_pipeline
from pmre.ops.backup import pg_dump_command, sqlite_backup, sqlite_restore
from pmre.ops.health import HealthMonitor
from pmre.ops.watchdog import Watchdog, check_disk, is_reconnect_storm, snapshot_miss_rate


# --- watchdog checks ------------------------------------------------------
def test_snapshot_miss_rate_and_storm():
    assert abs(snapshot_miss_rate(100, 97) - 0.03) < 1e-9
    assert snapshot_miss_rate(0, 0) == 0.0
    assert is_reconnect_storm(10, 30) is True   # 20/hr > 6
    assert is_reconnect_storm(1, 60) is False


def test_check_disk_returns_pct():
    st = check_disk("/")
    assert 0.0 <= st.used_pct <= 100.0


def test_watchdog_miss_rate_alerts(db):
    hm = HealthMonitor(db.session_factory)
    wd = Watchdog(hm)
    wd.run_miss_rate_check(expected=100, actual=90, threshold=0.02)  # 10% miss
    with db.session() as s:
        ev = s.execute(select(SystemHealthEvent).where(SystemHealthEvent.service == "watchdog")).scalar_one()
        assert "miss rate" in ev.message


def test_watchdog_clock_skew_critical(db):
    hm = HealthMonitor(db.session_factory)
    wd = Watchdog(hm)
    assert wd.run_clock_check(offset_ms=500, abort_ms=250) is False
    with db.session() as s:
        ev = s.execute(select(SystemHealthEvent)).scalars().all()
        assert any("clock drift" in e.message for e in ev)


def test_watchdog_silence_check_alerts_dead_collector(db):
    hm = HealthMonitor(db.session_factory)
    hm.heartbeat("clob_ws")
    wd = Watchdog(hm)
    # snapshotter never sent a heartbeat → flagged silent + critical
    silent = wd.run_silence_check(["clob_ws", "snapshotter"], max_silence_s=180)
    assert silent == ["snapshotter"]
    with db.session() as s:
        crit = s.execute(select(SystemHealthEvent).where(SystemHealthEvent.severity == "critical")).scalars().all()
        assert any("snapshotter" in e.message for e in crit)


def test_watchdog_disk_alert_via_monkeypatch(db, monkeypatch):
    import pmre.ops.watchdog as wmod

    class FakeUsage:
        total = 100
        used = 92
        free = 8

    monkeypatch.setattr(wmod.shutil, "disk_usage", lambda p: FakeUsage())
    hm = HealthMonitor(db.session_factory)
    wd = Watchdog(hm)
    st = wd.run_disk_check("/", warn_pct=80, critical_pct=90)
    assert st.critical is True
    with db.session() as s:
        crit = s.execute(select(SystemHealthEvent).where(SystemHealthEvent.severity == "critical")).scalars().all()
        assert any("disk critical" in e.message for e in crit)


# --- backup / restore drill ----------------------------------------------
def test_pg_dump_command_builder():
    argv = pg_dump_command("postgresql+psycopg2://pmre:pw@db.local:5432/pmre", "/tmp/pmre.dump")
    assert argv[0] == "pg_dump"
    assert "-h" in argv and "db.local" in argv
    assert "-U" in argv and "pmre" in argv
    assert argv[-1] == "pmre"


def test_sqlite_backup_and_restore_roundtrip(tmp_path):
    src = str(tmp_path / "live.db")
    db = Database(f"sqlite+pysqlite:///{src}")
    db.create_all()
    with db.session() as s:
        s.add(Market(slug="btc-updown-5m-backup"))
        s.commit()
    # back up, then restore into a scratch DB and verify the row survives
    backup = sqlite_backup(src, str(tmp_path / "backup" / "pmre.bak"))
    scratch = sqlite_restore(backup, str(tmp_path / "scratch.db"))
    restored = Database(f"sqlite+pysqlite:///{scratch}")
    with restored.session() as s:
        m = s.execute(select(Market)).scalar_one()
        assert m.slug == "btc-updown-5m-backup"


# --- END-TO-END PIPELINE --------------------------------------------------
def test_full_pipeline_discovers_planted_edge_candidate(db):
    build_demo_dataset(db.session_factory, days=25)
    result = run_full_pipeline(db.session_factory, Settings())
    assert result["n_candidates"] >= 1, "planted edge should surface at least one candidate"
    with db.session() as s:
        cands = s.execute(select(StrategyCandidate)).scalars().all()
        assert all(c.status == "research_only" for c in cands)  # never auto-promoted
        # the planted bin is around 0.60 and must be among the candidates
        assert any(c.price_bin_lo is not None and abs(c.price_bin_lo - 0.60) < 1e-9 for c in cands)
        # every candidate carries positive CI-lower net EV evidence
        assert all(c.net_ev_ci_lower_95 is not None and c.net_ev_ci_lower_95 > 0 for c in cands)
        # and passed walk-forward
        assert all(c.walk_forward_pass for c in cands)


def test_full_pipeline_null_dataset_yields_no_candidates(db):
    # perfectly calibrated data (no planted edge) → no candidates
    build_demo_dataset(db.session_factory, days=25, planted_edge=0.0, seed=999)
    result = run_full_pipeline(db.session_factory, Settings())
    assert result["n_candidates"] == 0, "null dataset must not manufacture candidates"


def test_hourly_runtime_is_fast(db):
    build_demo_dataset(db.session_factory, days=10, markets_per_day=60)
    import time

    from pmre.analytics.runner import HourlyAnalytics
    t0 = time.time()
    HourlyAnalytics(db.session_factory, Settings()).run(fdr_min_n=200)
    assert time.time() - t0 < 120  # acceptance: hourly job < 2 min
