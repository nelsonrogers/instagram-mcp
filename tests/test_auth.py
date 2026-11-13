"""Token storage, refresh and the OAuth helpers (against a mocked Meta HTTP API)."""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from instagram_mcp.auth import (
    AuthError,
    NotAuthenticatedError,
    StoredToken,
    TokenManager,
    TokenStore,
    authorize_url,
    code_from_redirect,
    exchange_code,
    exchange_for_long_lived,
)
from instagram_mcp.config import Settings

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
SECRET_TOKEN = "IGAAsecret-token-value"


def make_token(age: timedelta, *, lifetime: timedelta = timedelta(days=60)) -> StoredToken:
    obtained = NOW - age
    return StoredToken(
        access_token=SECRET_TOKEN,
        obtained_at=obtained,
        expires_at=obtained + lifetime,
        username="atelier.lune",
        user_id="17841400000000001",
        permissions=["instagram_business_basic", "instagram_business_manage_insights"],
    )


def mock_http(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def refresh_handler(
    calls: list[httpx.Request], *, status: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/refresh_access_token"
        assert request.url.params["grant_type"] == "ig_refresh_token"
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "Session has expired", "code": 190}})
        return httpx.Response(
            200, json={"access_token": "IGAArefreshed", "token_type": "bearer", "expires_in": 5183944}
        )

    return handler


# --------------------------------------------------------------------------- storage


