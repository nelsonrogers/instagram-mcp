"""A small, read-only client for the Instagram Graph API.

It talks to graph.instagram.com (Instagram Login tokens) or graph.facebook.com (unified login
tokens), whichever the stored token belongs to. Callers write paths for the connected account
as ``me``, ``me/insights``, ``me/media``: on graph.facebook.com ``/me`` is the system user rather
than the Instagram account, so the client puts the account ID there instead.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any, Protocol

import httpx

from instagram_mcp.auth import (
    AUTH_COMMAND,
    FACEBOOK,
    HTTP_TIMEOUT,
    INSTAGRAM,
    PERMISSIONS_BY_GRAPH,
    AuthError,
    Clock,
    StoredToken,
    TokenManager,
    meta_error,
    redact,
    utcnow,
)
from instagram_mcp.config import Settings

logger = logging.getLogger(__name__)

# Graph API error codes (https://developers.facebook.com/docs/graph-api/guides/error-handling).
TOKEN_ERROR_CODES = {190, 102}
PERMISSION_ERROR_CODES = {10, 200, 3}
RATE_LIMIT_CODES = {4, 17, 32, 613, 80002}
TRANSIENT_CODES = {1, 2}
INVALID_PARAMETER_CODE = 100


class InstagramAPIError(Exception):
    """A Graph API request failed (with a message meant for the user)."""

    def __init__(self, message: str, *, code: int | None = None, subcode: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode


class InstagramAPI(Protocol):
    """What the tools need from Instagram. Tests inject a fake implementation."""

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a Graph API path; ``me`` (alone or as the first segment) is the connected account."""
        ...

    def token_info(self) -> StoredToken: ...

    def last_refresh_error(self) -> str | None: ...

    def download(self, url: str) -> bytes:
        """Fetch a media image from Instagram's CDN (thumbnails)."""
        ...


# Media images are served from Meta's CDNs; anything else is refused.
CDN_HOST_SUFFIXES = (".cdninstagram.com", ".fbcdn.net")
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024


def is_cdn_url(url: str) -> bool:
    parsed = httpx.URL(url)
    host = parsed.host or ""
    return parsed.scheme == "https" and any(host.endswith(suffix) for suffix in CDN_HOST_SUFFIXES)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def translate_error(
    response: httpx.Response, secrets: tuple[str, ...] = (), *, graph: str = INSTAGRAM
) -> Exception:
    """Turn a Graph API error response into AuthError or InstagramAPIError.

    ``secrets`` (the token) are removed from Meta's message, which can quote the request.
    ``graph`` picks the permission names in the advice (they differ between the two APIs).
    """
    error = meta_error(response)
    code = _int_or_none(error.get("code"))
    subcode = _int_or_none(error.get("error_subcode"))
    message = redact(str(error.get("message") or f"HTTP {response.status_code}"), secrets)
    detail = (
        f"{message} (code {code}" + (f", subcode {subcode}" if subcode else "") + ")" if code else message
    )
    if code in TOKEN_ERROR_CODES:
        return AuthError(
            f"Instagram rejected the access token: {detail}. It was revoked, expired, or the "
            f"password changed. Ask the user to run `{AUTH_COMMAND}` again."
        )
    if code in PERMISSION_ERROR_CODES:
        basic, insights = PERMISSIONS_BY_GRAPH[graph]
        return InstagramAPIError(
            f"Missing permission: {detail}. Generate a new token with both "
            f"{basic} and {insights}, then run `{AUTH_COMMAND}`.",
            code=code,
            subcode=subcode,
        )
    if code in RATE_LIMIT_CODES:
        return InstagramAPIError(
            f"Instagram rate limit reached: {detail}. Wait an hour and try again.", code=code, subcode=subcode
        )
    return InstagramAPIError(f"Instagram API error: {detail}", code=code, subcode=subcode)


class GraphInstagramClient:
    """Thread-safe GET client with automatic token refresh and one retry on transient errors.

    Token refreshes are serialised by the TokenManager; requests themselves run concurrently.
    """

    def __init__(
        self, settings: Settings, *, http: httpx.Client | None = None, clock: Clock = utcnow
    ) -> None:
        self.settings = settings
        self._http = http or httpx.Client(headers={"User-Agent": "instagram-mcp"})
        self._tokens = TokenManager(settings, self._http, clock=clock)

    def token_info(self) -> StoredToken:
        return self._tokens.token()

    def last_refresh_error(self) -> str | None:
        return self._tokens.last_refresh_error

    def download(self, url: str) -> bytes:
        if not is_cdn_url(url):
            raise InstagramAPIError("Refusing to download an image from outside Instagram's CDN.")
        try:
            with self._http.stream("GET", url, timeout=HTTP_TIMEOUT, follow_redirects=True) as response:
                if response.status_code >= 400:
                    raise InstagramAPIError(f"The image download failed (HTTP {response.status_code}).")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise InstagramAPIError("The image is unexpectedly large; skipped.")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise InstagramAPIError(f"Could not download the image ({type(exc).__name__}).") from exc
        return b"".join(chunks)

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        stored = self._tokens.token()
        token = stored.access_token
        url = f"{stored.host}/{self.settings.api_version}/{account_path(path, stored)}"
        query = {**(params or {}), "access_token": token}
        for attempt in (1, 2):
            try:
                # httpx.Client is thread-safe: tools send a few requests in parallel.
                response = self._http.get(url, params=query, timeout=HTTP_TIMEOUT)
            except httpx.HTTPError as exc:
                if attempt == 1:
                    time.sleep(1.0)
                    continue
                # str(exc) may contain the URL with the token: keep it out of the message.
                raise InstagramAPIError(f"Could not reach Instagram ({type(exc).__name__}).") from exc
            if response.status_code < 400:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise InstagramAPIError("Instagram returned a response that is not JSON.") from exc
                if not isinstance(payload, dict):
                    raise InstagramAPIError("Instagram returned an unexpected response.")
                return payload
            error = translate_error(response, (token,), graph=stored.graph)
            transient = response.status_code >= 500 or (
                isinstance(error, InstagramAPIError) and error.code in TRANSIENT_CODES
            )
            if transient and attempt == 1:
                time.sleep(1.0)
                continue
            raise error
        raise InstagramAPIError("Instagram request failed.")  # pragma: no cover - loop always returns


def account_path(path: str, token: StoredToken) -> str:
    """``me`` → the Instagram account ID on graph.facebook.com (where ``/me`` is the system user)."""
    path = path.lstrip("/")
    head, sep, rest = path.partition("/")
    if token.graph != FACEBOOK or head != "me":
        return path
    if not token.user_id:
        raise AuthError(
            f"The stored token doesn't say which Instagram account to read. Run `{AUTH_COMMAND}` again."
        )
    return f"{token.user_id}{sep}{rest}"


def paginate(
    api: InstagramAPI, path: str, params: dict[str, Any], *, max_items: int
) -> Iterator[dict[str, Any]]:
    """Yield ``data`` items across pages using ``paging.cursors.after`` (never the ``next`` URL,
    which embeds the token)."""
    query = dict(params)
    seen = 0
    while seen < max_items:
        page = api.get(path, query)
        items = page.get("data") or []
        for item in items:
            if isinstance(item, dict):
                yield item
                seen += 1
                if seen >= max_items:
                    return
        paging = page.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not items or not after or not paging.get("next"):
            return
        query["after"] = after
