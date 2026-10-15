"""The TypeScript client's generated command types match the Python registry."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_generated_types_are_current() -> None:
    spec = importlib.util.spec_from_file_location(
        "generate_ts_types", ROOT / "scripts" / "generate_ts_types.py"
    )
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    current = generator.OUTPUT.read_text(encoding="utf-8")
    assert current == generator.render(), "run: python scripts/generate_ts_types.py"
