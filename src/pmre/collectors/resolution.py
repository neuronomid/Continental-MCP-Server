"""Resolution collector + oracle reconciliation.

Truth is the platform's *resolved outcome* (never inferred from a final price —
a 0.99 book can still be wrong on the oracle tick). The tie rule is encoded
exactly: ``end ≥ start → UP`` (mcp_plan.md §1.2 / mcp_phases.md Phase 4).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select

from ..logging_setup import get_logger
from .discovery import GammaClient, _maybe_json_list, _normalize_outcome, _parse_dt
from .slugs import parse_slug_start, start_dt


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


# --- live run-loop ---------------------------------------------------------
@dataclass
class _DueMarket:
    id: int
    slug: str
    resolution: dt.datetime
    window_start: dt.datetime | None
    price_to_beat: float | None


class ResolutionCollector:
    """Periodically reconciles finished windows against the platform's resolution.

    Truth is the resolved Gamma outcome (never inferred from a final price). Each
    sweep finds windows past their close + grace with no resolution row, fetches
    the market, and — once genuinely resolved — records the winner, close-call
    margin (proxy-end vs price-to-beat) and proxy/oracle divergence, which
    back-labels every snapshot's ``was_correct_*``.
    """

    def __init__(self, session_factory, settings, health=None, gamma: GammaClient | None = None,
                 grace_minutes: float = 2.0):
        self.session_factory = session_factory
        self.settings = settings
        self.health = health
        self.gamma = gamma or GammaClient(settings.gamma_base_url, settings.user_agent)
        self.service = ResolutionService(
            session_factory, health=health, close_call_bps=settings.close_call_bps
        )
        self.grace_minutes = grace_minutes
        self.period_s = min(settings.market_period_s, 30)
        self.log = get_logger("collectors.resolution")

    def _resolution_time(self, slug: str, fallback: dt.datetime | None) -> dt.datetime | None:
        try:
            return start_dt(parse_slug_start(slug) + 300)
        except ValueError:
            return fallback

    def _window_start(self, slug: str, fallback: dt.datetime | None) -> dt.datetime | None:
        try:
            return start_dt(parse_slug_start(slug))
        except ValueError:
            return fallback

    def due_markets(self, now: dt.datetime) -> list[_DueMarket]:
        from ..db.models import Market, MarketResolution

        deadline = now - dt.timedelta(minutes=self.grace_minutes)
        out: list[_DueMarket] = []
        with self.session_factory() as s:
            resolved = {r.market_id for r in s.execute(select(MarketResolution)).scalars()}
            for m in s.execute(select(Market)).scalars():
                if m.id in resolved:
                    continue
                res = self._resolution_time(m.slug, m.expected_resolution_time_utc)
                if res is None or res >= deadline:
                    continue
                out.append(
                    _DueMarket(
                        id=m.id,
                        slug=m.slug,
                        resolution=res,
                        window_start=self._window_start(m.slug, m.slug_derived_start_utc),
                        price_to_beat=m.price_to_beat,
                    )
                )
        return out

    async def resolve_due_once(self, now: dt.datetime | None = None) -> int:
        from ..features.btc_history import btc_price_at

        now = now or dt.datetime.now(dt.UTC)
        resolved = 0
        for m in self.due_markets(now):
            try:
                # closed=true: a settled window is dropped by Gamma's default active filter.
                raw = await self.gamma.get_market_by_slug(m.slug, closed=True)
            except Exception as exc:  # pragma: no cover - live network
                if self.health:
                    self.health.warning("resolution", f"gamma fetch failed: {m.slug}", {"error": str(exc)})
                continue
            if not raw:
                continue
            parsed = parse_resolution(raw)
            if not parsed.resolved:
                continue  # closed/settled but no clear winner yet — retry next sweep
            proxy_end = btc_price_at(self.session_factory, m.resolution)
            ptb = m.price_to_beat
            if ptb is None and m.window_start is not None:
                ptb = btc_price_at(self.session_factory, m.window_start)
            try:
                self.service.resolve(m.id, parsed, proxy_end=proxy_end, price_to_beat=ptb)
                resolved += 1
            except Exception as exc:  # pragma: no cover - defensive
                if self.health:
                    self.health.warning("resolution", f"resolve failed m{m.id}", {"error": str(exc)})
        return resolved

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                n = await self.resolve_due_once()
                if n:
                    self.log.info("resolution_sweep", resolved=n)
            except Exception as exc:  # pragma: no cover - defensive
                if self.health:
                    self.health.warning("resolution", f"resolution sweep error: {exc}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.period_s)
            except TimeoutError:
                pass
