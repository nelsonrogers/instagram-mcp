from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from instagram_mcp.config import Settings
from instagram_mcp.server import build_server
from tests.fakes import NOW, FakeInstagramAPI


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the developer's real config/token; pin "today" for date maths."""
    for name in list(os.environ):
        if name.startswith("INSTAGRAM_MCP_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("instagram_mcp.periods.now", lambda tz: NOW.astimezone(tz))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=tmp_path / "config", output_dir=tmp_path / "out", timezone="Europe/Paris")


@pytest.fixture
def fake_api() -> FakeInstagramAPI:
    return FakeInstagramAPI()


@pytest.fixture
def make_client(settings: Settings) -> Callable[..., Client]:
    def factory(api: FakeInstagramAPI | None = None) -> Client:
        return Client(build_server(settings, api=api or FakeInstagramAPI()))

    return factory


@pytest.fixture
async def client(make_client: Callable[..., Client], fake_api: FakeInstagramAPI) -> AsyncIterator[Client]:
    async with make_client(fake_api) as connected:
        yield connected


async def call(client: Client, name: str, **arguments: Any) -> dict[str, Any]:
    import json

    result = await client.call_tool(name, arguments)
    text = next((block.text for block in result.content if getattr(block, "text", None)), "")
    assert not result.is_error, f"{name} failed: {text}"
    payload = result.structured_content
    return payload if isinstance(payload, dict) else json.loads(text)


async def call_error(client: Client, name: str, **arguments: Any) -> str:
    result = await client.call_tool(name, arguments)
    assert result.is_error, f"{name} unexpectedly succeeded"
    return "\n".join(block.text for block in result.content if getattr(block, "text", None))
