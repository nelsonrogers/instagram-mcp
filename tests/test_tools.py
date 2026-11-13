"""End-to-end tests through the MCP client against the fake Instagram API."""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pytest
from mcp import Client

from instagram_mcp.config import Settings
from instagram_mcp.selfcheck import run_checks
from tests.conftest import call, call_error
from tests.fakes import AGE_GENDER, FakeInstagramAPI, daily_values

pytestmark = pytest.mark.anyio

TOOLS = {
    "get_instagram_auth_status",
    "get_instagram_profile",
    "get_instagram_performance_summary",
    "get_instagram_metrics_over_time",
    "get_instagram_audience",
    "get_instagram_online_followers",
    "query_instagram_api",
    "list_instagram_media",
    "get_instagram_media_insights",
    "get_instagram_top_media",
    "list_instagram_stories",
    "list_instagram_comments",
    "list_instagram_tagged_media",
    "get_instagram_public_account",
    "get_instagram_report",
}


def days(start: str, end: str) -> list[date]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def total(metric: str, start: str, end: str) -> int:
    return sum(daily_values(day)[metric] for day in days(start, end))


# --------------------------------------------------------------------------- catalogue


async def test_tools_are_read_only_described_and_prefixed(client: Client) -> None:
    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == TOOLS
    for tool in tools.values():
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.description and len(tool.description) > 80, tool.name
        assert "instagram" in tool.name  # never collides with other servers' tools
        for name, schema in tool.input_schema.get("properties", {}).items():
            assert schema.get("description"), f"{tool.name}.{name} has no description"
    prompts = {prompt.name for prompt in (await client.list_prompts()).prompts}
    assert prompts == {"instagram_checkup", "post_deep_dive", "audience_portrait", "benchmark_accounts"}


# --------------------------------------------------------------------------- account


async def test_auth_status_when_signed_in(client: Client) -> None:
    status = await call(client, "get_instagram_auth_status")
    assert status["signed_in"] is True
    assert status["handle"] == "@atelier.lune" and status["account_type"] == "Media_Creator"
    assert status["token_expires_in_days"] == 57
    assert status["permissions"] == ["instagram_business_basic", "instagram_business_manage_insights"]
    assert "next_step" not in status


async def test_auth_status_when_signed_out(make_client: Callable[..., Client]) -> None:
    async with make_client(FakeInstagramAPI(signed_in=False)) as client:
        status = await call(client, "get_instagram_auth_status")
        assert status["signed_in"] is False and "instagram-mcp auth" in status["next_step"]
        assert "instagram-mcp auth" in await call_error(client, "get_instagram_profile")


async def test_auth_status_on_a_network_error_is_not_a_sign_out(make_client: Callable[..., Client]) -> None:
    from instagram_mcp.instagram import InstagramAPIError

    class Offline(FakeInstagramAPI):
        def get(self, path: str, params: dict[str, object] | None = None) -> dict[str, object]:
            raise InstagramAPIError("Could not reach Instagram (ConnectError).")

    async with make_client(Offline()) as client:
        status = await call(client, "get_instagram_auth_status")
    assert status["signed_in"] is True and status["handle"] == "@atelier.lune"
    assert "try again" in status["next_step"] and "ConnectError" in status["error"]


async def test_auth_status_for_a_unified_login_token(make_client: Callable[..., Client]) -> None:
    async with make_client(FakeInstagramAPI(graph="facebook")) as client:
        status = await call(client, "get_instagram_auth_status")
    assert status["api_host"] == "graph.facebook.com" and status["token_expiry"] == "never"
    assert status["token_expires_at"] is None and status["account_type"] is None
    assert status["permissions"] == ["instagram_basic", "instagram_manage_insights"]


