"""`instagram-mcp check`: call every tool against your account and print what came back.

Runs the server in-process through the MCP client, so it tests the same code path an agent
uses. "ok" also means the answer has data: an empty answer is reported as a warning. Run it after
setup, after upgrading, or when Meta changes something.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from mcp import Client

from instagram_mcp.config import Settings
from instagram_mcp.instagram import GraphInstagramClient, InstagramAPI
from instagram_mcp.server import build_server

Summary = Callable[[dict[str, Any]], tuple[list[str], str | None]]


def _payload(result: Any) -> dict[str, Any]:
    if isinstance(result.structured_content, dict):
        return result.structured_content
    text = next((block.text for block in result.content if getattr(block, "text", None)), "{}")
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"message": text}
    return parsed if isinstance(parsed, dict) else {}


def _auth(p: dict[str, Any]) -> tuple[list[str], str | None]:
    days = p.get("token_expires_in_days")
    expiry = f"token valid {days} more days" if days is not None else f"token expiry: {p.get('token_expiry')}"
    return [f"account {p.get('handle')} on {p.get('api_host')}, {expiry}"], None


def _profile(p: dict[str, Any]) -> tuple[list[str], str | None]:
    line = f"{p.get('handle')}: {p.get('followers')} followers, {p.get('following')} following, {p.get('posts')} posts"
    return [line], None if p.get("followers") is not None else "no follower count"


def _summary(p: dict[str, Any]) -> tuple[list[str], str | None]:
    totals = p.get("totals") or {}
    shown = ", ".join(f"{k} {v}" for k, v in totals.items() if v is not None)
    lines = [f"{_dates(p)}: {shown}"]
    lines += [f"by content type: {json.dumps(p.get('by_content_type'))}"]
    return lines, None if totals.get("views") else "no views in the period"


def _trend(p: dict[str, Any]) -> tuple[list[str], str | None]:
    rows = p.get("rows") or []
    views = [row.get("views") for row in rows]
    return [f"{_dates(p)}: {len(rows)} {p.get('granularity')} rows, views {views}"], None if any(
        views
    ) else "no views"


def _audience(p: dict[str, Any]) -> tuple[list[str], str | None]:
    rows = p.get("rows") or []
    label = next((k for k in ("age", "code", "city", "country") if rows and k in rows[0]), "")
    shown = ", ".join(
        f"{r.get(label)} {r.get('app_share_pct') or r.get('share_of_known_pct') or r.get('share_pct')}%"
        for r in rows[:6]
    )
    return [f"{p.get('audience')} by {p.get('breakdown')}: {shown}"], None if rows else "no rows"


def _online(p: dict[str, Any]) -> tuple[list[str], str | None]:
    return [
        f"best hours ({p.get('timezone')}): {p.get('best_hours')} over {p.get('days_covered')} days"
    ], None


def _media(p: dict[str, Any]) -> tuple[list[str], str | None]:
    media = p.get("media") or []
    lines = []
    for item in media[:5]:
        caption = " ".join(str(item.get("caption") or "").split())[:40]
        metrics = ", ".join(
            f"{k} {item[k]}"
            for k in ("views", "reach", "interactions", "saves", "avg_watch_time_s")
            if item.get(k) is not None
        )
        lines.append(f"{item.get('type'):<8} {str(item.get('published_at'))[:10]}  {metrics}  {caption}")
    return [f"{len(media)} posts", *lines], None if media else "no posts"


def _stories(p: dict[str, Any]) -> tuple[list[str], str | None]:
    return [f"{p.get('count')} live stories"], None


def _comments(p: dict[str, Any]) -> tuple[list[str], str | None]:
    return [f"{p.get('count')} comments on {p.get('media_scanned')} posts"], None


def _tags(p: dict[str, Any]) -> tuple[list[str], str | None]:
    return [f"{p.get('count')} posts tag the account"], None


def _report(p: dict[str, Any]) -> tuple[list[str], str | None]:
    size_kb = len(json.dumps(p, ensure_ascii=False)) // 1024
    lines = [f"{_dates(p)}: {len(p.get('posts') or [])} posts, {size_kb} KB"]
    lines += [f"unavailable {k}: {v}" for k, v in (p.get("unavailable") or {}).items()]
    return lines, "some sections unavailable" if p.get("unavailable") else None


def _dates(p: dict[str, Any]) -> str:
    rng = p.get("date_range") or {}
    return f"{rng.get('start_date')}..{rng.get('end_date')} ({rng.get('timezone')})"


CHECKS: list[tuple[str, dict[str, Any], Summary]] = [
    ("get_instagram_auth_status", {}, _auth),
    ("get_instagram_profile", {}, _profile),
    ("get_instagram_performance_summary", {"period": "last_28_days"}, _summary),
    ("get_instagram_performance_summary", {"period": "last_90_days", "compare_to_previous": False}, _summary),
    ("get_instagram_metrics_over_time", {"granularity": "week", "period": "last_90_days"}, _trend),
    ("get_instagram_audience", {"breakdown": "age"}, _audience),
    ("get_instagram_audience", {"breakdown": "gender"}, _audience),
    ("get_instagram_audience", {"breakdown": "city", "limit": 5}, _audience),
    ("get_instagram_audience", {"breakdown": "country", "audience": "engaged", "limit": 5}, _audience),
    ("get_instagram_online_followers", {}, _online),
    ("list_instagram_media", {"limit": 5, "include_insights": True, "include_thumbnails": True}, _media),
    ("get_instagram_top_media", {"period": "last_90_days", "limit": 3}, _media),
    ("list_instagram_stories", {}, _stories),
    ("list_instagram_comments", {"posts": 3, "limit": 10}, _comments),
    ("list_instagram_tagged_media", {"limit": 5}, _tags),
    ("get_instagram_report", {"period": "last_28_days"}, _report),
]


async def run_checks(settings: Settings, api: InstagramAPI | None = None) -> int:
    api = api or GraphInstagramClient(settings)
    failures = warnings = 0
    async with Client(build_server(settings, api=api)) as client:
        for name, arguments, summarise in CHECKS:
            started = time.monotonic()
            result = await client.call_tool(name, arguments)
            elapsed = time.monotonic() - started
            label = f"{name}({', '.join(f'{k}={v}' for k, v in arguments.items())})"
            payload = _payload(result)
            if result.is_error or (name == "get_instagram_auth_status" and not payload.get("signed_in")):
                failures += 1
                print(f"FAIL  {label}  [{elapsed:.1f}s]", flush=True)
                print(
                    "      " + str(payload.get("message") or payload.get("next_step") or payload)[:300],
                    flush=True,
                )
                if name == "get_instagram_auth_status":
                    print("\nSave a token first: instagram-mcp auth", flush=True)
                    return 1
                continue
            lines, warning = summarise(payload)
            warnings += bool(warning)
            print(
                f"{'warn' if warning else 'ok  '}  {label}  [{elapsed:.1f}s]"
                + (f"  ({warning})" if warning else "")
            )
            for line in lines:
                print(f"      {line}")
            for note in payload.get("notes") or []:
                print(f"      note: {note}")
            print(end="", flush=True)
    passed = len(CHECKS) - failures
    print(f"\n{passed}/{len(CHECKS)} checks passed, {warnings} with warnings.", flush=True)
    return 1 if failures else 0
