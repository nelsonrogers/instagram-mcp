"""Account analytics: period totals, trends, audience, online followers and raw queries."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from instagram_mcp.context import (
    READ_ONLY,
    AppContext,
    EndDateArg,
    PeriodArg,
    SaveToArg,
    StartDateArg,
    tool_errors,
)
from instagram_mcp.insights import (
    ACCOUNT_KEYS,
    UNIQUE_METRICS,
    account_totals,
    breakdown_totals,
    change_pct,
    city_name,
    demographics,
    first_line,
    meta_hour_to_local,
    online_followers,
    parallel,
    pct,
    to_int,
)
from instagram_mcp.instagram import INVALID_PARAMETER_CODE, InstagramAPIError
from instagram_mcp.periods import DateRange, earliest_day, today_in

AccountMetric = Literal[
    "views",
    "reach",
    "accounts_engaged",
    "interactions",
    "likes",
    "comments",
    "shares",
    "saves",
    "replies",
    "reposts",
    "profile_link_taps",
    "follows",
    "unfollows",
    "net_follows",
]
Breakdown = Literal["age", "gender", "age_gender", "city", "country"]
DEFAULT_TREND_METRICS: list[AccountMetric] = ["views", "reach", "interactions", "follows", "unfollows"]
MAX_TREND_REQUESTS = 400
GENDER_LABELS = {"F": ("female", "Female"), "M": ("male", "Male"), "U": ("unknown", "Unknown")}
ADULT_AGES = ("18-24", "25-34", "35-44", "45-54", "55-64", "65+")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_.]+(?:/[A-Za-z0-9_.]+)*$")
_FORBIDDEN_PARAMS = {"access_token", "appsecret_proof", "method", "_method"}


# --------------------------------------------------------------------------- summary


def performance_summary(
    ctx: AppContext, rng: DateRange, *, compare_to_previous: bool, include_breakdowns: bool
) -> dict[str, Any]:
    api = ctx.api
    current = account_totals(api, rng)
    totals = current["values"]
    notes: list[str] = list(current["notes"])
    result: dict[str, Any] = {"date_range": rng.to_dict(), "totals": totals}
    if totals.get("interactions") is not None and totals.get("reach"):
        result["engagement_rate_pct"] = pct(totals["interactions"], totals["reach"], 2)

    if compare_to_previous:
        previous_range = rng.previous()
        earliest = earliest_day(today_in(ctx.tz))
        shortfall = (earliest - previous_range.start).days
        if 0 < shortfall <= 3 and previous_range.end >= earliest:
            # e.g. last_365_days: the previous year starts a day before Meta's two-year limit.
            previous_range = DateRange(
                earliest, previous_range.end, f"previous {rng.days - shortfall} days", ctx.tz
            )
            notes.append(f"The previous period is {shortfall} day(s) shorter: Meta keeps two years of data.")
            shortfall = 0
        if shortfall > 0:
            notes.append(
                "No comparison: the previous period starts more than two years ago, beyond Meta's data."
            )
        else:
            previous = account_totals(api, previous_range)["values"]
            result["previous_date_range"] = previous_range.to_dict()
            result["previous_totals"] = previous
            result["change_pct"] = {key: change_pct(totals.get(key), previous.get(key)) for key in totals}

    if include_breakdowns:
        by_type = breakdown_totals(api, rng, "media_product_type")
        by_follow = breakdown_totals(api, rng, "follow_type")
        notes += by_type["notes"] + by_follow["notes"]
        content: dict[str, dict[str, int]] = defaultdict(dict)
        for metric, rows in by_type["values"].items():
            for kind, value in rows.items():
                content[kind][metric] = value
        result["by_content_type"] = dict(content)
        audience: dict[str, Any] = {}
        for metric, rows in by_follow["values"].items():
            total = sum(rows.values())
            audience[metric] = {**rows, "followers_share_pct": pct(rows.get("followers", 0), total)}
        result["by_follower_status"] = audience
    notes.append(
        "Meta's numbers can still change for up to 48 hours. follows/unfollows come from Meta's "
        "follows_and_unfollows metric (unfollows include accounts that left Instagram)."
    )
    result["notes"] = notes
    return result


# --------------------------------------------------------------------------- trends


def metrics_over_time(
    ctx: AppContext,
    rng: DateRange,
    metrics: list[str],
    granularity: Literal["day", "week", "month"],
    include_follower_total: bool,
) -> dict[str, Any]:
    buckets = rng.buckets(granularity)
    wants_follows = (
        any(m in ("follows", "unfollows", "net_follows") for m in metrics) or include_follower_total
    )
    requests = sum(len(b.windows()) for b, _ in buckets) * (2 if wants_follows else 1)
    if requests > MAX_TREND_REQUESTS:
        raise ToolError(
            f"That needs about {requests} requests to Instagram ({len(buckets)} {granularity} buckets). "
            "Use a coarser granularity (week or month) or a shorter period."
        )
    keys = list(metrics)
    if include_follower_total and "net_follows" not in keys:
        keys.append("net_follows")

    def read(bucket: tuple[DateRange, bool]) -> dict[str, Any]:
        part, partial = bucket
        values = account_totals(ctx.api, part, keys, parallel_windows=False)["values"]
        return {
            "start_date": part.start.isoformat(),
            "end_date": part.end.isoformat(),
            "partial": partial,
            **values,
        }

    rows = parallel(read, buckets)
    notes: list[str] = []
    if any(m in UNIQUE_METRICS for m in keys) and any(b.days > 29 for b, _ in buckets):
        notes.append("reach and accounts_engaged are null for buckets longer than 29 days (Meta's limit).")
    if include_follower_total:
        current = to_int(ctx.profile("followers_count", fresh=True).get("followers_count"))
        if current is not None:
            later = 0
            for row in reversed(rows):
                row["followers_end"] = current - later
                later += row.get("net_follows") or 0
            notes.append(
                "followers_end is estimated backwards from today's follower count with net follows, so it "
                "drifts slightly (Meta's follows/unfollows don't match the count exactly)."
            )
        if "net_follows" not in metrics:
            for row in rows:
                row.pop("net_follows", None)
    if any(row["partial"] for row in rows):
        notes.append("Buckets cut off by the date range are flagged partial=true.")
    return {"date_range": rng.to_dict(), "granularity": granularity, "rows": rows, "notes": notes}


# --------------------------------------------------------------------------- audience


def audience_breakdown(
    ctx: AppContext,
    audience: Literal["followers", "engaged"],
    breakdown: Breakdown,
    timeframe: Literal["this_week", "this_month"],
    limit: int,
) -> dict[str, Any]:
    api = ctx.api
    notes: list[str] = []
    followers = (
        to_int(ctx.profile("followers_count").get("followers_count")) if audience == "followers" else None
    )
    result: dict[str, Any] = {"audience": audience, "breakdown": breakdown}
    if audience == "engaged":
        result["timeframe"] = timeframe

    if breakdown in ("age", "gender", "age_gender"):
        try:
            rows = demographics(api, audience, "age,gender", timeframe)
        except InstagramAPIError as exc:
            # The two-way breakdown works but isn't documented: fall back to the single one.
            if breakdown == "age_gender" or exc.code not in (INVALID_PARAMETER_CODE, 1):
                raise
            single = demographics(api, audience, breakdown, timeframe)
            rows = [((v[0], "U") if breakdown == "age" else ("", v[0]), c) for v, c in single]
            notes.append(
                f"Meta refused the age × gender breakdown ({first_line(exc)}); used {breakdown} alone."
            )
        total = sum(count for _, count in rows)
        if breakdown == "gender":
            by_gender: dict[str, int] = defaultdict(int)
            for (_, gender), count in rows:
                by_gender[gender] += count
            known = by_gender.get("F", 0) + by_gender.get("M", 0)
            out = [
                {
                    "code": GENDER_LABELS.get(g, (g.lower(), g))[0],
                    "label": GENDER_LABELS.get(g, (g.lower(), g))[1],
                    "count": count,
                    "share_pct": pct(count, total),
                    "share_of_known_pct": pct(count, known) if g in ("F", "M") else None,
                }
                for g, count in sorted(by_gender.items(), key=lambda kv: -kv[1])
            ]
            notes.append("The Instagram app shows share_of_known_pct (women vs men, unknown left out).")
        elif breakdown == "age":
            by_age: dict[str, int] = defaultdict(int)
            known_adults: dict[str, int] = defaultdict(int)
            for (age, gender), count in rows:
                by_age[age] += count
                if gender in ("F", "M") and age in ADULT_AGES:
                    known_adults[age] += count
            known_total = sum(known_adults.values())
            out = [
                {
                    "age": age,
                    "count": by_age[age],
                    "share_pct": pct(by_age[age], total),
                    "app_share_pct": pct(known_adults.get(age, 0), known_total)
                    if age in ADULT_AGES
                    else None,
                }
                for age in sorted(by_age, key=_age_order)
            ]
            notes.append(
                "app_share_pct matches the Instagram app's age chart, which counts only adults whose gender "
                "is known; share_pct is over every account in the breakdown."
            )
        else:
            out = [
                {
                    "age": age,
                    "gender": GENDER_LABELS.get(gender, (gender.lower(), gender))[0],
                    "count": count,
                    "share_pct": pct(count, total),
                }
                for (age, gender), count in sorted(rows, key=lambda r: (_age_order(r[0][0]), r[0][1]))
            ]
        result["rows"] = out
        result["total"] = total
    else:
        rows = demographics(api, audience, breakdown, timeframe)
        listed = sum(count for _, count in rows)
        base = followers or listed
        ranked = sorted(rows, key=lambda r: -r[1])[:limit]
        if breakdown == "city":
            out = [
                {
                    "city": city_name(values[0]),
                    "name": values[0],
                    "count": count,
                    "share_pct": pct(count, base),
                }
                for values, count in ranked
            ]
        else:
            out = [
                {"country": values[0].upper(), "count": count, "share_pct": pct(count, base)}
                for values, count in ranked
            ]
        result["rows"] = out
        result["listed_total"] = listed
        notes.append(
            "Meta returns only the top 45 values. "
            + (
                "share_pct is a share of all followers, like the app."
                if followers
                else "share_pct is a share of the accounts listed."
            )
        )
    if followers is not None:
        result["followers"] = followers
    notes.append("Meta shows demographics only for accounts with at least 100 followers or engaged accounts.")
    result["notes"] = notes
    return result


def _age_order(age: str) -> int:
    digits = re.match(r"\d+", age)
    return int(digits.group()) if digits else 999


# --------------------------------------------------------------------------- online followers


def online_followers_summary(ctx: AppContext, days: int) -> dict[str, Any]:
    today = today_in(ctx.tz)
    rng = DateRange(today - timedelta(days=days), today - timedelta(days=1), f"last_{days}_days", ctx.tz)
    daily = online_followers(ctx.api, rng)
    if not daily:
        raise ToolError(
            "Instagram returned no online-follower data (Meta needs at least 100 followers and keeps it for "
            "30 days)."
        )
    by_slot: dict[tuple[int, int], list[int]] = defaultdict(list)
    by_hour: dict[int, list[int]] = defaultdict(list)
    for day, hours in daily:
        for hour, count in hours.items():
            local = meta_hour_to_local(day, hour, ctx.tz)
            by_hour[local.hour].append(count)
            by_slot[(local.weekday(), local.hour)].append(count)
    averages = {hour: round(sum(v) / len(v)) for hour, v in sorted(by_hour.items())}
    peak = max(averages.values()) or 1
    hour_rows: list[dict[str, Any]] = [
        {"hour": hour, "avg_online": avg, "share_of_peak_pct": pct(avg, peak)}
        for hour, avg in averages.items()
    ]
    weekday_names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    slot_averages = {slot: round(sum(v) / len(v)) for slot, v in by_slot.items()}
    slots = [
        {"weekday": weekday_names[wd], "hour": h, "avg_online": avg}
        for (wd, h), avg in sorted(slot_averages.items(), key=lambda kv: -kv[1])
    ]
    return {
        "days_covered": len(daily),
        "timezone": str(ctx.tz),
        "by_hour": hour_rows,
        "best_hours": sorted(averages, key=lambda hour: -averages[hour])[:3],
        "best_slots": slots[:5],
        "notes": [
            "Followers online per hour, averaged over the days covered, in the report timezone (Meta "
            "reports US Pacific hours; they are converted here). Meta keeps 30 days of this data."
        ],
    }


# --------------------------------------------------------------------------- raw query


def raw_query(ctx: AppContext, path: str, params: dict[str, str] | None) -> dict[str, Any]:
    clean = path.strip().strip("/")
    if not _SAFE_PATH.match(clean) or ".." in clean:
        raise ToolError(
            "path must be a relative Graph API path such as 'me/insights' or '<media-id>/children'."
        )
    query = {str(k): str(v) for k, v in (params or {}).items()}
    forbidden = _FORBIDDEN_PARAMS & {k.lower() for k in query}
    if forbidden:
        raise ToolError(f"These parameters can't be set here: {', '.join(sorted(forbidden))}.")
    payload = ctx.api.get(clean, query)
    paging = payload.get("paging")
    if isinstance(paging, dict):
        # Meta's next/previous URLs embed the access token: keep only the cursors.
        payload["paging"] = {"cursors": paging.get("cursors")} if paging.get("cursors") else {}
        if paging.get("next"):
            payload["paging"]["has_next_page"] = True
    return payload


# --------------------------------------------------------------------------- tools


def register(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(title="Instagram performance summary", annotations=READ_ONLY)
    @tool_errors
    def get_instagram_performance_summary(
        period: PeriodArg = "last_28_days",
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        compare_to_previous: Annotated[
            bool, Field(description="Also read the previous period of equal length and compute % change.")
        ] = True,
        include_breakdowns: Annotated[
            bool,
            Field(
                description=(
                    "Add totals by content type (reel, post, carousel, story) and views/reach from followers "
                    "vs non-followers."
                )
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Account-level totals for a period, like the Insights overview in the Instagram app:
        views, reach, accounts engaged, interactions (likes, comments, shares, saves, replies,
        reposts), profile link taps, follows, unfollows and net follows, each with the previous
        period and % change, plus engagement rate (interactions / reach). Any range up to 2 years;
        reach and accounts_engaged need a range of 29 days or less (unique accounts can't be added
        up). For trends use get_instagram_metrics_over_time; for posts get_instagram_top_media."""
        rng = ctx.resolve(period, start_date, end_date)
        return performance_summary(
            ctx, rng, compare_to_previous=compare_to_previous, include_breakdowns=include_breakdowns
        )

    @mcp.tool(title="Instagram metrics over time", annotations=READ_ONLY)
    @tool_errors
    def get_instagram_metrics_over_time(
        metrics: Annotated[
            list[AccountMetric] | None,
            Field(description="Metrics per bucket. Default: views, reach, interactions, follows, unfollows."),
        ] = None,
        granularity: Annotated[
            Literal["day", "week", "month"], Field(description="Bucket size. Weeks run Monday to Sunday.")
        ] = "day",
        period: PeriodArg = "last_28_days",
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        include_follower_total: Annotated[
            bool,
            Field(
                description=(
                    "Add followers_end per bucket: the follower count at the end of each bucket, estimated "
                    "backwards from today's count (for growth charts)."
                )
            ),
        ] = False,
        save_to: SaveToArg = None,
    ) -> dict[str, Any]:
        """A time series of account metrics by day, week or month, for trend analysis and growth
        charts (views, reach, interactions, likes, comments, shares, saves, follows, unfollows,
        net follows, …). One Instagram request per bucket: use week or month for long periods.
        Buckets cut off by the range are flagged partial=true."""
        rng = ctx.resolve(period, start_date, end_date)
        result = metrics_over_time(
            ctx, rng, list(metrics or DEFAULT_TREND_METRICS), granularity, include_follower_total
        )
        if save_to:
            result["saved_to"] = ctx.save_rows(result["rows"], save_to)
        return result

    @mcp.tool(title="Instagram audience", annotations=READ_ONLY)
    @tool_errors
    def get_instagram_audience(
        breakdown: Annotated[
            Literal["age", "gender", "age_gender", "city", "country"],
            Field(description="age, gender, age_gender (both together), city or country (top 45)."),
        ],
        audience: Annotated[
            Literal["followers", "engaged"],
            Field(
                description=(
                    "'followers' = who follows the account (a snapshot, like the app's audience view); "
                    "'engaged' = accounts that interacted with content during the timeframe."
                )
            ),
        ] = "followers",
        timeframe: Annotated[
            Literal["this_week", "this_month"],
            Field(description="Window for the engaged audience (Meta's own: the current week or month)."),
        ] = "this_month",
        limit: Annotated[int, Field(ge=1, le=45, description="Maximum rows for city and country.")] = 45,
    ) -> dict[str, Any]:
        """Who the audience is: age, gender, age × gender, top cities and top countries of the
        followers, or of the accounts that engaged with the content. Rows have counts and
        share_pct; age also has app_share_pct (the app's own rule) and gender share_of_known_pct."""
        return audience_breakdown(ctx, audience, breakdown, timeframe, limit)

    @mcp.tool(title="Instagram online followers", annotations=READ_ONLY)
    @tool_errors
    def get_instagram_online_followers(
        days: Annotated[int, Field(ge=1, le=30, description="Days to average over (Meta keeps 30).")] = 28,
    ) -> dict[str, Any]:
        """When followers are on Instagram: average followers online per hour of the day, and the
        best weekday + hour slots, in the report timezone. Use it to pick posting times."""
        return online_followers_summary(ctx, days)

    @mcp.tool(title="Raw Instagram API query", annotations=READ_ONLY)
    @tool_errors
    def query_instagram_api(
        path: Annotated[
            str,
            Field(
                description=(
                    "Graph API path, relative: 'me' is the connected account, e.g. 'me/insights', "
                    "'me/media', '<media-id>/insights', '<media-id>/children'."
                )
            ),
        ],
        params: Annotated[
            dict[str, str] | None,
            Field(
                description=(
                    "Query parameters, e.g. {'metric': 'views,reach', 'period': 'day', 'metric_type': "
                    "'total_value', 'since': '1730000000', 'until': '1732000000'}."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Run any read-only GET on Meta's Instagram Graph API for the connected account and return
        Meta's raw JSON: the escape hatch when no other tool fits. Account insights allow at most
        30 days between since and until (Unix seconds) and go back 2 years. Pagination: pass
        'after' from paging.cursors. Meta's error message is returned if a request is invalid."""
        return raw_query(ctx, path, params)


__all__ = ["ACCOUNT_KEYS", "register"]