async def test_profile(client: Client) -> None:
    profile = await call(client, "get_instagram_profile")
    assert (
        profile["handle"] == "@atelier.lune" and profile["url"] == "https://www.instagram.com/atelier.lune/"
    )
    assert (profile["followers"], profile["following"], profile["posts"]) == (8420, 214, 131)
    assert profile["biography"].startswith("Couture") and profile["website"].startswith("https://")


# --------------------------------------------------------------------------- analytics


async def test_performance_summary_compares_with_the_previous_period(client: Client) -> None:
    summary = await call(client, "get_instagram_performance_summary")
    assert summary["date_range"]["start_date"] == "2026-10-14" and summary["date_range"]["days"] == 28
    totals = summary["totals"]
    assert totals["views"] == total("views", "2026-10-14", "2026-11-10")
    assert totals["reach"] == int(total("reach", "2026-10-14", "2026-11-10") * 0.55)  # one request
    assert totals["follows"] == total("follows", "2026-10-14", "2026-11-10")
    assert totals["net_follows"] == totals["follows"] - totals["unfollows"]
    assert summary["previous_date_range"]["end_date"] == "2026-10-13"
    assert summary["change_pct"]["views"] is not None
    assert set(summary["by_content_type"]) == {"reel", "post", "carousel", "story"}
    assert summary["by_follower_status"]["views"]["followers_share_pct"] == 35.0


async def test_reach_is_given_for_ranges_meta_reads_in_one_request(client: Client) -> None:
    summary = await call(client, "get_instagram_performance_summary", period="last_7_days")
    assert summary["totals"]["reach"] == int(total("reach", "2026-11-04", "2026-11-10") * 0.55)
    assert summary["engagement_rate_pct"] > 0
    long = await call(
        client, "get_instagram_performance_summary", period="last_90_days", compare_to_previous=False
    )
    assert long["totals"]["reach"] is None and any("unique accounts" in note for note in long["notes"])
    assert long["totals"]["views"] == total("views", "2026-08-13", "2026-11-10")
    assert "change_pct" not in long


async def test_summary_refuses_dates_older_than_two_years(client: Client) -> None:
    error = await call_error(client, "get_instagram_performance_summary", start_date="2023-01-01")
    assert "two years" in error


async def test_metrics_over_time(client: Client, settings: Settings) -> None:
    trend = await call(
        client,
        "get_instagram_metrics_over_time",
        granularity="week",
        start_date="2026-10-14",
        end_date="2026-11-10",
        include_follower_total=True,
        save_to="trend.csv",
    )
    rows = trend["rows"]
    assert [row["start_date"] for row in rows][:2] == ["2026-10-14", "2026-10-19"] and rows[0]["partial"]
    assert rows[1]["views"] == total("views", "2026-10-19", "2026-10-25")
    assert rows[-1]["followers_end"] == 8420 and "net_follows" not in rows[-1]
    assert rows[-2]["followers_end"] == 8420 - (
        total("follows", "2026-11-09", "2026-11-10") - total("unfollows", "2026-11-09", "2026-11-10")
    )
    saved = Path(trend["saved_to"])
    assert saved.parent == settings.output_dir and len(list(csv.DictReader(saved.open()))) == len(rows)


async def test_metrics_over_time_guards_the_request_count(client: Client) -> None:
    error = await call_error(client, "get_instagram_metrics_over_time", period="all_available")
    assert "coarser granularity" in error
    monthly = await call(
        client, "get_instagram_metrics_over_time", period="last_365_days", granularity="month"
    )
    rows = {row["start_date"][:7]: row for row in monthly["rows"]}
    assert len(rows) == 13 and rows["2026-03"]["reach"] is None  # 31 days: two requests
    assert rows["2026-02"]["reach"] is not None  # 28 days: one request


