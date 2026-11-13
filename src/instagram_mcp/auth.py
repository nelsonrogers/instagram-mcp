"""Access tokens: storage, refresh and the optional OAuth exchange.

Two kinds of token reach the same Instagram data:

- **Instagram Login** (``graph = "instagram"``, host graph.instagram.com): a long-lived Instagram
  User access token that lasts 60 days and can be refreshed (once it is at least 24 hours old)
  for another 60 days. It is refreshed automatically when it gets old, so a monthly run keeps
  it alive. Meta no longer offers this setup to apps created after October 2026.
- **Unified login** (``graph = "facebook"``, host graph.facebook.com): the system-user token of
  Meta's "API setup with unified login" (or a Facebook user token from "API setup with Facebook
  login"). Its lifetime is chosen in the app's configuration (often "never"); it can't be
  refreshed through the API, so when it expires a new one is generated in the App Dashboard.
  ``/me`` is the system user there, not the Instagram account, so the account ID is stored.

This module keeps one token per *profile* in the config directory (``0600`` file in a ``0700``
folder, outside any repository).

Nothing here writes to stdout (that stream carries the MCP JSON-RPC messages), and tokens and
app secrets never appear in log lines or error messages.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from instagram_mcp.config import Settings

logger = logging.getLogger(__name__)

INSTAGRAM = "instagram"  # Instagram API with Instagram Login
FACEBOOK = "facebook"  # Instagram API with unified login (or Facebook Login)
GRAPH_HOSTS = {INSTAGRAM: "https://graph.instagram.com", FACEBOOK: "https://graph.facebook.com"}
GRAPH_HOST = GRAPH_HOSTS[INSTAGRAM]
AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
CODE_EXCHANGE_URL = "https://api.instagram.com/oauth/access_token"

SCOPE_BASIC = "instagram_business_basic"
SCOPE_INSIGHTS = "instagram_business_manage_insights"
SCOPES = (SCOPE_BASIC, SCOPE_INSIGHTS)
# The same two permissions under their graph.facebook.com names (unified login, Facebook Login).
FACEBOOK_SCOPES = ("instagram_basic", "instagram_manage_insights")
PERMISSIONS_BY_GRAPH = {INSTAGRAM: SCOPES, FACEBOOK: FACEBOOK_SCOPES}

LONG_LIVED_LIFETIME = timedelta(days=60)
MIN_AGE_BEFORE_REFRESH = timedelta(hours=24)  # Meta refuses to refresh younger tokens
REFRESH_WHEN_LESS_THAN = timedelta(days=50)  # i.e. refresh tokens that are ~10+ days old
REFRESH_RETRY_AFTER = timedelta(hours=1)  # after a failed refresh, don't retry on every call
HTTP_TIMEOUT = 30.0

AUTH_COMMAND = "instagram-mcp auth"
SETUP_DOC_GUIDE = 'the "One-time Meta app setup" section of the instagram-mcp README'

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


class AuthError(Exception):
    """An authentication problem with a message meant for the user."""


class NotAuthenticatedError(AuthError):
    """No stored token for the active profile."""


# --------------------------------------------------------------------------- stored token


@dataclass
class StoredToken:
    access_token: str
    obtained_at: datetime
    # None: no expiry date (a unified-login token set to never expire, or Meta didn't say).
    expires_at: datetime | None
    # True when Meta did not tell us the lifetime. For an Instagram Login token pasted from the
    # App Dashboard, the expiry is then assumed to be 60 days after it was saved (Meta's rule);
    # with ``expires_at`` None, it means the lifetime is unknown rather than "never".
    expires_at_estimated: bool = False
    # The Instagram professional account ID. On graph.facebook.com every request uses it.
    user_id: str | None = None
    username: str | None = None
    permissions: list[str] = field(default_factory=list)
    source: str = "dashboard"  # dashboard | oauth
    graph: str = INSTAGRAM  # which API the token belongs to: INSTAGRAM or FACEBOOK

    def to_json(self) -> str:
        data = asdict(self)
        data["obtained_at"] = self.obtained_at.isoformat()
        data["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        return json.dumps(data, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StoredToken:
        graph = str(data.get("graph") or INSTAGRAM)  # files saved before unified login: Instagram
        if graph not in GRAPH_HOSTS:
            raise ValueError(f"unknown graph {graph!r}")
        expires_at = data.get("expires_at")
        return cls(
            access_token=str(data["access_token"]),
            obtained_at=_parse_dt(data["obtained_at"]),
            expires_at=_parse_dt(expires_at) if expires_at else None,
            expires_at_estimated=bool(data.get("expires_at_estimated", False)),
            user_id=_opt_str(data.get("user_id")),
            username=_opt_str(data.get("username")),
            permissions=[str(p) for p in data.get("permissions") or []],
            source=str(data.get("source") or "dashboard"),
            graph=graph,
        )

    @property
    def host(self) -> str:
        return GRAPH_HOSTS[self.graph]

    @property
    def refreshable(self) -> bool:
        """Only Instagram Login tokens can be renewed through the API (``ig_refresh_token``)."""
        return self.graph == INSTAGRAM and self.expires_at is not None

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def refresh_due(self, now: datetime) -> bool:
        if not self.refreshable or self.expires_at is None:
            return False
        old_enough = now - self.obtained_at >= MIN_AGE_BEFORE_REFRESH
        return old_enough and self.expires_at - now < REFRESH_WHEN_LESS_THAN

    def can_refresh(self, now: datetime) -> bool:
        return self.refreshable and not self.expired(now) and now - self.obtained_at >= MIN_AGE_BEFORE_REFRESH


def _parse_dt(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _opt_str(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def _write_private(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path``, readable only by the current user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        Path(tmp).replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class TokenStore:
    """Reads and writes the token for one profile."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> StoredToken | None:
        if not self.path.is_file():
            return None
        try:
            return StoredToken.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AuthError(
                f"The stored token at {self.path} is unreadable ({type(exc).__name__}). "
                f"Run `{AUTH_COMMAND}` to save a new one."
            ) from exc

    def save(self, token: StoredToken) -> None:
        _write_private(self.path, token.to_json())

    def delete(self) -> bool:
        if self.path.is_file():
            self.path.unlink()
            return True
        return False


