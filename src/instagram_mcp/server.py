"""MCP server factory."""

from __future__ import annotations

from mcp.server import MCPServer

from instagram_mcp import __version__, prompts
from instagram_mcp.config import Settings
from instagram_mcp.context import AppContext
from instagram_mcp.instagram import GraphInstagramClient, InstagramAPI
from instagram_mcp.tools import register_all

INSTRUCTIONS = """\
Read-only tools for an Instagram Business or Creator account (Meta's official Instagram API).

Getting started: if a tool reports a sign-in problem, call get_instagram_auth_status and relay its
`next_step`. Signing in happens in a terminal (`instagram-mcp auth`), never in the chat: tokens
must not be pasted here.

Picking tools:
- "How am I doing": get_instagram_performance_summary (period vs previous period).
- Trends and growth charts: get_instagram_metrics_over_time (day/week/month buckets).
- Best and worst content: get_instagram_top_media; catalogue: list_instagram_media
  (include_insights for per-post numbers); specific posts: get_instagram_media_insights.
- Audience (age, gender, cities, countries; followers or engaged accounts): get_instagram_audience.
- When to post: get_instagram_online_followers. Live stories: list_instagram_stories.
- Comments: list_instagram_comments. Mentions/UGC: list_instagram_tagged_media.
- Benchmarking other accounts: get_instagram_public_account.
- A full snapshot in one call (monthly reports): get_instagram_report.
- Anything else: query_instagram_api (raw Graph API GET).

Data notes: every result states its exact date_range and timezone: quote it. Rolling periods end
yesterday; Meta's numbers can still change for 48 hours and go back 2 years. Post metrics are
lifetime totals. Percentages are 0-100, *_s are seconds. Anything Instagram can't provide is null
or listed in `unavailable`/`notes`: say so instead of inventing numbers.
"""


def build_server(settings: Settings | None = None, api: InstagramAPI | None = None) -> MCPServer:
    """Create the MCP server. ``api`` can be injected for tests."""
    settings = settings or Settings.from_env()
    ctx = AppContext(settings=settings, api=api or GraphInstagramClient(settings))
    mcp = MCPServer(
        name="instagram-mcp",
        title="Instagram",
        description="Read-only Instagram analytics for creators: performance, trends, audience, posts, reels, stories and comments.",
        instructions=INSTRUCTIONS,
        website_url="https://github.com/nelsonrogers/instagram-mcp",
        version=__version__,
    )
    register_all(mcp, ctx)
    prompts.register(mcp)
    return mcp
