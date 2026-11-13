"""Reading and normalising Instagram data: account totals, breakdowns, demographics and posts.

Everything here speaks Meta's API on one side and returns snake_case values on the other, so
the tools stay small. Facts below were checked against a real account in November 2026 (Graph
API v26.0, graph.facebook.com); docs/decisions.md lists them.

- Account insights accept at most 30 days per request and go back two years; longer ranges are
  read window by window (``DateRange.windows``) and summed. ``reach`` and ``accounts_engaged``
  count unique accounts, so they can't be summed: they are only given for ranges that fit in
  one request.
- ``follows_and_unfollows`` only answers with ``breakdown=follow_type``: ``FOLLOWER`` is the
  accounts that started following, ``NON_FOLLOWER`` the ones that stopped.
- Meta's own daily buckets (``time_series``, ``online_followers``) are US Pacific days.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from instagram_mcp.instagram import (
    INVALID_PARAMETER_CODE,
    RATE_LIMIT_CODES,
    InstagramAPI,
    InstagramAPIError,
    paginate,
)
from instagram_mcp.periods import DateRange
from instagram_mcp.thumbnails import ThumbnailError, webp_data_uri

META_TZ = ZoneInfo("America/Los_Angeles")  # Meta's insight days
PARALLEL_REQUESTS = 4

# --------------------------------------------------------------------------- catalogues

# Account metrics read with metric_type=total_value: API name -> output key.
ACCOUNT_METRICS: dict[str, str] = {
    "views": "views",
    "reach": "reach",
    "accounts_engaged": "accounts_engaged",
    "total_interactions": "interactions",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
    "saves": "saves",
    "replies": "replies",
    "reposts": "reposts",
    "profile_links_taps": "profile_link_taps",
}
FOLLOW_KEYS = ("follows", "unfollows", "net_follows")
ACCOUNT_KEYS: tuple[str, ...] = (*ACCOUNT_METRICS.values(), *FOLLOW_KEYS)
UNIQUE_METRICS = {"reach", "accounts_engaged"}  # unique accounts: not additive across windows
DEFAULT_ACCOUNT_METRICS: tuple[str, ...] = ACCOUNT_KEYS

# Breakdowns Meta accepts on account totals, and the metrics that take them.
BREAKDOWNS: dict[str, tuple[str, ...]] = {
    "media_product_type": ("views", "reach", "total_interactions", "likes", "comments", "shares", "saves"),
    "follow_type": ("views", "reach"),
    "contact_button_type": ("profile_links_taps",),
}
CODES: dict[str, str] = {
    # media_product_type
    "REEL": "reel",
    "POST": "post",
    "STORY": "story",
    "CAROUSEL_CONTAINER": "carousel",
    "AD": "ad",
    "IGTV": "igtv",
    # follow_type
    "FOLLOWER": "followers",
    "NON_FOLLOWER": "non_followers",
    "UNKNOWN": "unknown",
}

# Post types, from media_product_type / media_type.
MEDIA_KINDS = {
    "REELS": "reel",
    "STORY": "story",
    "CAROUSEL_ALBUM": "carousel",
    "IMAGE": "image",
    "VIDEO": "video",
}
MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,timestamp,permalink,shortcode,thumbnail_url,media_url,"
    "like_count,comments_count,is_comment_enabled,is_shared_to_feed"
)
MEDIA_PAGE_SIZE = 50
MAX_MEDIA_SCANNED = 1000
OLDER_POSTS_BEFORE_STOP = 3  # tolerate a few out-of-order (e.g. pinned) posts while scanning

# Lifetime metrics per post type (Meta refuses the others with code 100). Reels checked live;
# feed and story sets from Meta's IG Media Insights reference.
MEDIA_METRICS: dict[str, tuple[str, ...]] = {
    "reel": (
        "views",
        "reach",
        "likes",
        "comments",
        "shares",
        "saved",
        "total_interactions",
        "reposts",
        "ig_reels_avg_watch_time",
        "ig_reels_video_view_total_time",
        "reels_skip_rate",
    ),
    "feed": (
        "views",
        "reach",
        "likes",
        "comments",
        "shares",
        "saved",
        "total_interactions",
        "follows",
        "profile_visits",
        "profile_activity",
        "reposts",
    ),
    "story": (
        "views",
        "reach",
        "replies",
        "shares",
        "reposts",
        "total_interactions",
        "follows",
        "profile_visits",
        "profile_activity",
        "navigation",
    ),
}
MEDIA_METRIC_KEYS: dict[str, str] = {
    "views": "views",
    "reach": "reach",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
    "saved": "saves",
    "total_interactions": "interactions",
    "reposts": "reposts",
    "follows": "follows",
    "profile_visits": "profile_visits",
    "profile_activity": "profile_activity",
    "replies": "replies",
    "navigation": "navigation",
    "reels_skip_rate": "skip_rate_pct",
}
_REFUSED_METRIC = re.compile(r"\bthe\s+([a-z_]+)\s+metric\b|\bmetric\s*\(([a-z_]+)\)", re.IGNORECASE)

DEMOGRAPHIC_METRICS = {"followers": "follower_demographics", "engaged": "engaged_audience_demographics"}
DEMOGRAPHIC_TIMEFRAMES = {"followers": "this_month", "engaged": "this_month"}


class NoDataError(InstagramAPIError):
    """Instagram answered, but without the numbers asked for."""


# --------------------------------------------------------------------------- small helpers


def to_int(value: Any) -> int | None:
    try:
        return None if value is None or isinstance(value, bool) else int(value)
    except (TypeError, ValueError):
        return None


def pct(part: float | None, whole: float | None, digits: int = 1) -> float | None:
    if part is None or not whole:
        return None
    return round(100 * part / whole, digits)


def change_pct(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return round(100 * (current - previous) / previous, 1)


def code(value: Any) -> str:
    raw = str(value)
    return CODES.get(raw, raw.lower())


def first_line(exc: BaseException) -> str:
    text = str(exc)
    return text.splitlines()[0] if text else type(exc).__name__


def parallel(fn: Any, items: Iterable[Any]) -> list[Any]:
    """``[fn(item)]`` with a few requests in flight (the client is thread-safe)."""
    items = list(items)
    if len(items) <= 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=PARALLEL_REQUESTS) as pool:
        return list(pool.map(fn, items))


def _serial(fn: Any, items: Iterable[Any]) -> list[Any]:
    return [fn(item) for item in items]


def is_invalid_parameter(exc: InstagramAPIError) -> bool:
    return exc.code == INVALID_PARAMETER_CODE


def parse_time(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


# --------------------------------------------------------------------------- account totals


def _total_values(payload: dict[str, Any]) -> dict[str, int | None]:
    return {
        str(metric.get("name")): to_int((metric.get("total_value") or {}).get("value"))
        for metric in payload.get("data") or []
    }


def _breakdown_values(payload: dict[str, Any]) -> dict[str, dict[str, int]]:
    """``{metric: {dimension code: value}}`` from a total_value answer with one breakdown."""
    out: dict[str, dict[str, int]] = {}
    for metric in payload.get("data") or []:
        rows: dict[str, int] = {}
        for block in (metric.get("total_value") or {}).get("breakdowns") or []:
            for result in block.get("results") or []:
                values = result.get("dimension_values") or []
                count = to_int(result.get("value"))
                if values and count is not None:
                    key = code(values[-1])
                    rows[key] = rows.get(key, 0) + count
        out[str(metric.get("name"))] = rows
    return out


def _insights(api: InstagramAPI, window: DateRange, metrics: Iterable[str], **extra: Any) -> dict[str, Any]:
    return api.get(
        "me/insights",
        {
            "metric": ",".join(metrics),
            "period": "day",
            "metric_type": "total_value",
            "since": window.since,
            "until": window.until,
            **extra,
        },
    )


def account_totals(
    api: InstagramAPI,
    rng: DateRange,
    keys: Iterable[str] = DEFAULT_ACCOUNT_METRICS,
    *,
    parallel_windows: bool = True,
) -> dict[str, Any]:
    """Totals of account metrics over ``rng`` (any length up to two years).

    Returns ``{"values": {key: int | None}, "notes": [...]}``. Unique-account metrics are
    ``None`` when the range needs more than one request.
    """
    wanted = list(dict.fromkeys(keys))
    unknown = [key for key in wanted if key not in ACCOUNT_KEYS]
    if unknown:
        raise ValueError(f"Unknown metric {unknown[0]!r}. Valid: {', '.join(ACCOUNT_KEYS)}.")
    windows = rng.windows()
    notes: list[str] = []
    api_names = [name for name, key in ACCOUNT_METRICS.items() if key in wanted]
    if len(windows) > 1:
        skipped = [name for name in api_names if name in UNIQUE_METRICS]
        if skipped:
            notes.append(
                f"{', '.join(ACCOUNT_METRICS[n] for n in skipped)}: unique accounts can't be added up across "
                f"Meta's 30-day request limit, so they are null for this {rng.days}-day range. Use a range of "
                "up to 29 days, or get_instagram_metrics_over_time for weekly values."
            )
        api_names = [name for name in api_names if name not in UNIQUE_METRICS]
    values: dict[str, int | None] = dict.fromkeys(wanted)
    run = parallel if parallel_windows else _serial
    if api_names:
        for payload in run(lambda w: _insights(api, w, api_names), windows):
            for name, value in _total_values(payload).items():
                key = ACCOUNT_METRICS.get(name)
                if key in values and value is not None:
                    values[key] = (values[key] or 0) + value
    if any(key in wanted for key in FOLLOW_KEYS):
        follows = follows_and_unfollows(api, rng, parallel_windows=parallel_windows)
        for key in FOLLOW_KEYS:
            if key in values:
                values[key] = follows[key]
    return {"values": values, "notes": notes}


def follows_and_unfollows(
    api: InstagramAPI, rng: DateRange, *, parallel_windows: bool = True
) -> dict[str, int | None]:
    follows = unfollows = 0
    seen = False
    run = parallel if parallel_windows else _serial
    for payload in run(
        lambda w: _insights(api, w, ["follows_and_unfollows"], breakdown="follow_type"), rng.windows()
    ):
        rows = _breakdown_values(payload).get("follows_and_unfollows") or {}
        if rows:
            seen = True
        follows += rows.get("followers", 0)
        unfollows += rows.get("non_followers", 0)
    if not seen:
        return {"follows": None, "unfollows": None, "net_follows": None}
    return {"follows": follows, "unfollows": unfollows, "net_follows": follows - unfollows}


def breakdown_totals(api: InstagramAPI, rng: DateRange, breakdown: str) -> dict[str, Any]:
    """``{"values": {metric key: {dimension: value}}, "notes": [...]}`` for one breakdown."""
    windows = rng.windows()
    names = list(BREAKDOWNS[breakdown])
    notes: list[str] = []
    if len(windows) > 1 and any(n in UNIQUE_METRICS for n in names):
        names = [n for n in names if n not in UNIQUE_METRICS]
        notes.append(
            f"reach by {breakdown} is left out: unique accounts can't be added up over {rng.days} days."
        )
    merged: dict[str, dict[str, int]] = {}
    for payload in parallel(lambda w: _insights(api, w, names, breakdown=breakdown), windows):
        for name, rows in _breakdown_values(payload).items():
            target = merged.setdefault(ACCOUNT_METRICS.get(name, name), {})
            for dim, value in rows.items():
                target[dim] = target.get(dim, 0) + value
    return {"values": merged, "notes": notes}


# --------------------------------------------------------------------------- demographics


def parse_demographic_rows(
    payload: dict[str, Any], keys: list[str], metric: str | None = None
) -> list[tuple[tuple[str, ...], int]]:
    """``[((value per key), count)]`` from a demographics answer.

    ``keys`` are the breakdowns asked for (``["country"]``, ``["age", "gender"]``). Meta's
    ``dimension_keys`` say where each one is in ``dimension_values`` (they can start with
    ``"timeframe"``). A single breakdown missing from ``dimension_keys`` is taken as the last value.
    """
    rows: list[tuple[tuple[str, ...], int]] = []
    for entry in payload.get("data") or []:
        if metric and entry.get("name") != metric:
            continue
        for block in (entry.get("total_value") or {}).get("breakdowns") or []:
            dimensions = [str(key) for key in block.get("dimension_keys") or []]
            if all(key in dimensions for key in keys):
                positions = [dimensions.index(key) for key in keys]
            elif len(keys) == 1:
                positions = [-1]
            else:
                continue
            for result in block.get("results") or []:
                values = result.get("dimension_values") or []
                count = to_int(result.get("value"))
                if count is None or not values or max(positions) >= len(values):
                    continue
                rows.append((tuple(str(values[position]) for position in positions), count))
    return rows


def demographics(
    api: InstagramAPI, audience: str, breakdown: str, timeframe: str | None = None
) -> list[tuple[tuple[str, ...], int]]:
    """Rows for one demographic breakdown (``age``, ``gender``, ``city``, ``country`` or
    ``age,gender``) of followers or engaged accounts."""
    metric = DEMOGRAPHIC_METRICS[audience]
    params = {
        "metric": metric,
        "period": "lifetime",
        "metric_type": "total_value",
        "breakdown": breakdown,
        "timeframe": timeframe or DEMOGRAPHIC_TIMEFRAMES[audience],
    }
    rows = parse_demographic_rows(api.get("me/insights", params), breakdown.split(","), metric)
    if not rows:
        raise NoDataError(
            f"Instagram returned no {audience} demographics for '{breakdown}' (Meta needs at least 100 "
            f"{'followers' if audience == 'followers' else 'engaged accounts'} to show them)."
        )
    return rows


def city_name(raw: str) -> str:
    """The app shows the city alone ("Paris"); the API adds the region ("Paris, Île-de-France").
    Names whose two parts are the same place ("Réunion, Réunion") are kept as they are."""
    city, _, region = raw.partition(",")
    city, region = city.strip(), region.strip()
    if not region or region.casefold() == city.casefold():
        return raw.strip()
    return city


# --------------------------------------------------------------------------- online followers


def online_followers(api: InstagramAPI, rng: DateRange) -> list[tuple[str, dict[int, int]]]:
    """``[(Meta day, {hour: followers online})]``; Meta's days and hours are US Pacific time."""
    out: list[tuple[str, dict[int, int]]] = []
    for window in rng.windows():
        payload = api.get(
            "me/insights",
            {
                "metric": "online_followers",
                "period": "lifetime",
                "since": window.since,
                "until": window.until,
            },
        )
        for entry in payload.get("data") or []:
            for value in entry.get("values") or []:
                hours = {
                    int(h): int(v) for h, v in (value.get("value") or {}).items() if to_int(v) is not None
                }
                end = parse_time(value.get("end_time"))
                if hours and end is not None:
                    day = (end.astimezone(META_TZ) - timedelta(days=1)).date().isoformat()
                    out.append((day, hours))
    return out