# --------------------------------------------------------------------------- Meta responses


def meta_error(response: httpx.Response) -> dict[str, Any]:
    """The ``error`` object of a Graph API error response (empty when absent)."""
    try:
        payload = response.json()
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    error = payload.get("error")
    if isinstance(error, dict):
        return error
    # api.instagram.com/oauth/access_token uses a flatter shape.
    if "error_message" in payload or "error_type" in payload:
        return {
            "message": payload.get("error_message"),
            "type": payload.get("error_type"),
            "code": payload.get("code"),
        }
    return {}


# Query parameters that carry secrets in Graph API and OAuth URLs.
_SECRET_PARAMS = re.compile(r"\b((?:access_token|input_token|client_secret|fb_exchange_token)=)[^&\s\"'>]+")


class RedactSecretsFilter(logging.Filter):
    """Masks tokens in HTTP log lines.

    httpx logs every request URL at INFO level, and Graph API URLs carry the access token. The
    CLI keeps those loggers at WARNING, but a program that embeds the server (or a debugging
    session) may configure logging differently, so the records themselves are cleaned too.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        cleaned = _SECRET_PARAMS.sub(r"\1[redacted]", message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


def install_log_redaction() -> None:
    """Attach :class:`RedactSecretsFilter` to the HTTP client loggers (idempotent)."""
    for name in ("httpx", "httpcore"):
        http_logger = logging.getLogger(name)
        if not any(isinstance(f, RedactSecretsFilter) for f in http_logger.filters):
            http_logger.addFilter(RedactSecretsFilter())


install_log_redaction()


def redact(text: str, secrets: tuple[str, ...]) -> str:
    """Remove tokens and secrets from text that may be shown to a person or a model."""
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    return text


def describe_meta_error(response: httpx.Response, secrets: tuple[str, ...] = ()) -> str:
    error = meta_error(response)
    message = redact(str(error.get("message") or f"HTTP {response.status_code}"), secrets)
    code = error.get("code")
    subcode = error.get("error_subcode")
    suffix = f" (code {code}" + (f", subcode {subcode}" if subcode else "") + ")" if code else ""
    return message + suffix


def _json(response: httpx.Response, what: str, secrets: tuple[str, ...]) -> dict[str, Any]:
    if response.status_code >= 400:
        raise AuthError(f"Instagram refused to {what}: {describe_meta_error(response, secrets)}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthError(f"Instagram sent an unreadable answer while trying to {what}.") from exc
    if not isinstance(payload, dict):
        raise AuthError(f"Instagram sent an unexpected answer while trying to {what}.")
    return payload


def _send(http: httpx.Client, method: str, url: str, what: str, **kwargs: Any) -> httpx.Response:
    try:
        return http.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
    except httpx.HTTPError as exc:
        # str(exc) can contain the request URL (and so the token): don't show it.
        raise AuthError(f"Could not reach Instagram to {what} ({type(exc).__name__}).") from exc


def refresh_long_lived(http: httpx.Client, access_token: str, now: datetime) -> StoredToken:
    """Exchange a valid long-lived token (24 h+ old) for a fresh 60-day one."""
    response = _send(
        http,
        "GET",
        f"{GRAPH_HOST}/refresh_access_token",
        "refresh the access token",
        params={"grant_type": "ig_refresh_token", "access_token": access_token},
    )
    payload = _json(response, "refresh the access token", (access_token,))
    return _token_from_payload(payload, now, fallback=access_token)


def exchange_for_long_lived(
    http: httpx.Client, short_lived_token: str, app_secret: str, now: datetime
) -> StoredToken:
    """Exchange a 1-hour token (from the OAuth code exchange) for a 60-day token."""
    response = _send(
        http,
        "GET",
        f"{GRAPH_HOST}/access_token",
        "create a long-lived token",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": app_secret,
            "access_token": short_lived_token,
        },
    )
    payload = _json(response, "create a long-lived token", (short_lived_token, app_secret))
    return _token_from_payload(payload, now, fallback=None)


def _token_from_payload(payload: dict[str, Any], now: datetime, *, fallback: str | None) -> StoredToken:
    token = payload.get("access_token") or fallback
    if not token:
        raise AuthError("Instagram did not return an access token.")
    expires_in = payload.get("expires_in")
    try:
        lifetime = timedelta(seconds=int(expires_in)) if expires_in is not None else None
    except (TypeError, ValueError):
        lifetime = None
    return StoredToken(
        access_token=str(token),
        obtained_at=now,
        expires_at=now + (lifetime or LONG_LIVED_LIFETIME),
        expires_at_estimated=lifetime is None,
    )


# --------------------------------------------------------------------------- token manager


class TokenManager:
    """Hands out the stored token, refreshing it when it is due.

    A refresh that fails while the token is still valid is logged and remembered (see
    ``last_refresh_error``) but does not block the caller: the current token keeps working.
    """

    def __init__(self, settings: Settings, http: httpx.Client, *, clock: Clock = utcnow) -> None:
        self.settings = settings
        self.store = TokenStore(settings.token_file)
        self._http = http
        self._clock = clock
        self._lock = threading.Lock()
        self.last_refresh_error: str | None = None
        self._refresh_failed_at: datetime | None = None

    def current(self) -> StoredToken:
        """The stored token, without refreshing it."""
        token = self.store.load()
        if token is None:
            raise NotAuthenticatedError(
                f"Not signed in to Instagram (profile '{self.settings.profile}'). Ask the user to run "
                f"`{AUTH_COMMAND}` in a terminal (one-time Meta app setup: {SETUP_DOC_GUIDE})."
            )
        return token

    def token(self) -> StoredToken:
        """A usable token: refreshed first when due. Raises AuthError when it has expired."""
        with self._lock:
            token = self.current()
            now = self._clock()
            if token.expired(now):
                raise AuthError(_expired_message(token))
            failed = self._refresh_failed_at
            backing_off = failed is not None and now - failed < REFRESH_RETRY_AFTER
            if token.refresh_due(now) and not backing_off:
                try:
                    token = self._refresh_locked(token, now)
                except AuthError as exc:
                    self.last_refresh_error = str(exc)
                    self._refresh_failed_at = now
                    logger.warning("Token refresh failed; using the current token: %s", exc)
            return token

    def refresh(self) -> StoredToken:
        """Refresh now (used by ``instagram-mcp refresh``)."""
        with self._lock:
            token = self.current()
            now = self._clock()
            if token.expired(now):
                raise AuthError(f"The token has already expired: run `{AUTH_COMMAND}` again.")
            if not token.refreshable:
                lifetime = (
                    f"It expires on {token.expires_at.date().isoformat()}"
                    if token.expires_at
                    else "Meta gave it no expiry date"
                )
                raise AuthError(
                    "This token comes from Meta's unified login (graph.facebook.com), which can't be "
                    f"renewed through the API. {lifetime}. When it stops working, generate a new one "
                    f"in the Meta App Dashboard and run `{AUTH_COMMAND}`."
                )
            if not token.can_refresh(now):
                raise AuthError(
                    "Meta only refreshes tokens that are at least 24 hours old. "
                    f"This one was saved at {token.obtained_at.isoformat(timespec='minutes')}; try later."
                )
            return self._refresh_locked(token, now)

    def _refresh_locked(self, token: StoredToken, now: datetime) -> StoredToken:
        fresh = refresh_long_lived(self._http, token.access_token, now)
        fresh.user_id, fresh.username, fresh.graph = token.user_id, token.username, token.graph
        fresh.permissions, fresh.source = token.permissions, token.source
        self.store.save(fresh)
        self.last_refresh_error = None
        self._refresh_failed_at = None
        logger.info("Refreshed the Instagram token; it now expires on %s", fresh.expires_at)
        return fresh


def _expired_message(token: StoredToken) -> str:
    day = token.expires_at.date().isoformat() if token.expires_at else "an unknown date"
    if token.refreshable:
        return (
            f"The Instagram access token expired on {day} (tokens last 60 days and are renewed "
            "automatically only when the server is used at least once in that window). Ask the user "
            f"to run `{AUTH_COMMAND}` again."
        )
    return (
        f"The Instagram access token expired on {day}. Ask the user to generate a new one in the Meta "
        f"App Dashboard and run `{AUTH_COMMAND}` again."
    )


# --------------------------------------------------------------------------- OAuth (optional)


def authorize_url(app_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": ",".join(SCOPES),
            "state": state,
            "force_reauth": "true",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def code_from_redirect(pasted: str, expected_state: str) -> str:
    """Extract the authorization code from the URL the browser was sent to (or a bare code)."""
    text = pasted.strip()
    if not text:
        raise AuthError("Nothing was pasted.")
    if "://" not in text and "?" not in text:
        return text.removesuffix("#_")
    params = parse_qs(urlparse(text).query)
    if "error" in params:
        reason = (params.get("error_description") or params["error"])[0]
        raise AuthError(f"Instagram did not grant access: {reason}")
    state = (params.get("state") or [""])[0]
    if not secrets.compare_digest(state, expected_state):
        raise AuthError("That URL belongs to a different sign-in attempt (state mismatch). Start again.")
    code = (params.get("code") or [""])[0].removesuffix("#_")
    if not code:
        raise AuthError("No ?code= in that URL. Paste the full address from the browser's address bar.")
    return code


def exchange_code(
    http: httpx.Client, *, app_id: str, app_secret: str, redirect_uri: str, code: str
) -> tuple[str, str | None, list[str]]:
    """Exchange an authorization code for a short-lived token: ``(token, user_id, permissions)``."""
    response = _send(
        http,
        "POST",
        CODE_EXCHANGE_URL,
        "exchange the authorization code",
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )
    payload = _json(response, "exchange the authorization code", (app_secret, code))
    data = payload.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        payload = data[0]
    token = payload.get("access_token")
    if not token:
        raise AuthError("Instagram did not return an access token for that code.")
    permissions = payload.get("permissions") or []
    if isinstance(permissions, str):
        permissions = [p for p in permissions.split(",") if p]
    return str(token), _opt_str(payload.get("user_id")), [str(p) for p in permissions]