def test_token_store_round_trip_and_permissions(settings: Settings) -> None:
    store = TokenStore(settings.token_file)
    assert store.load() is None
    token = make_token(timedelta(days=1))
    store.save(token)
    assert store.load() == token
    assert settings.token_file == settings.config_dir / "tokens" / "default.json"
    if sys.platform != "win32":
        assert stat.S_IMODE(settings.token_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(settings.token_file.parent.stat().st_mode) == 0o700
    assert store.delete() and not store.exists()


def test_unreadable_token_file_explains_what_to_do(settings: Settings) -> None:
    settings.token_file.parent.mkdir(parents=True)
    settings.token_file.write_text("{not json", encoding="utf-8")
    with pytest.raises(AuthError, match="instagram-mcp auth"):
        TokenStore(settings.token_file).load()


# --------------------------------------------------------------------------- refresh policy


def manager(
    settings: Settings, token: StoredToken | None, handler: Callable[[httpx.Request], httpx.Response]
) -> TokenManager:
    if token is not None:
        TokenStore(settings.token_file).save(token)
    return TokenManager(settings, mock_http(handler), clock=lambda: NOW)


def test_young_token_is_not_refreshed(settings: Settings) -> None:
    calls: list[httpx.Request] = []
    tokens = manager(settings, make_token(timedelta(days=5)), refresh_handler(calls))
    assert tokens.token().access_token == SECRET_TOKEN
    assert calls == []


def test_old_token_is_refreshed_and_saved(settings: Settings) -> None:
    calls: list[httpx.Request] = []
    tokens = manager(settings, make_token(timedelta(days=31)), refresh_handler(calls))
    fresh = tokens.token()
    assert len(calls) == 1 and calls[0].url.params["access_token"] == SECRET_TOKEN
    assert fresh.access_token == "IGAArefreshed"
    assert fresh.expires_at == NOW + timedelta(seconds=5183944) and not fresh.expires_at_estimated
    stored = TokenStore(settings.token_file).load()
    assert stored is not None and stored.access_token == "IGAArefreshed"
    assert stored.username == "atelier.lune" and stored.permissions  # metadata survives the refresh
    tokens.token()
    assert len(calls) == 1  # the fresh token is not refreshed again


def test_failed_refresh_keeps_the_valid_token(settings: Settings) -> None:
    calls: list[httpx.Request] = []
    tokens = manager(settings, make_token(timedelta(days=40)), refresh_handler(calls, status=400))
    assert tokens.token().access_token == SECRET_TOKEN
    assert tokens.last_refresh_error and "Session has expired" in tokens.last_refresh_error
    assert SECRET_TOKEN not in tokens.last_refresh_error


def test_expired_token_asks_for_a_new_sign_in(settings: Settings) -> None:
    calls: list[httpx.Request] = []
    tokens = manager(settings, make_token(timedelta(days=61)), refresh_handler(calls))
    with pytest.raises(AuthError, match=r"expired on 2026-11-11.*instagram-mcp auth"):
        tokens.token()
    assert calls == []


def test_missing_token(settings: Settings) -> None:
    tokens = manager(settings, None, refresh_handler([]))
    with pytest.raises(NotAuthenticatedError, match="instagram-mcp auth"):
        tokens.token()


def test_manual_refresh_needs_a_day_old_token(settings: Settings) -> None:
    calls: list[httpx.Request] = []
    tokens = manager(settings, make_token(timedelta(hours=2)), refresh_handler(calls))
    with pytest.raises(AuthError, match="24 hours"):
        tokens.refresh()
    TokenStore(settings.token_file).save(make_token(timedelta(days=2)))
    assert tokens.refresh().access_token == "IGAArefreshed"


def test_transport_errors_do_not_leak_the_token(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed for {request.url}")

    tokens = manager(settings, make_token(timedelta(days=2)), handler)
    with pytest.raises(AuthError) as excinfo:
        tokens.refresh()
    assert SECRET_TOKEN not in str(excinfo.value) and "ConnectError" in str(excinfo.value)


# --------------------------------------------------------------------------- OAuth helpers


def test_authorize_url_requests_both_permissions() -> None:
    url = urlparse(authorize_url("1234567890", "https://localhost/", "st4te"))
    query = parse_qs(url.query)
    assert url.netloc == "www.instagram.com" and url.path == "/oauth/authorize"
    assert query["scope"] == ["instagram_business_basic,instagram_business_manage_insights"]
    assert query["response_type"] == ["code"] and query["state"] == ["st4te"]
    assert query["redirect_uri"] == ["https://localhost/"]


def test_code_from_redirect() -> None:
    assert code_from_redirect("https://localhost/?code=AQBx123&state=st4te#_", "st4te") == "AQBx123"
    assert code_from_redirect("AQBx123#_", "st4te") == "AQBx123"
    with pytest.raises(AuthError, match="state mismatch"):
        code_from_redirect("https://localhost/?code=AQBx123&state=other", "st4te")
    with pytest.raises(AuthError, match="did not grant"):
        code_from_redirect(
            "https://localhost/?error=access_denied&error_reason=user_denied&state=st4te", "st4te"
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "data": [
                {
                    "access_token": "IGshort",
                    "user_id": "1784",
                    "permissions": "instagram_business_basic,instagram_business_manage_insights",
                }
            ]
        },
        {
            "access_token": "IGshort",
            "user_id": 1784,
            "permissions": ["instagram_business_basic", "instagram_business_manage_insights"],
        },
    ],
)
def test_exchange_code_accepts_both_response_shapes(payload: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.host == "api.instagram.com"
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["authorization_code"] and form["code"] == ["AQBx123"]
        return httpx.Response(200, json=payload)

    token, user_id, permissions = exchange_code(
        mock_http(handler), app_id="1", app_secret="s3cret", redirect_uri="https://localhost/", code="AQBx123"
    )
    assert token == "IGshort" and user_id == "1784"
    assert permissions == ["instagram_business_basic", "instagram_business_manage_insights"]


def test_exchange_for_long_lived() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/access_token"
        assert request.url.params["grant_type"] == "ig_exchange_token"
        return httpx.Response(
            200, json={"access_token": "IGlong", "token_type": "bearer", "expires_in": 5184000}
        )

    token = exchange_for_long_lived(mock_http(handler), "IGshort", "s3cret", NOW)
    assert token.access_token == "IGlong" and token.expires_at == NOW + timedelta(days=60)


def test_code_exchange_error_is_readable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error_type": "OAuthException", "code": 400, "error_message": "Invalid redirect_uri"}
        )

    with pytest.raises(AuthError, match="Invalid redirect_uri"):
        exchange_code(mock_http(handler), app_id="1", app_secret="s", redirect_uri="https://x/", code="c")


