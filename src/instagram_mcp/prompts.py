"""Reusable prompt templates (shown as slash commands / prompt pickers in MCP clients)."""

from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field


def register(mcp: MCPServer) -> None:
    @mcp.prompt(title="Instagram check-up")
    def instagram_checkup(
        period: Annotated[
            str, Field(description="e.g. last_28_days, last_90_days, last_month")
        ] = "last_28_days",
    ) -> str:
        """Review how the account performed in a period and suggest what to do next."""
        return (
            f"Give me an Instagram check-up for {period}.\n"
            "1. get_instagram_performance_summary for the period (compare with the previous period).\n"
            "2. get_instagram_top_media by views and by saves, and get_instagram_metrics_over_time "
            "(granularity week) for views, reach and follows.\n"
            "3. get_instagram_online_followers for posting times.\n"
            "Then write a short report: what went up or down and why (point to specific posts), what "
            "kind of content works (reels vs carousels vs photos, hooks, topics), and 3 concrete, "
            "data-backed ideas for the next posts. Quote exact numbers and the date range, and say when "
            "data is too thin to conclude."
        )

    @mcp.prompt(title="Post deep dive")
    def post_deep_dive(
        media_id: Annotated[str, Field(description="The post or reel to analyse (media ID)")],
    ) -> str:
        """Explain how one post performed and what to learn from it."""
        return (
            f"Do a deep dive on Instagram post {media_id}.\n"
            "Use get_instagram_media_insights for it, then list_instagram_media(include_insights=True, "
            "limit=30) to compare it with the account's typical post of the same type. Read its comments "
            "with list_instagram_comments(media_id=...) if the token allows it. Explain how it did "
            "against the usual (views, reach, saves, shares, watch time and skip rate for reels), what "
            "the comments say, and give specific suggestions for the next post's hook, format and caption."
        )

    @mcp.prompt(title="Audience portrait")
    def audience_portrait() -> str:
        """Describe who follows and who engages with the account."""
        return (
            "Describe my Instagram audience. Call get_instagram_audience for age, gender, city and country, "
            "once for followers and once for audience='engaged'. Compare the two (who follows vs who "
            "actually engages), add get_instagram_online_followers for when they're online, and summarise "
            "it as a short audience portrait with the key percentages."
        )

    @mcp.prompt(title="Benchmark accounts")
    def benchmark_accounts(
        usernames: Annotated[str, Field(description="Comma-separated usernames of similar accounts")],
    ) -> str:
        """Compare the account with similar public accounts."""
        return (
            f"Benchmark my Instagram account against: {usernames}.\n"
            "Use get_instagram_profile and list_instagram_media(limit=12) for mine, and "
            "get_instagram_public_account for each of the others (public numbers only: followers, posts, "
            "likes, comments, reel views). Compare posting frequency, formats, and engagement per follower "
            "(likes + comments / followers, on the same basis for everyone), and say what I could borrow."
        )
