"""A deterministic, rule-checking fake of the Instagram Graph API endpoints the server uses.

It serves a fictional creator account (@atelier.lune) and rejects the request shapes the real
API rejects, as observed on a real account in November 2026 (Graph API v26.0): deprecated
metrics, ranges over 30 days or older than 2 years, metrics a media type doesn't support,
breakdowns a metric doesn't take, time_series on total-value-only metrics, and so on. The
tests therefore catch invalid queries instead of only checking our own parsing.
"""

from __future__ import annotations

import io
import math
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from instagram_mcp.auth import (
    FACEBOOK,
    FACEBOOK_SCOPES,
    INSTAGRAM,
    SCOPES,
    NotAuthenticatedError,
    StoredToken,
    utcnow,
)
from instagram_mcp.instagram import InstagramAPIError, is_cdn_url

TODAY = date(2026, 11, 11)
NOW = datetime(2026, 11, 11, 10, 30, tzinfo=ZoneInfo("Europe/Paris"))
PACIFIC = ZoneInfo("America/Los_Angeles")
MAX_RANGE_SECONDS = 2_592_000

PROFILE = {
    "id": "26012345678901234",  # app-scoped ID (graph.instagram.com)
    "user_id": "17841400000000001",  # Instagram professional account ID
    "username": "atelier.lune",
    "name": "Atelier Lune · couture & carnets",
    "biography": "Couture, carnets et tutos du dimanche ✂️",
    "website": "https://atelier-lune.example",
    "profile_picture_url": "https://scontent-cdg4-1.cdninstagram.com/v/fake/profile.jpg",
    "account_type": "Media_Creator",  # Meta documents Business | Media_Creator
    "followers_count": 8420,
    "follows_count": 214,
    "media_count": 131,
}
PROFILE_FIELDS = {*PROFILE, "ig_id"}
# graph.facebook.com's IG User has no user_id or account_type (its `id` is the account ID).
FACEBOOK_PROFILE_FIELDS = PROFILE_FIELDS - {"user_id", "account_type"}

# A public account for Business Discovery.
PUBLIC_ACCOUNTS = {
    "example.peer.account": {
        "username": "example.peer.account",
        "name": "Example Peer Account",
        "biography": "A fictional account for tests",
        "website": "https://peer.example",
        "followers_count": 12000,
        "follows_count": 300,
        "media_count": 410,
        "profile_picture_url": "https://scontent-cdg4-1.cdninstagram.com/v/fake/peer.jpg",
    }
}

# Followers per breakdown value, shaped like the real follower_demographics answers.
DEMOGRAPHICS: dict[str, list[tuple[str, int]]] = {
    "gender": [("F", 5512), ("M", 2601), ("U", 307)],
    "age": [
        ("13-17", 41),
        ("18-24", 903),
        ("25-34", 3285),
        ("35-44", 2104),
        ("45-54", 1080),
        ("55-64", 612),
        ("65+", 395),
    ],
    "city": [
        ("Paris, Île-de-France", 702),
        ("Lyon, Auvergne-Rhône-Alpes", 233),
        ("Nantes, Pays de la Loire", 151),
        ("Bordeaux, Nouvelle-Aquitaine", 140),
        ("Montréal, Quebec", 97),
        ("Lille, Hauts-de-France", 95),
        ("Brussels, Brussels Region", 88),
    ],
    "country": [
        ("FR", 6870),
        ("BE", 402),
        ("CA", 318),
        ("CH", 221),
        ("MA", 96),
        ("LU", 54),
        ("US", 41),
    ],
}
# The same followers by age and gender (each age row adds up to DEMOGRAPHICS["age"], each gender
# column to DEMOGRAPHICS["gender"]). Unknown gender skews older, as seen on real accounts, so ages
# over F+M followers (what the app shows) differ from ages over everyone.
AGE_GENDER: dict[str, tuple[int, int, int]] = {  # age: (F, M, U)
    "13-17": (24, 12, 5),
    "18-24": (605, 280, 18),
    "25-34": (2223, 1010, 52),
    "35-44": (1393, 650, 61),
    "45-54": (676, 340, 64),
    "55-64": (365, 190, 57),
    "65+": (226, 119, 50),
}
ENGAGED_SHARE = 0.2  # engaged-audience counts are this share of the follower counts

