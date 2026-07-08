"""Resolution collector + oracle reconciliation.

Truth is the platform's *resolved outcome* (never inferred from a final price —
a 0.99 book can still be wrong on the oracle tick). The tie rule is encoded
exactly: ``end ≥ start → UP`` (mcp_plan.md §1.2 / mcp_phases.md Phase 4).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select

from .discovery import _maybe_json_list, _normalize_outcome, _parse_dt


@dataclass
class ParsedResolution:
    resolved: bool
    winning_outcome: str | None  # UP | DOWN
    resolved_at: dt.datetime | None
    source: str = "gamma"


def parse_resolution(raw: dict) -> ParsedResolution:
    """Determine the winning outcome from a resolved Gamma market object."""
    closed = bool(raw.get("closed", False))
    outcomes = _maybe_json_list(raw.get("outcomes"))
    prices = _maybe_json_list(raw.get("outcomePrices") or raw.get("outcome_prices"))
    resolved_at = _parse_dt(raw.get("resolvedDate") or raw.get("endDate"))

    winner = None
    if raw.get("winningOutcome"):
        winner = _normalize_outcome(raw["winningOutcome"])
    elif outcomes and prices and len(outcomes) == len(prices):
        try:
            fprices = [float(p) for p in prices]
        except (TypeError, ValueError):
            fprices = []
        if fprices:
            idx = max(range(len(fprices)), key=lambda i: fprices[i])
            # A genuine resolution has a clear 1/0 split.
            if fprices[idx] >= 0.99:
                winner = _normalize_outcome(outcomes[idx])

    resolved = closed and winner is not None
    return ParsedResolution(
        resolved=resolved, winning_outcome=winner, resolved_at=resolved_at
    )


def expected_outcome_from_proxy(proxy_end: float, price_to_beat: float) -> str:
    """Tie rule: end ≥ start → UP."""
    return "UP" if proxy_end >= price_to_beat else "DOWN"


def classify_close_call(
    proxy_end: float, price_to_beat: float, threshold_bps: float = 2.0
) -> tuple[float, bool, bool]:
    """Return ``(signed_margin_bps, was_close_call, tie_rule_applied)``."""
    signed_margin_bps = (proxy_end - price_to_beat) / price_to_beat * 1e4
    was_close_call = abs(signed_margin_bps) < threshold_bps
    tie_rule_applied = abs(proxy_end - price_to_beat) < 1e-9
    return signed_margin_bps, was_close_call, tie_rule_applied


def _opposite(side: str) -> str:
    return "DOWN" if side == "UP" else "UP"


def _favored_by_mid(up_mid, down_mid) -> str | None:
    if up_mid is None and down_mid is None:
        return None
    if down_mid is None:
        return "UP"
    if up_mid is None:
        return "DOWN"
    return "UP" if up_mid >= down_mid else "DOWN"


def _favored_by_ask(up_ask, down_ask) -> str | None:
    if up_ask is None and down_ask is None:
        return None
    if down_ask is None:
        return "UP"
    if up_ask is None:
        return "DOWN"
    return "UP" if up_ask >= down_ask else "DOWN"


class ResolutionService:
    def __init__(self, session_factory, health=None, close_call_bps: float = 2.0):
        self.session_factory = session_factory
        self.health = health
        self.close_call_bps = close_call_bps

    def resolve(
        self,
        market_id: int,
        parsed: ParsedResolution,
        proxy_end: float | None = None,
        price_to_beat: float | None = None,
        oracle_end: float | None = None,
    ) -> int:
        from ..db.models import Market, MarketResolution, MarketToken

        if not parsed.resolved or parsed.winning_outcome is None:
            raise ValueError("cannot resolve: market not resolved / no winner")

        with self.session_factory() as s:
            market = s.get(Market, market_id)
            if price_to_beat is None:
                price_to_beat = market.price_to_beat if market else None

            margin_bps = None
            was_close = False
            tie_applied = False
            proxy_oracle_div = None
            if proxy_end is not None and price_to_beat:
                margin_bps, was_close, tie_applied = classify_close_call(
                    proxy_end, price_to_beat, self.close_call_bps
                )
            if oracle_end is not None and proxy_end is not None and price_to_beat:
                proxy_oracle_div = (proxy_end - oracle_end) / price_to_beat * 1e4

            existing = s.execute(
                select(MarketResolution).where(MarketResolution.market_id == market_id)
            ).scalar_one_or_none()
            if existing is None:
                existing = MarketResolution(market_id=market_id)
                s.add(existing)
            existing.winning_outcome = parsed.winning_outcome
            # Always stamp a resolution time so daily exports partition correctly.
            existing.resolved_at = (
                parsed.resolved_at
                or (market.expected_resolution_time_utc if market else None)
                or dt.datetime.now(dt.UTC)
            )
            existing.resolution_source = parsed.source
            existing.price_to_beat = price_to_beat
            existing.proxy_end_price = proxy_end
            existing.oracle_end_price = oracle_end
            existing.proxy_oracle_divergence_bps = proxy_oracle_div
            existing.margin_bps = margin_bps
            existing.was_close_call = was_close
            existing.tie_rule_applied = tie_applied

            # label tokens
            for tok in s.execute(
                select(MarketToken).where(MarketToken.market_id == market_id)
            ).scalars():
                tok.is_winner = tok.outcome == parsed.winning_outcome
            if market is not None:
                market.closed = True
                market.active = False

            rid = existing.market_id
            s.flush()
            self._backlabel(s, market_id, parsed.winning_outcome, was_close)
            s.commit()
            return rid

    def _backlabel(self, s, market_id: int, winner: str, was_close: bool) -> None:
        from ..db.models import Snapshot

        for snap in s.execute(
            select(Snapshot).where(Snapshot.market_id == market_id)
        ).scalars():
            fav_mid = _favored_by_mid(snap.up_mid, snap.down_mid)
            fav_ask = _favored_by_ask(snap.up_best_ask, snap.down_best_ask)
            fav_lt = None
            if snap.dominant_side is not None:
                if snap.last_trade_price is None or snap.last_trade_price >= 0.5:
                    fav_lt = snap.dominant_side
                else:
                    fav_lt = _opposite(snap.dominant_side)
            snap.was_correct_mid = None if fav_mid is None else fav_mid == winner
            snap.was_correct_ask = None if fav_ask is None else fav_ask == winner
            snap.was_correct_last_trade = None if fav_lt is None else fav_lt == winner
            snap.was_close_call = was_close

    def unresolved_overdue(
        self, now: dt.datetime | None = None, grace_minutes: float = 10.0
    ) -> list[int]:
        """Market ids whose end + grace has passed but which have no resolution."""
        from ..db.models import Market, MarketResolution

        now = now or dt.datetime.now(dt.UTC)
        deadline = now - dt.timedelta(minutes=grace_minutes)
        overdue: list[int] = []
        with self.session_factory() as s:
            resolved_ids = {
                r.market_id for r in s.execute(select(MarketResolution)).scalars()
            }
            for m in s.execute(select(Market)).scalars():
                end = m.expected_resolution_time_utc
                if end is not None and end < deadline and m.id not in resolved_ids:
                    overdue.append(m.id)
        if overdue and self.health:
            self.health.warning(
                "resolution", f"{len(overdue)} markets unresolved past grace", {"ids": overdue[:20]}
            )
        return overdue