async def test_audience_age_matches_the_apps_rule(client: Client) -> None:
    result = await call(client, "get_instagram_audience", breakdown="age")
    rows = {row["age"]: row for row in result["rows"]}
    known = {age: f + m for age, (f, m, _) in AGE_GENDER.items() if age != "13-17"}
    assert rows["25-34"]["app_share_pct"] == round(100 * known["25-34"] / sum(known.values()), 1)
    assert rows["13-17"]["app_share_pct"] is None
    assert result["total"] == 8420 and round(sum(r["share_pct"] for r in result["rows"])) == 100


async def test_audience_gender_city_country_and_engaged(client: Client) -> None:
    gender = await call(client, "get_instagram_audience", breakdown="gender")
    female = next(row for row in gender["rows"] if row["code"] == "female")
    assert female["share_of_known_pct"] == 67.9
    cities = await call(client, "get_instagram_audience", breakdown="city", limit=3)
    assert [row["city"] for row in cities["rows"]] == ["Paris", "Lyon", "Nantes"]
    assert cities["rows"][0]["share_pct"] == 8.3  # of all 8,420 followers, like the app
    engaged = await call(
        client, "get_instagram_audience", breakdown="country", audience="engaged", timeframe="this_week"
    )
    assert engaged["timeframe"] == "this_week" and engaged["rows"][0]["country"] == "FR"


async def test_audience_falls_back_when_age_gender_is_refused(make_client: Callable[..., Client]) -> None:
    async with make_client(FakeInstagramAPI(age_by_gender=False)) as client:
        result = await call(client, "get_instagram_audience", breakdown="age")
    assert any("used age alone" in note for note in result["notes"])
    assert all(row["app_share_pct"] is None for row in result["rows"])


async def test_online_followers_are_converted_from_pacific_hours(client: Client) -> None:
    result = await call(client, "get_instagram_online_followers", days=7)
    assert result["timezone"] == "Europe/Paris" and result["days_covered"] == 7
    assert result["best_hours"][0] == 21  # 12:00 in California is 21:00 in Paris
    assert len(result["by_hour"]) == 24 and result["best_slots"][0]["hour"] == 21


async def test_raw_query_hides_paging_urls_and_refuses_token_params(client: Client) -> None:
    page = await call(client, "query_instagram_api", path="me/media", params={"fields": "id", "limit": "2"})
    assert len(page["data"]) == 2 and page["paging"]["has_next_page"] is True
    assert "next" not in page["paging"] and "access_token" not in json.dumps(page)
    assert "can't be set" in await call_error(
        client, "query_instagram_api", path="me", params={"access_token": "x"}
    )
    assert "relative" in await call_error(client, "query_instagram_api", path="https://evil.example/me")
    assert "nonexisting field" in await call_error(
        client, "query_instagram_api", path="me", params={"fields": "nope"}
    )


# --------------------------------------------------------------------------- content


async def test_list_media_with_insights_and_thumbnails(client: Client, fake_api: FakeInstagramAPI) -> None:
    result = await call(
        client, "list_instagram_media", limit=4, include_insights=True, include_thumbnails=True
    )
    media = result["media"]
    assert len(media) == 4 and media[0]["published_at"].startswith("2026-11-11T10:00:00+01:00")
    reel = next(m for m in media if m["type"] == "reel")
    assert reel["avg_watch_time_s"] and reel["skip_rate_pct"] and "follows" not in reel
    carousel = next(m for m in media if m["type"] == "carousel")
    assert carousel["follows"] is not None and carousel["profile_visits"] is not None
    assert all(m["thumbnail"].startswith("data:image/webp;base64,") for m in media)
    assert not any(url.endswith(".mp4") for url in fake_api.downloads)


