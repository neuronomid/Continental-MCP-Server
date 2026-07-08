"""Test helpers importable from any test module (absolute import)."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_path(name: str) -> Path:
    return FIXTURES / name


def load_json_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())