def meta_hour_to_local(day: str, hour: int, tz: tzinfo) -> datetime:
    start = datetime.fromisoformat(day).replace(tzinfo=META_TZ) + timedelta(hours=hour)
    return start.astimezone(tz)


# --------------------------------------------------------------------------- posts


def media_kind(item: dict[str, Any]) -> str:
    """``reel``, ``story``, ``carousel``, ``image`` or ``video``."""
    product = str(item.get("media_product_type") or "")
    if product in ("REELS", "STORY"):
        return MEDIA_KINDS[product]
    return MEDIA_KINDS.get(str(item.get("media_type")), "image")


def metric_family(kind: str) -> str:
    return kind if kind in ("reel", "story") else "feed"


def scan_media(
    api: InstagramAPI,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    max_items: int = MAX_MEDIA_SCANNED,
    fields: str = MEDIA_FIELDS,
) -> Iterator[dict[str, Any]]:
    """Posts published in ``[since, until)``, newest first.

    ``/me/media`` lists the newest first; the scan stops after a few consecutive posts older than
    ``since``, so one out-of-order item (a pinned post) can't end it early.
    """
    older_in_a_row = 0
    for item in paginate(api, "me/media", {"fields": fields, "limit": MEDIA_PAGE_SIZE}, max_items=max_items):
        published = parse_time(item.get("timestamp"))
        if since is not None and published is not None and published < since:
            older_in_a_row += 1
            if older_in_a_row >= OLDER_POSTS_BEFORE_STOP:
                return
            continue
        older_in_a_row = 0
        if until is not None and published is not None and published >= until:
            continue
        yield item


