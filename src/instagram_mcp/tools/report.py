"""One-call report: profile, period performance, audience, posts and best posting hours."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import Annotated, Any, TypeVar

from mcp.server import MCPServer
from pydantic import Field

from instagram_mcp import periods
from instagram_mcp.auth import AuthError
from instagram_mcp.context import (
    READ_ONLY,
    AppContext,
    EndDateArg,
    PeriodArg,
    StartDateArg,
    tool_errors,
)
from instagram_mcp.insights import first_line, scan_media
from instagram_mcp.instagram import InstagramAPIError
from instagram_mcp.periods import DateRange, midnight
from instagram_mcp.thumbnails import ThumbnailError
from instagram_mcp.tools.account import profile
from instagram_mcp.tools.analytics import (
    Breakdown,
    audience_breakdown,
    online_followers_summary,
    performance_summary,
)
from instagram_mcp.tools.content import enrich

SCHEMA_VERSION = "1"
TOP_PLACES = 10
T = TypeVar("T")


def _section(unavailable: dict[str, str], key: str, fn: Callable[[], T]) -> T | None:
    """Run one section; record (instead of raising) a recoverable failure."""
    try:
        return fn()
    except AuthError:
        raise
    except (InstagramAPIError, ThumbnailError, KeyError, TypeError, ValueError, IndexError) as exc:
        reason = first_line(exc)
        if not isinstance(exc, InstagramAPIError | ThumbnailError):
            reason = f"Unexpected response from Instagram ({type(exc).__name__}: {reason})"
        unavailable[key] = reason
        return None


def _rows(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((result or {}).get("rows") or [])


def build_report(ctx: AppContext, rng: DateRange, *, include_thumbnails: bool) -> dict[str, Any]:
    """The report. Raises only when the account itself can't be read (sign-in problems)."""
    unavailable: dict[str, str] = {}
    notes: list[str] = []
    account = profile(ctx)
    account.pop("notes", None)

    performance = _section(
        unavailable,
        "performance",
        lambda: performance_summary(ctx, rng, compare_to_previous=True, include_breakdowns=True),
    )
    if performance:
        notes += performance.pop("notes", [])

    audience: dict[str, Any] = {}
    sections: tuple[tuple[str, Breakdown, int], ...] = (
        ("gender", "gender", 3),
        ("age", "age", 10),
        ("cities", "city", TOP_PLACES),
        ("countries", "country", TOP_PLACES),
    )
    for key, breakdown, limit in sections:
        result = _section(
            unavailable,
            f"audience.{key}",
            partial(audience_breakdown, ctx, "followers", breakdown, "this_month", limit),
        )
        audience[key] = _rows(result)

    since, until = midnight(rng.start, ctx.tz), midnight(rng.end + timedelta(days=1), ctx.tz)
    items = _section(unavailable, "posts", lambda: list(scan_media(ctx.api, since=since, until=until))) or []
    posts, post_notes = enrich(ctx, items, insights=True, thumbnails=include_thumbnails)
    notes += post_notes

    best = _section(unavailable, "best_hours", lambda: online_followers_summary(ctx, 28))
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": periods.now(ctx.tz).isoformat(timespec="seconds"),
        "source": "Instagram Graph API",
        "date_range": rng.to_dict(),
        "profile": account,
        "performance": performance,
        "audience": audience,
        "posts": posts,
        "best_hours": {k: best[k] for k in ("best_hours", "best_slots", "timezone")} if best else None,
        "unavailable": unavailable,
        "notes": [
            *notes,
            "Post metrics are lifetime totals for the posts published in date_range. Audience is the "
            "followers' current demographics.",
        ],
    }


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(title="Instagram report", annotations=READ_ONLY)
    @tool_errors
    def get_instagram_report(
        period: PeriodArg = "last_28_days",
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        include_thumbnails: Annotated[
            bool,
            Field(description="Embed each post's image as a small WebP data URI (~5 KB per post)."),
        ] = False,
        save_to: Annotated[
            str | None, Field(description="Optional .json file to also save the report to.")
        ] = None,
    ) -> dict[str, Any]:
        """Everything about the account for a period in one stable JSON snapshot (schema_version 1):
        profile, performance totals vs the previous period (with content-type and follower
        splits), follower audience (gender, age, top 10 cities and countries), every post published
        in the period with its lifetime insights, and the best hours to post. Sections Instagram
        can't provide are null or empty and explained in `unavailable`; the call still succeeds.
        Shape: docs/report.md."""
        rng = ctx.resolve(period, start_date, end_date)
        report = build_report(ctx, rng, include_thumbnails=include_thumbnails)
        if save_to:
            report["saved_to"] = ctx.save_json(report, save_to)
        return report
