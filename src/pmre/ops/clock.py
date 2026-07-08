"""Clock-drift checks (chrony). Snapshots are refused when drift is too large."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


@dataclass
class ClockStatus:
    offset_ms: float
    ok: bool
    warn: bool
    source: str = "chrony"


def parse_chronyc_tracking(output: str) -> float:
    """Extract |System time| offset in milliseconds from ``chronyc tracking``.

    Example line: ``System time     : 0.000012345 seconds slow of NTP time``.
    """
    for line in output.splitlines():
        if "System time" in line:
            m = re.search(r"([0-9]*\.?[0-9]+)\s+seconds", line)
            if m:
                return abs(float(m.group(1))) * 1000.0
    raise ValueError("could not parse chronyc tracking output")


def evaluate_drift(
    offset_ms: float, abort_ms: float = 250.0, warn_ms: float = 50.0
) -> ClockStatus:
    return ClockStatus(
        offset_ms=offset_ms,
        ok=offset_ms <= abort_ms,
        warn=offset_ms > warn_ms,
    )


def check_clock_drift(
    abort_ms: float = 250.0, warn_ms: float = 50.0, _runner=None
) -> ClockStatus:
    runner = _runner or (
        lambda: subprocess.run(
            ["chronyc", "tracking"], capture_output=True, text=True, timeout=5
        ).stdout
    )
    output = runner()
    offset_ms = parse_chronyc_tracking(output)
    return evaluate_drift(offset_ms, abort_ms, warn_ms)