def summarize_media(item: dict[str, Any], tz: tzinfo) -> dict[str, Any]:
    published = parse_time(item.get("timestamp"))
    return {
        "id": str(item.get("id")),
        "type": media_kind(item),
        "published_at": published.astimezone(tz).isoformat() if published else None,
        "caption": item.get("caption") or "",
        "permalink": item.get("permalink"),
        "shortcode": item.get("shortcode"),
        "likes": to_int(item.get("like_count")),
        "comments": to_int(item.get("comments_count")),
        "comments_enabled": item.get("is_comment_enabled"),
        "shared_to_feed": item.get("is_shared_to_feed"),
    }


class MediaInsightsReader:
    """Reads lifetime insights per post, remembering metrics Meta refuses for a post type.

    One request per post. When Meta refuses a metric as an invalid parameter, it is dropped for
    that post type and the request repeated; the dropped metrics are reported in ``notes``.
    After a rate-limit error the remaining posts are skipped.
    """

    def __init__(self, api: InstagramAPI) -> None:
        self.api = api
        self.refused: dict[str, set[str]] = {}
        self.rate_limited: InstagramAPIError | None = None
        self.failures: list[str] = []

    def metrics_for(self, family: str) -> list[str]:
        refused = self.refused.get(family, set())
        return [m for m in MEDIA_METRICS[family] if m not in refused]

    def read(self, media_id: str, family: str) -> dict[str, Any] | None:
        if self.rate_limited is not None:
            self.failures.append(f"skipped after a rate-limit error ({first_line(self.rate_limited)})")
            return None
        for _ in range(len(MEDIA_METRICS[family])):
            wanted = self.metrics_for(family)
            if not wanted:
                return {}
            try:
                return self._parse(
                    self.api.get(f"{media_id}/insights", {"metric": ",".join(wanted)}), media_id
                )
            except InstagramAPIError as exc:
                if exc.code in RATE_LIMIT_CODES:
                    self.rate_limited = exc
                    self.failures.append(first_line(exc))
                    return None
                refused = _refused_metric(exc, wanted)
                if not is_invalid_parameter(exc) or refused is None:
                    self.failures.append(first_line(exc))
                    return None
                self.refused.setdefault(family, set()).add(refused)
        return None

    def _parse(self, payload: dict[str, Any], media_id: str) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        for metric in payload.get("data") or []:
            name = str(metric.get("name"))
            series = metric.get("values") or []
            raw[name] = series[0].get("value") if series else (metric.get("total_value") or {}).get("value")
        if "navigation" in raw and not isinstance(raw["navigation"], dict):
            raw["navigation"] = self._navigation(media_id) or to_int(raw["navigation"])
        return normalise_media_metrics(raw)

    def _navigation(self, media_id: str) -> dict[str, int] | None:
        try:
            payload = self.api.get(
                f"{media_id}/insights", {"metric": "navigation", "breakdown": "story_navigation_action_type"}
            )
        except InstagramAPIError:
            return None
        rows = _breakdown_values(payload).get("navigation")
        return rows or None

    def notes(self) -> list[str]:
        notes = [
            f"Meta doesn't provide {', '.join(sorted(metrics))} for {family} posts"
            for family, metrics in sorted(self.refused.items())
        ]
        if self.failures:
            notes.append(f"{len(self.failures)} post(s) have no insights (first reason: {self.failures[0]})")
        return notes