# Followers online per hour of Meta's (US Pacific) day: a synthetic curve that peaks at 12:00 PT
# (21:00 in Paris in winter) and is lowest around 19:00-20:00 PT (the European night).
ONLINE_BY_PACIFIC_HOUR = [
    round(1000 + 900 * math.cos(math.pi * (hour - 12) / 8)) - 5 * hour
    if abs(hour - 12) <= 8
    else 100 + 10 * hour
    for hour in range(24)
]

TOTAL_VALUE_METRICS = {
    "views",
    "reach",
    "accounts_engaged",
    "total_interactions",
    "likes",
    "comments",
    "shares",
    "saves",
    "replies",
    "reposts",
    "follows_and_unfollows",
    "profile_links_taps",
}
TIME_SERIES_METRICS = {"reach", "follower_count"}
DEMOGRAPHIC_METRICS = {"follower_demographics", "engaged_audience_demographics"}
DEPRECATED_METRICS = {
    "impressions",
    "plays",
    "video_views",
    "clips_replays_count",
    "ig_reels_aggregated_all_plays_count",
    "email_contacts",
    "phone_call_clicks",
    "text_message_clicks",
    "get_direction_clicks",
}
# Breakdowns each metric accepts on account insights (others: "An unknown error has occurred.").
ACCOUNT_BREAKDOWNS = {
    "views": {"media_product_type", "follow_type"},
    "reach": {"media_product_type", "follow_type"},
    "total_interactions": {"media_product_type"},
    "likes": {"media_product_type"},
    "comments": {"media_product_type"},
    "shares": {"media_product_type"},
    "saves": {"media_product_type"},
    "follows_and_unfollows": {"follow_type"},
    "profile_links_taps": {"contact_button_type"},
}
SPLITS = {
    "media_product_type": {"REEL": 80, "POST": 6, "CAROUSEL_CONTAINER": 4, "STORY": 10},
    "follow_type": {"FOLLOWER": 35, "NON_FOLLOWER": 60, "UNKNOWN": 5},
    "contact_button_type": {},
}

MEDIA_METRICS = {
    "REELS": {
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
    },
    "FEED": {
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
    },
    "STORY": {
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
    },
}
MEDIA_FIELDS = {
    "id",
    "caption",
    "media_type",
    "media_product_type",
    "timestamp",
    "permalink",
    "thumbnail_url",
    "media_url",
    "like_count",
    "comments_count",
    "shortcode",
    "username",
    "is_comment_enabled",
    "is_shared_to_feed",
}
COMMENT_FIELDS = {"id", "text", "timestamp", "like_count", "username"}


def _error(message: str, code: int = 100) -> InstagramAPIError:
    return InstagramAPIError(f"(#{code}) {message}", code=code)


CAPTIONS = {
    "REELS": "un tuto couture en 60 secondes ✂️ Enregistre-le pour plus tard, et dis-moi en commentaire "
    "ce que tu veux voir la semaine prochaine !",
    "CAROUSEL_ALBUM": "Mes carnets du mois, page par page 📒 Lequel est ton préféré ?",
    "IMAGE": "Nouvelle collection en vue… #couture #faitmain",
}


