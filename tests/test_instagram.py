"""The real Graph client, against a mocked graph.instagram.com."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from instagram_mcp.auth import AuthError, StoredToken, TokenStore
from instagram_mcp.config import DEFAULT_API_VERSION as V
from instagram_mcp.config import Settings
from instagram_mcp.instagram import GraphInstagramClient, InstagramAPIError, paginate

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
TOKEN = "IGAAsecret-token-value"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("instagram_mcp.instagram.time.sleep", lambda _seconds: None)


def client(settings: Settings, handler: Callable[[httpx.Request], httpx.Response]) -> GraphInstagramClient:
    TokenStore(settings.token_file).save(
        StoredToken(
            access_token=TOKEN, obtained_at=NOW - timedelta(days=2), expires_at=NOW + timedelta(days=58)
        )
    )
    return GraphInstagramClient(
        settings, http=httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: NOW
    )


def meta_error(code: int, message: str, status: int = 400, subcode: int | None = None) -> httpx.Response:
    error: dict[str, object] = {"message": message, "type": "OAuthException", "code": code, "fbtrace_id": "x"}
    if subcode:
        error["error_subcode"] = subcode
    return httpx.Response(status, json={"error": error})


def test_requests_are_versioned_and_carry_the_token(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "1", "username": "atelier.lune"})

    api = client(settings, handler)
    assert api.get("me", {"fields": "username"})["username"] == "atelier.lune"
    request = seen[0]
    assert str(request.url).startswith(f"https://graph.instagram.com/{V}/me?")
    assert request.url.params["access_token"] == TOKEN and request.url.params["fields"] == "username"


def test_api_version_is_configurable(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    custom = Settings(config_dir=settings.config_dir, api_version="v25.0")
    client(custom, handler).get("me")
    assert seen[0].url.path == "/v25.0/me"


def test_invalid_token_becomes_an_auth_error(settings: Settings) -> None:
    api = client(settings, lambda _r: meta_error(190, "Error validating access token", subcode=463))
    with pytest.raises(AuthError, match="instagram-mcp auth") as excinfo:
        api.get("me")
    assert "code 190, subcode 463" in str(excinfo.value) and TOKEN not in str(excinfo.value)


def test_missing_permission_names_the_scope(settings: Settings) -> None:
    api = client(
        settings, lambda _r: meta_error(10, "Application does not have permission for this action", 403)
    )
    with pytest.raises(InstagramAPIError, match="instagram_business_manage_insights") as excinfo:
        api.get("me/insights")
    assert excinfo.value.code == 10


def test_rate_limit_message(settings: Settings) -> None:
    api = client(settings, lambda _r: meta_error(4, "Application request limit reached"))
    with pytest.raises(InstagramAPIError, match="rate limit"):
        api.get("me")


def test_transient_errors_are_retried_once(settings: Settings) -> None:
    answers = [meta_error(2, "Service temporarily unavailable", 500), httpx.Response(200, json={"ok": True})]
    api = client(settings, lambda _r: answers.pop(0))
    assert api.get("me") == {"ok": True}

    failing = client(settings, lambda _r: meta_error(1, "An unknown error has occurred.", 500))
    with pytest.raises(InstagramAPIError, match="unknown error"):
        failing.get("me")


def test_network_errors_do_not_leak_the_token(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out: {request.url}")

    with pytest.raises(InstagramAPIError) as excinfo:
        client(settings, handler).get("me")
    assert TOKEN not in str(excinfo.value) and "ReadTimeout" in str(excinfo.value)


def test_paginate_follows_cursors_not_next_urls(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        after = request.url.params.get("after")
        if after is None:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "1"}, {"id": "2"}],
                    "paging": {
                        "cursors": {"after": "C2"},
                        "next": "https://graph.instagram.com/...&access_token=X",
                    },
                },
            )
        return httpx.Response(200, json={"data": [{"id": "3"}], "paging": {"cursors": {"after": "C3"}}})

    api = client(settings, handler)
    assert [item["id"] for item in paginate(api, "me/media", {"limit": 2}, max_items=10)] == ["1", "2", "3"]
    assert [r.url.params.get("after") for r in seen] == [None, "C2"]
    assert all(r.url.host == "graph.instagram.com" and r.url.path == f"/{V}/me/media" for r in seen)
    assert [item["id"] for item in paginate(api, "me/media", {"limit": 2}, max_items=1)] == ["1"]


def test_download_only_fetches_instagram_cdn_images(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"\xff\xd8jpeg-bytes")

    api = client(settings, handler)
    url = "https://scontent-cdg4-1.cdninstagram.com/v/t51.2885-15/123_n.jpg?stp=dst-jpg&_nc_ht=x&oh=sig"
    assert api.download(url) == b"\xff\xd8jpeg-bytes"
    assert "access_token" not in str(seen[0].url)  # CDN links are signed; the token stays home
    for bad in ("http://scontent.cdninstagram.com/x.jpg", "https://evil.example/x.jpg", "file:///etc/passwd"):
        with pytest.raises(InstagramAPIError, match="outside Instagram's CDN"):
            api.download(bad)
    assert len(seen) == 1


def test_download_errors_and_size_limit(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://scontent.xx.fbcdn.net/v/123.jpg"
    with pytest.raises(InstagramAPIError, match="HTTP 403"):
        client(settings, lambda _r: httpx.Response(403)).download(url)
    monkeypatch.setattr("instagram_mcp.instagram.MAX_DOWNLOAD_BYTES", 10)
    with pytest.raises(InstagramAPIError, match="unexpectedly large"):
        client(settings, lambda _r: httpx.Response(200, content=b"x" * 50)).download(url)


def test_graph_errors_never_echo_the_token(settings: Settings) -> None:
    api = client(settings, lambda _r: meta_error(190, f"Malformed access token {TOKEN}"))
    with pytest.raises(AuthError) as excinfo:
        api.get("me")
    assert TOKEN not in str(excinfo.value) and "[redacted]" in str(excinfo.value)


def unified_client(
    settings: Settings, handler: Callable[[httpx.Request], httpx.Response], *, user_id: str | None = "178414"
) -> GraphInstagramClient:
    TokenStore(settings.token_file).save(
        StoredToken(
            access_token="EAAsystem-user-token",
            obtained_at=NOW - timedelta(days=2),
            expires_at=None,
            user_id=user_id,
            graph="facebook",
        )
    )
    return GraphInstagramClient(
        settings, http=httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: NOW
    )


def test_unified_login_tokens_read_the_account_on_graph_facebook(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    api = unified_client(settings, handler)
    api.get("me", {"fields": "username"})
    api.get("me/insights", {"metric": "views"})
    api.get("/me/media")
    api.get("17900000000000001/insights")  # a media ID stays as it is
    assert {r.url.host for r in seen} == {"graph.facebook.com"}
    assert [r.url.path for r in seen] == [
        f"/{V}/178414",
        f"/{V}/178414/insights",
        f"/{V}/178414/media",
        f"/{V}/17900000000000001/insights",
    ]


def test_unified_login_errors_name_the_facebook_permissions(settings: Settings) -> None:
    api = unified_client(settings, lambda _r: meta_error(10, "Application does not have permission", 403))
    with pytest.raises(InstagramAPIError, match="instagram_manage_insights"):
        api.get("me/insights")
    without_account = unified_client(settings, lambda _r: httpx.Response(200, json={}), user_id=None)
    with pytest.raises(AuthError, match="which Instagram account"):
        without_account.get("me")
