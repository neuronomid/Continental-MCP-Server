"""Strategy candidate registry: extraction, gates, promotion, audit."""

from __future__ import annotations

from .candidates import CandidateEvidence, CandidateRegistry, make_candidate_id
from .gates import GateContext, GateResult, evaluate_gate

__all__ = [
    "CandidateRegistry",
    "CandidateEvidence",
    "make_candidate_id",
    "GateContext",
    "GateResult",
    "evaluate_gate",
]
