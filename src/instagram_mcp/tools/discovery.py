"""Public stats of other professional accounts (Meta's Business Discovery)."""

from __future__ import annotations

import re
from statistics import median
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from instagram_mcp.auth import FACEBOOK
from instagram_mcp.context import READ_ONLY, AppContext, tool_errors
from instagram_mcp.insights import media_kind, parse_time, pct, to_int

_USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")
PROFILE_FIELDS = (
    "username,name,biography,website,followers_count,follows_count,media_count,profile_picture_url"
)
MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count,view_count"
)


def public_account(ctx: AppContext, username: str, media_limit: int) -> dict[str, Any]:
    name = username.strip().lstrip("@")
    if not _USERNAME.match(name):
        raise ToolError(f"{username!r} is not a valid Instagram username.")
    if ctx.api.token_info().graph != FACEBOOK:
        raise ToolError(
            "Looking up other accounts (Meta's Business Discovery) only works with a unified login or "
            "Facebook Login token (graph.facebook.com), not with an Instagram Login token."
        )
    media = f",media.limit({media_limit}){{{MEDIA_FIELDS}}}" if media_limit else ""
    payload = ctx.api.get("me", {"fields": f"business_discovery.username({name}){{{PROFILE_FIELDS}{media}}}"})
    found = payload.get("business_discovery") or {}
    followers = to_int(found.get("followers_count"))
    posts = []
    for item in (found.get("media") or {}).get("data") or []:
        published = parse_time(item.get("timestamp"))
        likes, comments = to_int(item.get("like_count")), to_int(item.get("comments_count"))
        posts.append(
            {
                "id": str(item.get("id")),
                "type": media_kind(item),
                "published_at": ctx.local_time(published),
                "caption": item.get("caption") or "",
                "permalink": item.get("permalink"),
                "likes": likes,
                "comments": comments,
                "views": to_int(item.get("view_count")),
                "engagement_per_follower_pct": pct((likes or 0) + (comments or 0), followers, 2)
                if likes is not None
                else None,
            }
        )
    rates = [p["engagement_per_follower_pct"] for p in posts if p["engagement_per_follower_pct"] is not None]
    likes_list = [p["likes"] for p in posts if p["likes"] is not None]
    return {
        "username": found.get("username"),
        "handle": f"@{found['username']}" if found.get("username") else None,
        "url": f"https://www.instagram.com/{found['username']}/" if found.get("username") else None,
        "name": found.get("name"),
        "biography": found.get("biography"),
        "website": found.get("website"),
        "followers": followers,
        "following": to_int(found.get("follows_count")),
        "posts": to_int(found.get("media_count")),
        "recent_media": posts,
        "recent_media_summary": {
            "count": len(posts),
            "median_likes": median(likes_list) if likes_list else None,
            "median_engagement_per_follower_pct": round(median(rates), 2) if rates else None,
        },
        "notes": [
            "Public data only (Meta's Business Discovery): no reach, saves or audience. Likes are missing "
            "when the owner hides them. engagement_per_follower_pct = (likes + comments) / followers.",
            "Only Business and Creator accounts can be looked up; personal and age-restricted accounts can't.",
        ],
    }


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(title="Public Instagram account", annotations=READ_ONLY)
    @tool_errors
    def get_instagram_public_account(
        username: Annotated[
            str, Field(description="Another Business or Creator account's username, e.g. 'nasa'.")
        ],
        media_limit: Annotated[
            int, Field(ge=0, le=50, description="Recent posts to include (0 for the profile only).")
        ] = 12,
    ) -> dict[str, Any]:
        """Public profile and recent posts of another Business or Creator account (followers,
        posts, bio, and per post likes, comments and reel views), for benchmarking against
        peers or vetting partners. Needs a unified login or Facebook Login token."""
        return public_account(ctx, username, media_limit)
