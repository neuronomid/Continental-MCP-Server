"""Market discovery: Gamma slug lookup + CLOB fee-param normalisation.

Pure parsing (``parse_gamma_market``, ``parse_clob_market_info``) is separated
from I/O (``GammaClient``, ``ClobClient``) and persistence (``DiscoveryService``)
so parsers can be golden-fixture tested without a network.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from .. import COLLECTOR_VERSION
from ..logging_setup import get_logger
from .slugs import expected_windows, parse_slug_start, start_dt


class AmbiguousMappingError(ValueError):
    """Outcome→UP/DOWN mapping could not be resolved unambiguously."""


@dataclass
class ParsedToken:
    token_id: str
    outcome: str  # UP | DOWN
    outcome_index: int


@dataclass
class ParsedMarket:
    slug: str
    condition_id: str | None
    question_id: str | None
    title: str | None
    event_slug: str | None
    enable_order_book: bool
    active: bool
    closed: bool
    window_start_utc: dt.datetime | None
    expected_resolution_time_utc: dt.datetime | None
    slug_derived_start_utc: dt.datetime | None
    price_to_beat: float | None
    price_to_beat_source: str | None
    tokens: list[ParsedToken]
    raw: dict = field(default_factory=dict)


@dataclass
class FeeParams:
    fees_enabled: bool | None
    fee_rate_bps: float | None
    maker_rebate_bps: float | None
    tick_size: float | None
    min_order_size: float | None
    params_json: dict


# --- helpers ---------------------------------------------------------------
def _maybe_json_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            return [value]
    return [value]


def _normalize_outcome(name: str) -> str | None:
    low = str(name).strip().lower()
    if low in {"up", "yes", "higher", "above"}:
        return "UP"
    if low in {"down", "no", "lower", "below"}:
        return "DOWN"
    return None


def _parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.UTC)
    s = str(value).replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


_PTB_FIELDS = [
    "priceToBeat",
    "price_to_beat",
    "startingPrice",
    "startPrice",
    "referencePrice",
]


def parse_gamma_market(raw: dict) -> ParsedMarket:
    """Normalise a Gamma market object into a :class:`ParsedMarket`.

    Raises :class:`AmbiguousMappingError` if outcomes cannot be mapped to exactly
    one UP token and one DOWN token — we *refuse* rather than guess.
    """
    slug = raw.get("slug") or ""
    outcomes = _maybe_json_list(raw.get("outcomes"))
    token_ids = _maybe_json_list(raw.get("clobTokenIds") or raw.get("clob_token_ids"))

    if len(outcomes) != len(token_ids):
        raise AmbiguousMappingError(
            f"{slug}: outcomes ({len(outcomes)}) and token ids ({len(token_ids)}) length mismatch"
        )
    if len(outcomes) != 2:
        raise AmbiguousMappingError(f"{slug}: expected 2 outcomes, got {len(outcomes)}")

    tokens: list[ParsedToken] = []
    seen: set[str] = set()
    for idx, (name, tid) in enumerate(zip(outcomes, token_ids, strict=True)):
        mapped = _normalize_outcome(name)
        if mapped is None:
            raise AmbiguousMappingError(f"{slug}: unrecognised outcome name {name!r}")
        if mapped in seen:
            raise AmbiguousMappingError(f"{slug}: duplicate mapped outcome {mapped}")
        seen.add(mapped)
        tokens.append(ParsedToken(token_id=str(tid), outcome=mapped, outcome_index=idx))

    if seen != {"UP", "DOWN"}:
        raise AmbiguousMappingError(f"{slug}: mapping did not yield exactly UP+DOWN: {seen}")

    slug_start = None
    try:
        slug_start = start_dt(parse_slug_start(slug))
    except ValueError:
        pass

    window_start = _parse_dt(raw.get("startDate") or raw.get("gameStartTime")) or slug_start
    end = _parse_dt(raw.get("endDate") or raw.get("end_date_iso"))
    if end is None and slug_start is not None:
        end = slug_start + dt.timedelta(seconds=300)

    ptb, ptb_src = None, None
    for f in _PTB_FIELDS:
        if raw.get(f) is not None:
            try:
                ptb = float(raw[f])
                ptb_src = "gamma"
                break
            except (TypeError, ValueError):
                continue

    return ParsedMarket(
        slug=slug,
        condition_id=raw.get("conditionId") or raw.get("condition_id"),
        question_id=raw.get("questionID") or raw.get("question_id"),
        title=raw.get("question") or raw.get("title"),
        event_slug=(raw.get("events") or [{}])[0].get("slug") if raw.get("events") else raw.get("eventSlug"),
        enable_order_book=bool(raw.get("enableOrderBook", raw.get("enable_order_book", True))),
        active=bool(raw.get("active", True)),
        closed=bool(raw.get("closed", False)),
        window_start_utc=window_start,
        expected_resolution_time_utc=end,
        slug_derived_start_utc=slug_start,
        price_to_beat=ptb,
        price_to_beat_source=ptb_src,
        tokens=tokens,
        raw=raw,
    )


def parse_clob_market_info(raw: dict) -> FeeParams:
    """Extract fee/tick/min-size params from a CLOB market-info object."""
    fees_enabled = raw.get("fees_enabled")
    if fees_enabled is None:
        fees_enabled = raw.get("feesEnabled")

    fee_bps = None
    for f in ("fee_rate_bps", "taker_base_fee", "taker_fee_bps", "base_fee"):
        if raw.get(f) is not None:
            try:
                fee_bps = float(raw[f])
                break
            except (TypeError, ValueError):
                continue

    maker_bps = None
    for f in ("maker_base_fee", "maker_rebate_bps"):
        if raw.get(f) is not None:
            try:
                maker_bps = float(raw[f])
                break
            except (TypeError, ValueError):
                continue

    tick = raw.get("minimum_tick_size") or raw.get("tick_size") or raw.get("minimumTickSize")
    min_size = (
        raw.get("minimum_order_size")
        or raw.get("min_order_size")
        or raw.get("minimumOrderSize")
    )
    if fee_bps is not None and fees_enabled is None:
        fees_enabled = fee_bps > 0

    return FeeParams(
        fees_enabled=bool(fees_enabled) if fees_enabled is not None else None,
        fee_rate_bps=fee_bps,
        maker_rebate_bps=maker_bps,
        tick_size=float(tick) if tick is not None else None,
        min_order_size=float(min_size) if min_size is not None else None,
        params_json=raw,
    )


# --- I/O clients -----------------------------------------------------------
class GammaClient:
    def __init__(self, base_url: str, user_agent: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.headers = {"User-Agent": user_agent}
        self.timeout = timeout

    @retry(stop=stop_after_attempt(4), wait=wait_exponential_jitter(initial=0.5, max=10))
    async def get_market_by_slug(self, slug: str, closed: bool | None = None) -> dict | None:
        # Gamma's /markets?slug= implicitly returns only *active* markets, so a
        # resolved 5-minute window is dropped unless closed=true is requested
        # (discovery wants the live/active market; resolution wants the closed one).
        params: dict[str, str] = {"slug": slug}
        if closed is not None:
            params["closed"] = "true" if closed else "false"
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as c:
            resp = await c.get(f"{self.base_url}/markets", params=params)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data[0] if data else None
            return data

    @retry(stop=stop_after_attempt(4), wait=wait_exponential_jitter(initial=0.5, max=10))
    async def scan_by_tag(self, tag: str, limit: int = 200) -> list[dict]:
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as c:
            resp = await c.get(
                f"{self.base_url}/markets",
                params={"tag": tag, "limit": limit, "active": "true"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else [data]


class ClobClient:
    def __init__(self, base_url: str, user_agent: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.headers = {"User-Agent": user_agent}
        self.timeout = timeout

    @retry(stop=stop_after_attempt(4), wait=wait_exponential_jitter(initial=0.5, max=10))
    async def get_market_info(self, condition_id: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as c:
            resp = await c.get(f"{self.base_url}/markets/{condition_id}")
            resp.raise_for_status()
            return resp.json()


# --- persistence service ---------------------------------------------------
class DiscoveryService:
    """Persists discovered markets/tokens/fee schedules; refuses ambiguous maps."""

    def __init__(self, session_factory, health=None, fee_model_version: str = "fee-v1"):
        self.session_factory = session_factory
        self.health = health
        self.fee_model_version = fee_model_version

    def upsert_market(
        self,
        parsed: ParsedMarket,
        fees: FeeParams | None = None,
        discovered_via: str = "slug",
    ) -> int:
        from sqlalchemy import select

        from ..db.models import FeeSchedule, Market, MarketToken

        with self.session_factory() as s:
            m = s.execute(select(Market).where(Market.slug == parsed.slug)).scalar_one_or_none()
            if m is None:
                m = Market(slug=parsed.slug)
                s.add(m)
            m.condition_id = parsed.condition_id
            m.question_id = parsed.question_id
            m.title = parsed.title
            m.event_slug = parsed.event_slug
            m.enable_order_book = parsed.enable_order_book
            m.active = parsed.active
            m.closed = parsed.closed
            m.window_start_utc = parsed.window_start_utc
            m.expected_resolution_time_utc = parsed.expected_resolution_time_utc
            m.slug_derived_start_utc = parsed.slug_derived_start_utc
            if parsed.price_to_beat is not None:
                m.price_to_beat = parsed.price_to_beat
                m.price_to_beat_source = parsed.price_to_beat_source
            m.discovered_via = discovered_via
            m.collector_version = COLLECTOR_VERSION
            m.raw_json = parsed.raw
            if fees is not None:
                m.fees_enabled = fees.fees_enabled
                m.fee_rate_bps = fees.fee_rate_bps
                m.fee_params_json = fees.params_json
                m.tick_size = fees.tick_size
                m.min_order_size = fees.min_order_size
            s.flush()

            existing = {t.outcome: t for t in m.tokens}
            for pt in parsed.tokens:
                if pt.outcome in existing:
                    existing[pt.outcome].token_id = pt.token_id
                    existing[pt.outcome].outcome_index = pt.outcome_index
                else:
                    s.add(
                        MarketToken(
                            market_id=m.id,
                            token_id=pt.token_id,
                            outcome=pt.outcome,
                            outcome_index=pt.outcome_index,
                        )
                    )
            if fees is not None:
                s.add(
                    FeeSchedule(
                        market_id=m.id,
                        fees_enabled=fees.fees_enabled,
                        fee_rate_bps=fees.fee_rate_bps,
                        maker_rebate_bps=fees.maker_rebate_bps,
                        fee_params_json=fees.params_json,
                        fee_model_version=self.fee_model_version,
                    )
                )
            mid = m.id
            s.commit()
            return mid

    def handle_ambiguous(self, slug: str, error: Exception) -> None:
        if self.health:
            self.health.warning(
                "discovery", f"ambiguous outcome mapping refused: {slug}", {"error": str(error)}
            )


# --- live run-loop ---------------------------------------------------------
class DiscoveryCollector:
    """Periodic, slug-driven market discovery against the live Gamma/CLOB APIs.

    Each sweep resolves the current + next ``window_lookahead`` 5-minute windows to
    deterministic ``btc-updown-5m-<unix>`` slugs (see :mod:`.slugs`), looks each up
    on Gamma, normalises it (refusing ambiguous UP/DOWN maps), enriches it with CLOB
    fee/tick params, and upserts it via :class:`DiscoveryService`. All network calls
    are best-effort: a failure on one slug is logged and skipped so the loop — and
    the collector's heartbeat — stay alive.
    """

    def __init__(
        self,
        session_factory,
        settings,
        health=None,
        *,
        window_lookahead: int = 3,
    ):
        self.gamma = GammaClient(settings.gamma_base_url, settings.user_agent)
        self.clob = ClobClient(settings.clob_base_url, settings.user_agent)
        self.service = DiscoveryService(
            session_factory, health=health, fee_model_version=settings.fee_model_version
        )
        self.health = health
        self.window_lookahead = window_lookahead
        # Sweep well within a 5-minute window so a new market is captured promptly.
        self.period_s = min(settings.market_period_s, 30)
        self.log = get_logger("collectors.discovery")

    async def discover_once(self, now: dt.datetime | None = None) -> int:
        """Resolve + upsert the current and upcoming windows; return #markets upserted."""
        now = now or dt.datetime.now(dt.UTC)
        found = 0
        for _unix, slug in expected_windows(now, self.window_lookahead):
            try:
                raw = await self.gamma.get_market_by_slug(slug)
            except Exception as exc:
                if self.health:
                    self.health.warning("discovery", f"gamma lookup failed: {slug}", {"error": str(exc)})
                continue
            if not raw:
                continue  # window not listed yet — normal for the far edge of the lookahead
            try:
                parsed = parse_gamma_market(raw)
            except AmbiguousMappingError as exc:
                self.service.handle_ambiguous(slug, exc)
                continue
            fees = None
            if parsed.condition_id:
                try:
                    info = await self.clob.get_market_info(parsed.condition_id)
                    fees = parse_clob_market_info(info)
                except Exception as exc:
                    if self.health:
                        self.health.warning(
                            "discovery", f"clob market-info failed: {slug}", {"error": str(exc)}
                        )
            self.service.upsert_market(parsed, fees, discovered_via="slug")
            found += 1
        return found

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                n = await self.discover_once()
                self.log.info("discovery_sweep", markets_upserted=n)
            except Exception as exc:  # pragma: no cover - defensive; keep the loop alive
                if self.health:
                    self.health.warning("discovery", f"discovery sweep error: {exc}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.period_s)
            except TimeoutError:
                pass
