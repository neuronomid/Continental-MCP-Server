"""Phase 9 — MCP: every tool callable, envelope enforced, read-only, bearer auth."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import func, select

from pmre.analytics.runner import HourlyAnalytics
from pmre.config import Settings
from pmre.db.models import (
    Market,
    MarketResolution,
    Snapshot,
    StrategyCandidate,
)
from pmre.serving.auth import verify_mcp_token
from pmre.serving.envelope import REQUIRED_ENVELOPE_KEYS
from pmre.serving.mcp.server import create_mcp_server


@pytest.fixture
def seeded_db(db):
    with db.session() as s:
        for i in range(60):
            m = Market(slug=f"btc-updown-5m-{i}", fee_rate_bps=72.0,
                       price_to_beat=108000.0, fees_enabled=True, tick_size=0.001,
                       expected_resolution_time_utc=dt.datetime(2099, 1, 1, tzinfo=dt.UTC))
            s.add(m)
            s.flush()
            won = i % 3 != 0
            s.add(MarketResolution(market_id=m.id, winning_outcome="UP" if won else "DOWN"))
            s.add(Snapshot(market_id=m.id, label="t_240", target_seconds_left=240,
                           captured_at=dt.datetime(2026, 7, 7, 18, 1, tzinfo=dt.UTC),
                           dominant_side="UP", dominant_mid=0.62, dominant_ask=0.63,
                           up_best_bid=0.61, was_correct_mid=won,
                           session_primary="new_york", session_integrity="regular",
                           max_usd_buy_within_2c=15.0))
        s.add(StrategyCandidate(candidate_id="champ-x", status="champion", label="t_240",
                                entry_style="taker_ask", net_ev_ci_lower_95=0.006, n=800))
        s.commit()
    HourlyAnalytics(db.session_factory).run(run_id="mcp-run")
    return db


async def _call(mcp, name, args=None):
    result = await mcp.call_tool(name, args or {})
    # FastMCP serialises dict returns to a TextContent JSON block.
    if isinstance(result, tuple):
        result = result[0]
    block = result[0]
    return json.loads(block.text)


async def test_every_tool_callable_and_enveloped(seeded_db):
    mcp = create_mcp_server(seeded_db.session_factory, Settings())
    tools = await mcp.list_tools()
    names = [t.name for t in tools]
    # the full §7.2 tool set is present
    expected = {
        "get_system_health", "get_current_session", "get_current_btc5m_market",
        "get_latest_market_snapshot", "get_timestamp_performance", "get_calibration_curve",
        "get_session_performance", "get_regime_performance", "get_fee_parameters",
        "get_fair_value_snapshot", "get_maker_fill_estimates", "get_strategy_candidates",
        "get_champion_strategy", "get_paper_trade_performance", "get_analysis_run_summary",
        "get_data_quality_report",
    }
    assert expected <= set(names)

    args = {
        "get_latest_market_snapshot": {"market_id": 1, "label": "t_240"},
        "get_fee_parameters": {"market_id": 1},
        "get_fair_value_snapshot": {"market_id": 1},
    }
    for name in expected:
        env = await _call(mcp, name, args.get(name, {}))
        # envelope enforced on EVERY tool
        assert REQUIRED_ENVELOPE_KEYS <= set(env.keys()), f"{name} missing envelope keys"
        assert env["session_model_version"].startswith("sessions-")


async def test_champion_answer_from_seeded_data(seeded_db):
    mcp = create_mcp_server(seeded_db.session_factory, Settings())
    env = await _call(mcp, "get_champion_strategy")
    assert env["data"]["candidate_id"] == "champ-x"
    assert env["data"]["net_ev_ci_lower_95"] == 0.006


async def test_calibration_curve_tool_returns_bins(seeded_db):
    mcp = create_mcp_server(seeded_db.session_factory, Settings())
    env = await _call(mcp, "get_calibration_curve", {"scope": "total"})
    assert env["data"]["run_id"] == "mcp-run"
    assert len(env["data"]["bins"]) >= 1


async def test_tools_are_read_only_no_db_mutation(seeded_db):
    mcp = create_mcp_server(seeded_db.session_factory, Settings())

    def counts():
        with seeded_db.session() as s:
            return (
                s.execute(select(func.count()).select_from(Snapshot)).scalar_one(),
                s.execute(select(func.count()).select_from(Market)).scalar_one(),
                s.execute(select(func.count()).select_from(StrategyCandidate)).scalar_one(),
            )

    before = counts()
    tools = await mcp.list_tools()
    for t in tools:
        try:
            await _call(mcp, t.name, {"market_id": 1} if "market" in t.name or "fee" in t.name or "fair" in t.name else {})
        except Exception:
            pass
    assert counts() == before  # no tool wrote anything


def test_read_only_service_has_no_write_methods():
    from pmre.serving.service import ResearchService

    method_names = [m for m in dir(ResearchService) if not m.startswith("_")]
    # No method whose leading verb implies a write path.
    write_verbs = {"ingest", "insert", "write", "create", "update", "delete",
                   "promote", "set", "save", "add", "remove", "drop", "commit"}
    offenders = [m for m in method_names if m.split("_")[0] in write_verbs]
    assert offenders == [], offenders


def test_bearer_verification():
    assert verify_mcp_token("secret", "secret") is True
    assert verify_mcp_token("wrong", "secret") is False
    assert verify_mcp_token(None, "secret") is False
    assert verify_mcp_token("secret", "") is False