def _media(today: date) -> list[dict[str, Any]]:
    """Posts, newest first: one every 2 days (the newest published today), cycling reel /
    carousel / reel / photo, plus a second post on one day."""
    items: list[dict[str, Any]] = []
    noon = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=9)
    schedule = [(index, noon - timedelta(days=index * 2)) for index in range(40)]
    schedule.insert(2, (40, noon - timedelta(days=2, hours=-8)))  # same day as post 1, later
    for index, published in schedule:
        kind = ["REELS", "CAROUSEL_ALBUM", "REELS", "IMAGE"][index % 4]
        media_id = f"1790000000000{index:04d}"
        item: dict[str, Any] = {
            "id": media_id,
            "caption": f"Post {index + 1} : {CAPTIONS[kind]}",
            "media_type": "VIDEO" if kind == "REELS" else kind,
            "media_product_type": "REELS" if kind == "REELS" else "FEED",
            "timestamp": published.strftime("%Y-%m-%dT%H:%M:%S+0000"),
            "permalink": f"https://www.instagram.com/{'reel' if kind == 'REELS' else 'p'}/FAKE{index:04d}/",
            "shortcode": f"FAKE{index:04d}",
            "username": PROFILE["username"],
            "like_count": 90 + (index * 37) % 400,
            "comments_count": 3 + index % 9,
            "is_comment_enabled": True,
        }
        if kind == "REELS":
            item["is_shared_to_feed"] = True
            item["thumbnail_url"] = f"https://scontent-cdg4-1.cdninstagram.com/v/fake/{media_id}_thumb.jpg"
            item["media_url"] = f"https://scontent-cdg4-1.cdninstagram.com/v/fake/{media_id}.mp4"
        elif kind == "IMAGE":
            item["media_url"] = f"https://scontent-cdg4-1.cdninstagram.com/v/fake/{media_id}.jpg"
        elif index % 8 == 1:
            # Some albums carry their first child's *video* as media_url: never download it.
            item["media_url"] = f"https://scontent-cdg4-1.cdninstagram.com/v/fake/{media_id}_1.mp4"
        items.append(item)
    return items