async def test_list_media_filters_and_sorts(client: Client, settings: Settings) -> None:
    reels = await call(
        client,
        "list_instagram_media",
        media_type="reel",
        sort_by="most_viewed",
        limit=3,
        save_to="reels.json",
    )
    views = [m["views"] for m in reels["media"]]
    assert all(m["type"] == "reel" for m in reels["media"]) and views == sorted(views, reverse=True)
    saved = json.loads(Path(reels["saved_to"]).read_text(encoding="utf-8"))
    assert len(saved) == 3 and "thumbnail" not in saved[0]
    dated = await call(
        client, "list_instagram_media", published_after="2026-11-01", published_before="2026-11-05"
    )
    assert {m["published_at"][:10] for m in dated["media"]} == {"2026-11-01", "2026-11-03", "2026-11-05"}
    photos = await call(
        client, "list_instagram_media", media_type="feed", caption_contains="COLLECTION", limit=100
    )
    assert photos["media"] and all(m["type"] == "image" for m in photos["media"])


async def test_media_insights_for_each_type(client: Client) -> None:
    result = await call(
        client,
        "get_instagram_media_insights",
        media_ids=["17900000000000000", "17900000000000001", "17910000000000001"],
    )
    by_type = {m["type"]: m for m in result["media"]}
    assert set(by_type) == {"reel", "carousel", "story"}
    assert by_type["story"]["navigation"] == {
        "tap_forward": 90,
        "tap_back": 20,
        "tap_exit": 25,
        "swipe_forward": 5,
    }
    assert by_type["carousel"]["engagement_rate_pct"] > 0
    missing = await call(client, "get_instagram_media_insights", media_ids=["123"])
    assert missing["media"] == [] and any("123" in note for note in missing["notes"])


async def test_refused_metrics_are_dropped_and_reported(make_client: Callable[..., Client]) -> None:
    from instagram_mcp import insights

    api = FakeInstagramAPI()
    original = insights.MEDIA_METRICS["reel"]
    insights.MEDIA_METRICS["reel"] = (*original, "follows")  # Meta refuses follows for reels
    try:
        async with make_client(api) as client:
            result = await call(
                client, "list_instagram_media", media_type="reel", limit=3, include_insights=True
            )
    finally:
        insights.MEDIA_METRICS["reel"] = original
    assert all(m["views"] for m in result["media"])
    assert any("doesn't provide follows for reel posts" in note for note in result["notes"])
    retries = [p for p, q in api.calls if p.endswith("/insights") and "follows" in q.get("metric", "")]
    assert len(retries) == 1  # dropped after the first refusal


async def test_rate_limit_stops_further_insight_calls(make_client: Callable[..., Client]) -> None:
    api = FakeInstagramAPI(media_insights_errors={"17900000000000001": 4})
    async with make_client(api) as client:
        result = await call(client, "get_instagram_top_media", period="last_28_days")
    assert any("have no insights" in note for note in result["notes"])


async def test_top_media_ranks_posts_published_in_the_period(client: Client) -> None:
    result = await call(client, "get_instagram_top_media", metric="saves", limit=3, period="last_28_days")
    saves = [m["saves"] for m in result["media"]]
    assert (
        result["posts_in_range"] == 15
        and saves == sorted(saves, reverse=True)
        and result["media"][0]["rank"] == 1
    )


async def test_a_pinned_old_post_does_not_end_the_scan(make_client: Callable[..., Client]) -> None:
    async with make_client(FakeInstagramAPI(pinned_old_post=True)) as client:
        result = await call(client, "get_instagram_top_media", period="last_28_days")
    assert result["posts_in_range"] == 15


async def test_stories(client: Client) -> None:
    result = await call(client, "list_instagram_stories")
    assert result["count"] == 2 and all(s["type"] == "story" for s in result["stories"])
    assert result["stories"][0]["replies"] == 4 and result["stories"][0]["navigation"]["tap_forward"] == 90


async def test_comments(client: Client) -> None:
    one = await call(client, "list_instagram_comments", media_id="17900000000000002", include_replies=True)
    assert one["count"] == 5 and one["comments"][0]["text"].startswith("Commentaire")
    assert any(c["replies"] for c in one["comments"])
    latest = await call(client, "list_instagram_comments", posts=3, limit=7)
    assert latest["count"] == 7 and latest["media_scanned"] == 3