def _refused_metric(exc: InstagramAPIError, wanted: list[str]) -> str | None:
    """The metric Meta names in an "does not support the X metric" answer, if it is one we asked for."""
    text = str(exc)
    for match in _REFUSED_METRIC.finditer(text):
        name = match.group(1) or match.group(2)
        if name in wanted:
            return name
    named = [metric for metric in wanted if re.search(rf"\b{re.escape(metric)}\b", text)]
    return named[0] if len(named) == 1 else None


def normalise_media_metrics(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, key in MEDIA_METRIC_KEYS.items():
        if name in raw:
            out[key] = raw[name] if isinstance(raw[name], dict) else to_int(raw[name])
    if "ig_reels_avg_watch_time" in raw:
        value = to_int(raw["ig_reels_avg_watch_time"])
        out["avg_watch_time_s"] = round(value / 1000, 1) if value is not None else None
    if "ig_reels_video_view_total_time" in raw:
        value = to_int(raw["ig_reels_video_view_total_time"])
        out["total_watch_time_h"] = round(value / 3_600_000, 1) if value is not None else None
    if out.get("interactions") is not None and out.get("reach"):
        out["engagement_rate_pct"] = pct(out["interactions"], out["reach"], 2)
    return out


# --------------------------------------------------------------------------- thumbnails


def _still_image(item: dict[str, Any]) -> str | None:
    """A video's cover (never its ``media_url``, which is the video file), else the photo."""
    url = item.get("thumbnail_url") if item.get("media_type") == "VIDEO" else item.get("media_url")
    return str(url) if url else None


def image_url(api: InstagramAPI, item: dict[str, Any]) -> str | None:
    if item.get("media_type") != "CAROUSEL_ALBUM":
        return _still_image(item)
    # An album's own media_url may be its first child's video: use the first child instead.
    children = api.get(f"{item['id']}/children", {"fields": "media_type,media_url,thumbnail_url"})
    for child in children.get("data") or []:
        url = _still_image(child)
        if url:
            return url
    return None


def thumbnail(api: InstagramAPI, item: dict[str, Any]) -> str:
    """The post's image as a small WebP data URI (raises InstagramAPIError or ThumbnailError)."""
    url = image_url(api, item)
    if not url:
        raise ThumbnailError("Instagram gives no image for this post.")
    return webp_data_uri(api.download(url))


def utc_from_timestamp(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)