def _stories(today: date) -> list[dict[str, Any]]:
    morning = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=7)
    return [
        {
            "id": f"1791000000000000{n}",
            "caption": "",
            "media_type": "IMAGE" if n == 1 else "VIDEO",
            "media_product_type": "STORY",
            "timestamp": (morning - timedelta(hours=3 * n)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
            "permalink": f"https://www.instagram.com/stories/atelier.lune/1791000000000000{n}/",
            "media_url": f"https://scontent-cdg4-1.cdninstagram.com/v/fake/story{n}.jpg",
            "thumbnail_url": f"https://scontent-cdg4-1.cdninstagram.com/v/fake/story{n}_thumb.jpg",
        }
        for n in (1, 2)
    ]


TAGGED = [
    {
        "id": "17920000000000001",
        "username": "example.peer.account",
        "caption": "Merci @atelier.lune pour le patron !",
        "media_type": "IMAGE",
        "media_product_type": "FEED",
        "timestamp": "2026-11-02T15:00:00+0000",
        "permalink": "https://www.instagram.com/p/TAG0001/",
        "like_count": 210,
        "comments_count": 12,
    },
    {
        "id": "17920000000000002",
        "username": "example.fan.account",
        "caption": "Ma version du tuto d'@atelier.lune",
        "media_type": "VIDEO",
        "media_product_type": "REELS",
        "timestamp": "2026-10-20T18:30:00+0000",
        "permalink": "https://www.instagram.com/reel/TAG0002/",
        "like_count": 88,
        "comments_count": 4,
    },
]


def fake_image(url: str) -> bytes:
    """A deterministic portrait JPEG (a two-colour gradient) standing in for a CDN image."""
    from PIL import Image

    seed = sum(url.encode()) % 200
    width, height = (540, 960) if "_thumb" in url else (540, 675)
    ramp = Image.linear_gradient("L").resize((width, height))
    red = ramp.point(lambda v: (v * 3 // 4 + seed) % 256)
    green = Image.new("L", (width, height), 90 + seed % 100)
    blue = ramp.transpose(Image.Transpose.ROTATE_180).point(lambda v: v // 2 + 60)
    buffer = io.BytesIO()
    Image.merge("RGB", (red, green, blue)).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def daily_values(day: date) -> dict[str, int]:
    """One day of account activity (deterministic per date)."""
    n = day.toordinal()
    likes, comments, shares, saves = 120 + n % 4 * 10, 8 + n % 3, 15 + n % 5, 25 + n % 6
    replies, reposts = 2 + n % 2, 3 + n % 3
    return {
        "views": 6000 + n % 7 * 150,
        "reach": 3000 + n % 5 * 100,
        "accounts_engaged": 80 + n % 3 * 10,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "saves": saves,
        "replies": replies,
        "reposts": reposts,
        "total_interactions": likes + comments + shares + saves + replies + reposts,
        "profile_links_taps": n % 3,
        "follows": 6 + n % 4,
        "unfollows": 2 + n % 3,
    }


def _days(since: int, until: int) -> list[date]:
    """The (UTC noon-anchored) dates covered by [since, until)."""
    count = max(1, round((until - since) / 86_400))
    return [datetime.fromtimestamp(since + 43_200 + i * 86_400, tz=UTC).date() for i in range(count)]


def _split(total: int, shares: dict[str, int]) -> dict[str, int]:
    parts = {key: total * share // 100 for key, share in shares.items()}
    first = next(iter(parts), None)
    if first is not None:
        parts[first] += total - sum(parts.values())
    return parts


class FakeInstagramAPI:
    def __init__(
        self,
        *,
        today: date = TODAY,
        signed_in: bool = True,
        demographics: bool = True,
        age_by_gender: bool = True,
        media_insights: bool = True,
        thumbnails: bool = True,
        media_insights_errors: dict[str, int] | None = None,
        comments_permission: bool = True,
        pinned_old_post: bool = False,
        graph: str = INSTAGRAM,
    ) -> None:
        self.today = today
        self.graph = graph  # which API the fake's token belongs to (Instagram Login or unified login)
        self.signed_in = signed_in
        self.demographics = demographics
        self.age_by_gender = age_by_gender  # False: the two-way age,gender breakdown is refused
        self.media_insights = media_insights
        self.thumbnails = thumbnails
        self.comments_permission = comments_permission
        self.downloads: list[str] = []
        # One-shot errors for given media IDs (e.g. {id: 2} = a transient error, {id: 4} = rate limit).
        self.media_insights_errors = dict(media_insights_errors or {})
        self.media = _media(today)
        if pinned_old_post:  # a pinned post from last year listed first
            old = dict(self.media[-1], id="17900000000009999", timestamp="2025-06-01T17:00:00+0000")
            self.media.insert(0, old)
        self.stories = _stories(today)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # -- InstagramAPI protocol --------------------------------------------------------------

    def token_info(self) -> StoredToken:
        self._require_sign_in()
        now = utcnow()  # get_instagram_auth_status uses the real clock
        if self.graph == FACEBOOK:  # a unified-login system-user token set to never expire
            return StoredToken(
                access_token="EAAfake",
                obtained_at=now - timedelta(days=3),
                expires_at=None,
                username=PROFILE["username"],
                user_id=PROFILE["user_id"],
                permissions=list(FACEBOOK_SCOPES),
                graph=FACEBOOK,
            )
        return StoredToken(
            access_token="IGAAfake",
            obtained_at=now - timedelta(days=3),
            expires_at=now + timedelta(days=57, hours=1),
            username=PROFILE["username"],
            user_id=PROFILE["user_id"],
            permissions=list(SCOPES),
        )

    def last_refresh_error(self) -> str | None:
        return None

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_sign_in()
        params = dict(params or {})
        self.calls.append((path, params))
        if "access_token" in params:
            raise AssertionError("callers must not pass the token themselves")
        head, _, edge = path.partition("/")
        if head == "me":
            handlers = {
                "": self._me,
                "insights": self._user_insights,
                "media": self._media_list,
                "stories": self._stories_list,
                "tags": self._tags,
            }
            if edge in handlers:
                return handlers[edge](params)
        item = self._find(head)
        if item is not None:
            if edge == "":
                return self._select(item, params, MEDIA_FIELDS)
            if edge == "insights":
                return self._media_insights(item, params)
            if edge == "children":
                return self._children(item, params)
            if edge == "comments":
                return self._comments(item, params)
        raise _error(f"Unsupported get request. Object with ID '{head}' does not exist.")

    def download(self, url: str) -> bytes:
        self._require_sign_in()
        if not is_cdn_url(url):
            raise AssertionError(f"not a CDN URL: {url}")
        if url.endswith(".mp4"):
            raise AssertionError("tried to download a video instead of its thumbnail")
        self.downloads.append(url)
        if not self.thumbnails:
            raise InstagramAPIError("The image download failed (HTTP 403).")
        return fake_image(url)

    # -- helpers ------------------------------------------------------------------------------

    def _require_sign_in(self) -> None:
        if not self.signed_in:
            raise NotAuthenticatedError(
                "Not signed in to Instagram (profile 'default'). Run `instagram-mcp auth`."
            )

    def _find(self, media_id: str) -> dict[str, Any] | None:
        return next((m for m in [*self.media, *self.stories] if m["id"] == media_id), None)

    @staticmethod
    def _field_list(params: dict[str, Any]) -> list[str]:
        """Top-level fields, keeping nested ``edge{...}`` expansions whole."""
        raw = str(params.get("fields") or "")
        fields, depth, current = [], 0, ""
        for char in raw:
            depth += char in "{("
            depth -= char in "})"
            if char == "," and depth == 0:
                fields.append(current)
                current = ""
            else:
                current += char
        if current:
            fields.append(current)
        return fields

    def _select(self, item: dict[str, Any], params: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
        fields = self._field_list(params) or ["id"]
        unknown = [f for f in fields if f not in allowed]
        if unknown:
            raise _error(f"Tried accessing nonexisting field ({unknown[0]})")
        return {k: item[k] for k in fields if k in item} | {"id": item["id"]}

    def _check_range(self, params: dict[str, Any]) -> tuple[int, int]:
        try:
            since, until = int(params["since"]), int(params["until"])
        except (KeyError, ValueError) as exc:
            raise _error("since and until must be Unix timestamps") from exc
        if until - since > MAX_RANGE_SECONDS:
            raise _error("There cannot be more than 30 days (2592000 s) between since and until.")
        two_years = datetime.combine(
            self.today.replace(year=self.today.year - 2), datetime.min.time(), tzinfo=UTC
        )
        if since < two_years.timestamp():
            raise _error("since param is not valid. Metrics data is available for the last 2 years")
        if until <= since:
            raise _error("until must be after since")
        return since, until

    # -- account ------------------------------------------------------------------------------

    def _me(self, params: dict[str, Any]) -> dict[str, Any]:
        fields = self._field_list(params)
        discovery = [f for f in fields if f.startswith("business_discovery")]
        if discovery:
            if self.graph != FACEBOOK:
                raise _error("Tried accessing nonexisting field (business_discovery)")
            return {"business_discovery": self._business_discovery(discovery[0]), "id": PROFILE["user_id"]}
        if self.graph == FACEBOOK:
            profile = PROFILE | {"id": PROFILE["user_id"]}
            return self._select(profile, params, FACEBOOK_PROFILE_FIELDS)
        return self._select(PROFILE, params, PROFILE_FIELDS)

    def _business_discovery(self, expansion: str) -> dict[str, Any]:
        match = re.fullmatch(r"business_discovery\.username\(([^)]+)\)\{(.*)\}", expansion)
        if not match:
            raise _error("Syntax error in business_discovery")
        username, inner = match.groups()
        account = PUBLIC_ACCOUNTS.get(username)
        if account is None:
            raise _error(f"Invalid user id: cannot find user {username}", code=110)
        out: dict[str, Any] = {"id": "17841400000000777"}
        for field in self._field_list({"fields": inner}):
            media = re.fullmatch(r"media\.limit\((\d+)\)\{(.*)\}", field)
            if media:
                limit = int(media.group(1))
                wanted = media.group(2).split(",")
                posts = [
                    {
                        "id": f"1793000000000{n:04d}",
                        "caption": f"Public post {n}",
                        "media_type": "VIDEO" if n % 2 else "IMAGE",
                        "media_product_type": "REELS" if n % 2 else "FEED",
                        "timestamp": f"2026-11-{10 - n % 9:02d}T12:00:00+0000",
                        "permalink": f"https://www.instagram.com/p/PUB{n:04d}/",
                        "like_count": 300 + n * 10,
                        "comments_count": 10 + n,
                        "view_count": 5000 + n * 100 if n % 2 else None,
                    }
                    for n in range(limit)
                ]
                out["media"] = {"data": [{k: p[k] for k in wanted if p.get(k) is not None} for p in posts]}
            elif field in account:
                out[field] = account[field]
        return out

    def _user_insights(self, params: dict[str, Any]) -> dict[str, Any]:
        metrics = [m for m in str(params.get("metric") or "").split(",") if m]
        if not metrics:
            raise _error("The parameter metric is required")
        for metric in metrics:
            if metric in DEPRECATED_METRICS:
                raise _error(
                    "metric[0] must be one of the following values: "
                    + ", ".join(sorted(TOTAL_VALUE_METRICS | TIME_SERIES_METRICS))
                )
        if set(metrics) & DEMOGRAPHIC_METRICS:
            if len(metrics) != 1:
                raise _error("Demographic metrics must be requested alone")
            return self._demographics(metrics[0], params)
        if metrics == ["online_followers"]:
            return self._online_followers(params)
        if params.get("period") != "day":
            raise _error(f"The following periods ({params.get('period')}) are incompatible with the metric")
        metric_type = params.get("metric_type")
        for metric in metrics:
            if metric_type == "total_value" and metric not in TOTAL_VALUE_METRICS:
                raise _error(
                    f"The following metric ({metric}) is incompatible with the metric type (total_value)"
                )
            if metric_type != "total_value" and metric not in TIME_SERIES_METRICS:
                raise _error(
                    f"The following metric ({metric}) is incompatible with the metric type (time_series)"
                )
        since, until = self._check_range(params)
        days = _days(since, until)
        if metric_type != "total_value":
            raise AssertionError("the server should not need time_series")
        breakdown = params.get("breakdown")
        if breakdown:
            for metric in metrics:
                if breakdown not in ACCOUNT_BREAKDOWNS.get(metric, set()):
                    raise _error("An unknown error has occurred.", code=1)
        data = []
        for metric in metrics:
            entry: dict[str, Any] = {
                "name": metric,
                "period": "day",
                "title": metric,
                "id": f"x/insights/{metric}/day",
            }
            if metric == "follows_and_unfollows":
                if breakdown:
                    follows = sum(daily_values(d)["follows"] for d in days)
                    unfollows = sum(daily_values(d)["unfollows"] for d in days)
                    results = [
                        {"dimension_values": ["FOLLOWER"], "value": follows},
                        {"dimension_values": ["NON_FOLLOWER"], "value": unfollows},
                    ]
                    entry["total_value"] = {
                        "breakdowns": [{"dimension_keys": ["follow_type"], "results": results}]
                    }
                data.append(entry)  # without a breakdown Meta returns no value at all
                continue
            total = sum(daily_values(d)[metric] for d in days)
            if metric in {"reach", "accounts_engaged"}:
                total = int(total * 0.55)  # unique accounts: less than the sum of the days
            if breakdown:
                parts = _split(total, SPLITS[breakdown])
                if metric != "views" and breakdown == "media_product_type":
                    parts = _split(total, {"REEL": 85, "POST": 15})
                results = [{"dimension_values": [k], "value": v} for k, v in parts.items() if v]
                entry["total_value"] = {"breakdowns": [{"dimension_keys": [breakdown], "results": results}]}
            else:
                entry["total_value"] = {"value": total}
            data.append(entry)
        return {"data": data}

    def _online_followers(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("period") != "lifetime" or params.get("metric_type"):
            raise _error("online_followers needs period=lifetime and no metric_type")
        since, until = self._check_range(params)
        values = []
        for day in _days(since, until):
            end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=PACIFIC)
            hours = {} if day >= self.today else {str(h): v for h, v in enumerate(ONLINE_BY_PACIFIC_HOUR)}
            values.append(
                {"value": hours, "end_time": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")}
            )
        return {"data": [{"name": "online_followers", "period": "lifetime", "values": values}]}

    def _demographics(self, metric: str, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("period") != "lifetime":
            raise _error(
                f"The following periods ({params.get('period')}) are incompatible with the metric ({metric})"
            )
        timeframe = params.get("timeframe")
        allowed = {"this_week", "this_month"}
        if metric == "follower_demographics":
            allowed |= {"last_14_days", "last_30_days", "last_90_days", "prev_month"}
        if timeframe not in allowed:
            raise _error(f"The timeframe parameter specified {timeframe} is not supported for {metric}")
        breakdown = str(params.get("breakdown") or "")
        scale = 1.0 if metric == "follower_demographics" else ENGAGED_SHARE
        if breakdown == "age,gender":
            if not self.age_by_gender:
                raise _error("An unknown error has occurred.", code=1)
            results = [
                {"dimension_values": [age, gender], "value": int(count * scale)}
                for age, counts in AGE_GENDER.items()
                for gender, count in zip(("F", "M", "U"), counts, strict=True)
            ]
        elif breakdown in DEMOGRAPHICS:
            results = [
                {"dimension_values": [value], "value": int(count * scale)}
                for value, count in DEMOGRAPHICS[breakdown]
            ]
        else:
            raise _error("An unknown error has occurred.", code=1)
        blocks = [{"dimension_keys": breakdown.split(","), "results": results}] if self.demographics else []
        return {
            "data": [
                {
                    "name": metric,
                    "period": "lifetime",
                    "title": metric,
                    "total_value": {"breakdowns": blocks},
                    "id": f"{PROFILE['user_id']}/insights/{metric}/lifetime",
                }
            ]
        }

    # -- media --------------------------------------------------------------------------------

    def _page(self, items: list[dict[str, Any]], params: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
        limit = int(params.get("limit") or 25)
        if limit > 100:
            raise _error("limit must be at most 100")
        start = int(params.get("after") or 0)
        page = items[start : start + limit]
        data = [self._select(item, params, allowed) for item in page]
        result: dict[str, Any] = {
            "data": data,
            "paging": {"cursors": {"before": str(start), "after": str(start + len(page))}},
        }
        if start + limit < len(items):
            result["paging"]["next"] = (
                "https://graph.instagram.com/v26.0/me/media?access_token=REDACTED&after=..."
            )
        return result

    def _media_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._page(self.media, params, MEDIA_FIELDS)

    def _stories_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._page(self.stories, params, MEDIA_FIELDS)

    def _tags(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._page(TAGGED, params, MEDIA_FIELDS)

    def _children(self, item: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        media_id = item["id"]
        if item["media_type"] != "CAROUSEL_ALBUM":
            raise _error(f"Object with ID '{media_id}' has no children")
        self._select({"id": ""}, params, {"id", "media_type", "media_url", "thumbnail_url", "timestamp"})
        children = [
            {
                "id": f"{media_id}{n}",
                "media_type": "IMAGE",
                "media_url": f"https://scontent-cdg4-1.cdninstagram.com/v/fake/{media_id}_{n}.jpg",
            }
            for n in (1, 2, 3)
        ]
        if item.get("media_url", "").endswith(".mp4"):  # this album starts with a video
            children[0] = {
                "id": f"{media_id}1",
                "media_type": "VIDEO",
                "media_url": item["media_url"],
                "thumbnail_url": f"https://scontent-cdg4-1.cdninstagram.com/v/fake/{media_id}_1_thumb.jpg",
            }
        return {"data": children}

    def _media_insights(self, item: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        media_id = item["id"]
        product = item["media_product_type"]
        metrics = [m for m in str(params.get("metric") or "").split(",") if m]
        for metric in metrics:
            if metric in DEPRECATED_METRICS or metric not in MEDIA_METRICS[product]:
                raise _error(
                    f"The Media Insights API does not support the {metric} metric for this media product type."
                )
        if not self.media_insights:
            raise _error("An unknown error has occurred.", code=1)
        if media_id in self.media_insights_errors:
            code = self.media_insights_errors.pop(media_id)
            raise _error("Application request limit reached" if code == 4 else "Please retry", code=code)
        index = int(media_id[-4:]) % 1000
        base = 40_000 if product == "REELS" else 6_000 if product == "FEED" else 900
        views = base // (1 + index % 7) + 37 * index
        values = {
            "views": views,
            "reach": int(views * 0.7),
            "likes": views // 40,
            "comments": views // 900,
            "shares": views // 300,
            "saved": views // 200,
            "reposts": views // 2000,
            "replies": 4,
            "follows": 3 + index % 5,
            "profile_visits": 20 + index % 11,
            "profile_activity": 9 + index % 4,
            "ig_reels_avg_watch_time": 12_000 + 500 * (index % 9),
            "ig_reels_video_view_total_time": views * 9_000,
            "reels_skip_rate": 30 + index % 25,
            "navigation": 140,
        }
        values["total_interactions"] = (
            values["likes"] + values["comments"] + values["shares"] + values["saved"]
        )
        breakdown = params.get("breakdown")
        if breakdown:
            if metrics != ["navigation"] or breakdown != "story_navigation_action_type":
                raise _error(f"Incompatible breakdowns ({breakdown}) for metric ({metrics[0]})")
            results = [
                {"dimension_values": [k], "value": v}
                for k, v in {"TAP_FORWARD": 90, "TAP_BACK": 20, "TAP_EXIT": 25, "SWIPE_FORWARD": 5}.items()
            ]
            return {
                "data": [
                    {
                        "name": "navigation",
                        "period": "lifetime",
                        "total_value": {
                            "breakdowns": [
                                {"dimension_keys": ["story_navigation_action_type"], "results": results}
                            ]
                        },
                    }
                ]
            }
        return {
            "data": [
                {
                    "name": metric,
                    "period": "lifetime",
                    "values": [{"value": values[metric]}],
                    "id": f"{media_id}/insights/{metric}/lifetime",
                }
                for metric in metrics
            ]
        }

    def _comments(self, item: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        if not self.comments_permission:
            raise _error("Application does not have permission for this action", code=10)
        fields = self._field_list(params)
        replies_field = next((f for f in fields if f.startswith("replies")), None)
        plain = [f for f in fields if f != replies_field]
        unknown = [f for f in plain if f not in COMMENT_FIELDS]
        if unknown:
            raise _error(f"Tried accessing nonexisting field ({unknown[0]})")
        count = int(item.get("comments_count") or 0)
        published = datetime.strptime(item["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
        comments = []
        for n in range(count):
            comment = {
                "id": f"{item['id']}c{n}",
                "text": f"Commentaire {n + 1} : trop beau !",
                "timestamp": (published + timedelta(hours=n + 1)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
                "like_count": n % 4,
                "username": f"lectrice_{n}",
            }
            selected = {k: comment[k] for k in plain if k in comment} | {"id": comment["id"]}
            if replies_field:
                selected["replies"] = {
                    "data": [
                        {
                            "id": f"{comment['id']}r",
                            "text": "Merci !",
                            "timestamp": comment["timestamp"],
                            "like_count": 1,
                            "username": PROFILE["username"],
                        }
                    ]
                    if n % 3 == 0
                    else []
                }
            comments.append(selected)
        params = {**params, "fields": "id"}
        page = self._page(comments, params, {"id"})
        start = int(params.get("after") or 0)
        page["data"] = comments[start : start + int(params.get("limit") or 25)]
        return page