async def test_comments_without_permission_explain_the_fix(make_client: Callable[..., Client]) -> None:
    async with make_client(FakeInstagramAPI(comments_permission=False)) as client:
        error = await call_error(client, "list_instagram_comments")
    assert "instagram_business_manage_comments" in error and "instagram-mcp auth" in error


async def test_tagged_media(client: Client) -> None:
    result = await call(client, "list_instagram_tagged_media")
    assert [m["owner_username"] for m in result["media"]] == ["example.peer.account", "example.fan.account"]


async def test_public_account_needs_a_facebook_host_token(
    client: Client, make_client: Callable[..., Client]
) -> None:
    assert "unified login" in await call_error(
        client, "get_instagram_public_account", username="example.peer.account"
    )
    async with make_client(FakeInstagramAPI(graph="facebook")) as unified:
        account = await call(
            unified, "get_instagram_public_account", username="@example.peer.account", media_limit=4
        )
        assert account["followers"] == 12000 and len(account["recent_media"]) == 4
        assert account["recent_media"][0]["engagement_per_follower_pct"] == round(100 * 310 / 12000, 2)
        assert account["recent_media"][1]["views"] == 5100
        assert "cannot find" in await call_error(unified, "get_instagram_public_account", username="nobody")
        assert "valid" in await call_error(unified, "get_instagram_public_account", username="no spaces")


# --------------------------------------------------------------------------- report and self-check


async def test_report_degrades_section_by_section(make_client: Callable[..., Client]) -> None:
    async with make_client(FakeInstagramAPI(demographics=False, thumbnails=False)) as client:
        report = await call(client, "get_instagram_report", include_thumbnails=True)
    assert set(report["unavailable"]) == {
        "audience.gender",
        "audience.age",
        "audience.cities",
        "audience.countries",
    }
    assert report["audience"]["age"] == [] and len(report["posts"]) == 15
    assert all(post["thumbnail"] is None for post in report["posts"])
    assert any("no thumbnail" in note for note in report["notes"])


async def test_report_saves_json(client: Client, settings: Settings) -> None:
    report = await call(client, "get_instagram_report", period="last_7_days", save_to="week.json")
    saved = json.loads(Path(report["saved_to"]).read_text(encoding="utf-8"))
    assert saved["date_range"]["days"] == 7 and saved["profile"]["followers"] == 8420
    assert "token directory" in await call_error(
        client, "get_instagram_report", save_to=str(settings.config_dir / "r.json")
    )


async def test_selfcheck_calls_every_tool(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert await run_checks(settings, api=FakeInstagramAPI()) == 0
    out = capsys.readouterr().out
    assert "16/16 checks passed, 0 with warnings." in out
    # 9 h apart, except 26-31 Oct (8 h: Europe changed clocks a week before California).
    assert "best hours (Europe/Paris): [21, 20, 22]" in out


async def test_selfcheck_stops_when_signed_out(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run_checks(settings, api=FakeInstagramAPI(signed_in=False)) == 1
    assert "instagram-mcp auth" in capsys.readouterr().out


async def test_selfcheck_with_a_unified_login_token(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run_checks(settings, api=FakeInstagramAPI(graph="facebook")) == 0
    assert "token expiry: never" in capsys.readouterr().out


async def test_a_year_compares_with_a_slightly_shorter_previous_year(client: Client) -> None:
    summary = await call(
        client, "get_instagram_performance_summary", period="last_365_days", include_breakdowns=False
    )
    assert summary["previous_date_range"]["start_date"] == "2024-11-12"
    assert summary["previous_date_range"]["days"] == 364
    assert any("1 day(s) shorter" in note for note in summary["notes"])


async def test_thumbnails_are_capped(client: Client) -> None:
    error = await call_error(client, "list_instagram_media", include_thumbnails=True, limit=100)
    assert "limited to 60" in error
