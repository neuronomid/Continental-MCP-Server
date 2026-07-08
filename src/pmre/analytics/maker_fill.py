"""Maker fill model — 'if I had posted a limit buy at p at time t, would it fill?'

Fill is inferred from subsequent prints at/through ``p`` on that token: a passive
buy at ``p`` fills when a later print trades at price ≤ ``p`` (someone sold into
the bid). Produces P(fill | label, post_style, regime) and time-to-fill
quantiles (mcp_plan.md §1.5).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass
class MakerPost:
    label: str
    post_style: str  # join_bid | mid_minus_1tick | mid
    price: float
    ts: float  # seconds
    regime: str | None = None
    token_id: str | None = None


@dataclass
class Print:
    ts: float
    price: float
    token_id: str | None = None


def did_fill(post: MakerPost, prints: list[Print], horizon_s: float = 300.0) -> tuple[bool, float | None]:
    """Return (filled, time_to_fill_s). Buy at ``post.price`` fills on a later
    print with price ≤ post.price within ``horizon_s``."""
    best_ttf = None
    for pr in prints:
        if post.token_id is not None and pr.token_id is not None and pr.token_id != post.token_id:
            continue
        if pr.ts < post.ts:
            continue
        if pr.ts - post.ts > horizon_s:
            continue
        if pr.price <= post.price + 1e-12:
            ttf = pr.ts - post.ts
            if best_ttf is None or ttf < best_ttf:
                best_ttf = ttf
    return (best_ttf is not None), best_ttf


@dataclass
class FillEstimate:
    label: str
    post_style: str
    regime: str | None
    p_fill: float
    median_ttf_s: float | None
    sample_size: int
    offset_ticks: int = 0


class MakerFillModel:
    def __init__(self, horizon_s: float = 300.0):
        self.horizon_s = horizon_s

    def estimate(
        self, posts: list[MakerPost], prints: list[Print]
    ) -> list[FillEstimate]:
        groups: dict[tuple, list[tuple[bool, float | None]]] = {}
        # index prints by token for speed
        by_token: dict[str | None, list[Print]] = {}
        for pr in prints:
            by_token.setdefault(pr.token_id, []).append(pr)
        all_prints = prints
        for post in posts:
            candidate_prints = by_token.get(post.token_id, all_prints) if post.token_id else all_prints
            filled, ttf = did_fill(post, candidate_prints, self.horizon_s)
            key = (post.label, post.post_style, post.regime)
            groups.setdefault(key, []).append((filled, ttf))

        estimates: list[FillEstimate] = []
        for (label, style, regime), outcomes in sorted(groups.items(), key=lambda k: str(k[0])):
            fills = [o for o in outcomes if o[0]]
            p_fill = len(fills) / len(outcomes)
            ttfs = [o[1] for o in fills if o[1] is not None]
            median_ttf = statistics.median(ttfs) if ttfs else None
            estimates.append(
                FillEstimate(
                    label=label,
                    post_style=style,
                    regime=regime,
                    p_fill=p_fill,
                    median_ttf_s=median_ttf,
                    sample_size=len(outcomes),
                )
            )
        return estimates
