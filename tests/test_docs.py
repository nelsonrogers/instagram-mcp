"""Generated docs and the report sample stay in sync with the code."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import anyio

ROOT = Path(__file__).resolve().parent.parent


def _generator() -> Any:
    spec = importlib.util.spec_from_file_location("generate_docs", ROOT / "scripts" / "generate_docs.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__


def test_tool_reference_and_sample_are_up_to_date() -> None:
    module = _generator()
    tools, report = anyio.run(module.collect)
    stale = "is stale: run `uv run python scripts/generate_docs.py`"
    assert (ROOT / "docs" / "tools.md").read_text(encoding="utf-8") == module.render_tools(tools), (
        f"docs/tools.md {stale}"
    )
    sample = ROOT / "docs" / "examples" / "instagram-report.sample.json"
    assert sample.read_text(encoding="utf-8") == module.render_sample(report), f"the report sample {stale}"


def test_report_shape_is_documented() -> None:
    """Within schema_version 1, keys may only be added: the documented sample pins the shape."""
    sample = json.loads(
        (ROOT / "docs" / "examples" / "instagram-report.sample.json").read_text(encoding="utf-8")
    )
    assert sample["schema_version"] == "1"
    assert list(sample) == [
        "schema_version",
        "generated_at",
        "source",
        "date_range",
        "profile",
        "performance",
        "audience",
        "posts",
        "best_hours",
        "unavailable",
        "notes",
    ]
    doc = (ROOT / "docs" / "report.md").read_text(encoding="utf-8")
    for key in _shape(sample):
        assert f"`{key}`" in doc, f"docs/report.md doesn't describe `{key}`"
    for key in sample["performance"]["totals"]:
        assert f"`{key}`" in doc, f"docs/report.md doesn't describe totals.{key}"
    assert sample["posts"][0]["thumbnail"].startswith("data:image/webp;base64,")
