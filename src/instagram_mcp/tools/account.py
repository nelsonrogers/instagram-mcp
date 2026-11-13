"""Sign-in status and the account's profile."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from mcp.server import MCPServer

from instagram_mcp.auth import (
    AUTH_COMMAND,
    INSTAGRAM,
    REFRESH_WHEN_LESS_THAN,
    AuthError,
    NotAuthenticatedError,
    StoredToken,
    utcnow,
)
from instagram_mcp.config import Settings
from instagram_mcp.context import READ_ONLY, AppContext, tool_errors
from instagram_mcp.insights import to_int
from instagram_mcp.instagram import InstagramAPI, InstagramAPIError

# Fields both graph.instagram.com and graph.facebook.com serve.
PROFILE_FIELDS = (
    "id,username,name,biography,website,profile_picture_url,followers_count,follows_count,media_count"
)


def auth_status(api: InstagramAPI, settings: Settings) -> dict[str, Any]:
    status: dict[str, Any] = {
        "profile": settings.profile,
        "signed_in": False,
        "token_file": str(settings.token_file),
    }
    try:
        token = api.token_info()
        # account_type exists only on graph.instagram.com.
        me = api.get("me", {"fields": "username,account_type" if token.graph == INSTAGRAM else "username"})
    except NotAuthenticatedError:
        status["next_step"] = (
            f"Ask the user to run `{AUTH_COMMAND}` in a terminal after the one-time Meta app setup "
            "described in the README."
        )
        return status
    except AuthError as exc:
        status["error"] = str(exc)
        status["next_step"] = f"Ask the user to run `{AUTH_COMMAND}` in a terminal."
        return status
    except InstagramAPIError as exc:
        # The token exists and wasn't rejected: a network problem or rate limit, not a sign-in issue.
        me = {"username": api.token_info().username}
        status["error"] = str(exc)
        status["next_step"] = "Instagram could not be reached or is rate-limiting: try again in a while."
    now = utcnow()
    expires = token.expires_at
    status.update(
        {
            "signed_in": True,
            "username": me.get("username"),
            "handle": f"@{me['username']}" if me.get("username") else None,
            "account_type": me.get("account_type"),
            "api_host": token.host.removeprefix("https://"),
            "token_expires_at": expires.isoformat(timespec="seconds") if expires else None,
            "token_expires_in_days": max(0, (expires - now).days) if expires else None,
            "token_expiry_is_estimated": token.expires_at_estimated,
            "token_auto_refresh_from": _auto_refresh_from(token),
            "permissions": token.permissions or None,
        }
    )
    if expires is None:
        status["token_expiry"] = "unknown" if token.expires_at_estimated else "never"
    refresh_error = api.last_refresh_error()
    if refresh_error:
        status["last_refresh_error"] = refresh_error
    if expires and expires - now < timedelta(days=7) and "next_step" not in status:
        status["next_step"] = (
            f"The token expires soon: run `instagram-mcp refresh` or `{AUTH_COMMAND}`."
            if token.refreshable
            else f"The token expires soon: generate a new one in the Meta App Dashboard and run `{AUTH_COMMAND}`."
        )
    return status


def _auto_refresh_from(token: StoredToken) -> str | None:
    if not token.refreshable or token.expires_at is None:
        return None
    return (token.expires_at - REFRESH_WHEN_LESS_THAN).date().isoformat()


def profile(ctx: AppContext) -> dict[str, Any]:
    me = ctx.profile(PROFILE_FIELDS, fresh=True)
    username = me.get("username")
    return {
        "id": str(me.get("id")) if me.get("id") else None,
        "username": username,
        "handle": f"@{username}" if username else None,
        "url": f"https://www.instagram.com/{username}/" if username else None,
        "name": me.get("name") or None,
        "biography": me.get("biography") or None,
        "website": me.get("website") or None,
        "followers": to_int(me.get("followers_count")),
        "following": to_int(me.get("follows_count")),
        "posts": to_int(me.get("media_count")),
        "profile_picture_url": me.get("profile_picture_url"),
        "notes": ["profile_picture_url is a temporary Instagram CDN link: it expires after a few days."],
    }


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(title="Instagram auth status", annotations=READ_ONLY)
    @tool_errors
    def get_instagram_auth_status() -> dict[str, Any]:
        """Check whether the Instagram server has a working token, which account it reads, which
        API host it uses, and when the token expires (Instagram Login tokens renew themselves).
        Call this first if another Instagram tool reports a sign-in problem."""
        return auth_status(ctx.api, ctx.settings)

    @mcp.tool(title="Instagram profile", annotations=READ_ONLY)
    @tool_errors
    def get_instagram_profile() -> dict[str, Any]:
        """The connected Instagram account's profile and current counts: username, display name,
        biography, website, followers, following and number of posts. For performance over a
        period use get_instagram_performance_summary."""
        return profile(ctx)