def test_token_json_has_no_surprises(settings: Settings, tmp_path: Path) -> None:
    TokenStore(settings.token_file).save(make_token(timedelta(days=1)))
    data = json.loads(settings.token_file.read_text(encoding="utf-8"))
    assert set(data) == {
        "access_token",
        "obtained_at",
        "expires_at",
        "expires_at_estimated",
        "user_id",
        "username",
        "permissions",
        "source",
        "graph",
    }
    assert tmp_path in settings.token_file.parents  # tests never touch ~/.config


def test_a_failed_refresh_is_not_retried_on_every_call(settings: Settings) -> None:
    calls: list[httpx.Request] = []
    clock = [NOW]
    TokenStore(settings.token_file).save(make_token(timedelta(days=40)))
    tokens = TokenManager(settings, mock_http(refresh_handler(calls, status=400)), clock=lambda: clock[0])
    for _ in range(10):
        tokens.token()
    assert len(calls) == 1
    clock[0] = NOW + timedelta(hours=1, minutes=1)
    tokens.token()
    assert len(calls) == 2  # retried after an hour


def test_tokens_are_redacted_from_meta_error_messages(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": f"Malformed access token {SECRET_TOKEN}", "code": 190}}
        )

    tokens = manager(settings, make_token(timedelta(days=2)), handler)
    with pytest.raises(AuthError) as excinfo:
        tokens.refresh()
    assert SECRET_TOKEN not in str(excinfo.value) and "[redacted]" in str(excinfo.value)


# --------------------------------------------------------------------------- unified login


def unified_token(*, expires_at: datetime | None = None) -> StoredToken:
    return StoredToken(
        access_token="EAAsystem-user-token",
        obtained_at=NOW - timedelta(days=90),
        expires_at=expires_at,
        user_id="17841400000000001",
        username="atelier.lune",
        permissions=["instagram_basic", "instagram_manage_insights"],
        graph="facebook",
    )


def no_http(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request: {request.url}")


def test_unified_login_tokens_are_stored_and_never_refreshed(settings: Settings) -> None:
    store = TokenStore(settings.token_file)
    store.save(unified_token())
    loaded = store.load()
    assert loaded == unified_token() and loaded.host == "https://graph.facebook.com"
    assert json.loads(settings.token_file.read_text(encoding="utf-8"))["expires_at"] is None
    assert not loaded.refreshable and not loaded.refresh_due(NOW) and not loaded.expired(NOW)
    tokens = TokenManager(settings, mock_http(no_http), clock=lambda: NOW)
    assert tokens.token().access_token == "EAAsystem-user-token"  # no refresh request
    with pytest.raises(AuthError, match="unified login"):
        tokens.refresh()


def test_an_expired_unified_login_token_asks_for_a_new_one(settings: Settings) -> None:
    TokenStore(settings.token_file).save(unified_token(expires_at=NOW - timedelta(days=1)))
    with pytest.raises(AuthError, match="generate a new one in the Meta App Dashboard"):
        TokenManager(settings, mock_http(no_http), clock=lambda: NOW).token()


def test_token_files_saved_before_unified_login_are_instagram_tokens(settings: Settings) -> None:
    data = json.loads(make_token(timedelta(days=1)).to_json())
    del data["graph"]
    settings.token_file.parent.mkdir(parents=True)
    settings.token_file.write_text(json.dumps(data), encoding="utf-8")
    loaded = TokenStore(settings.token_file).load()
    assert loaded is not None and loaded.graph == "instagram" and loaded.refreshable
