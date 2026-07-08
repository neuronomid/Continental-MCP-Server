"""pm-research-engine (``pmre``).

24/7 research + intelligence service for Polymarket BTC 5-minute Up/Down markets.
Read-only with respect to trading: no keys, no orders, no bot-status decisions.
See ``mcp_plan.md`` and ``mcp_phases.md`` for the full specification.
"""

__version__ = "0.1.0"

# Version stamps carried on every relevant row so analyses are only compared
# within a version (mcp_phases.md cross-phase requirement).
COLLECTOR_VERSION = "collector-v1"
FEATURE_VERSION = "feature-v1"
FEE_MODEL_VERSION = "fee-v1"
REGIME_MODEL_VERSION = "regime-v1"
NET_EV_INPUTS_VERSION = "netev-v1"
