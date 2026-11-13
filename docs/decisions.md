# Design decisions and API facts

Why the server works the way it does. Meta's documentation is incomplete and sometimes wrong, so
the facts below were **checked against a real Creator account in November 2026** (Graph API
v26.0, unified login token on graph.facebook.com) unless marked otherwise. `instagram-mcp check`
calls every tool against your own account if you want to verify them again.

## What Meta's API actually does (November 2026)

### Account insights (`GET /{ig-user-id}/insights`)

| Fact | Detail |
|---|---|
| Range per request | At most 30 days (2,592,000 s) between `since` and `until`: "There cannot be more than 30 days". 30 calendar days across a daylight-saving change is 30 days + 1 h and is refused. |
| History | Two years: "Metrics data is available for the last 2 years". Meta's docs say 90 days; that is wrong. |
| `total_value` metrics | `views`, `reach`, `accounts_engaged`, `total_interactions`, `likes`, `comments`, `shares`, `saves`, `replies`, `reposts`, `profile_links_taps`, `follows_and_unfollows`. Several can be asked in one request. |
| `time_series` | Only `reach` and `follower_count` (daily, labelled with Meta's US Pacific day). |
| `follows_and_unfollows` | Returns no value without `breakdown=follow_type`. With it, `FOLLOWER` = accounts that started following (it equals the sum of `follower_count`), `NON_FOLLOWER` = accounts that stopped. |
| `follower_count` | Last 30 days only, excluding today. |
| Breakdowns | `media_product_type` (`REEL`, `POST`, `CAROUSEL_CONTAINER`, `STORY`, `AD`) on views, reach, interactions, likes, comments, shares, saves; `follow_type` (`FOLLOWER`, `NON_FOLLOWER`, `UNKNOWN`) on views and reach; `contact_button_type` on profile link taps. Unsupported combinations fail with "An unknown error has occurred" (code 1), not code 100. |
| `online_followers` | `period=lifetime` plus `since`/`until`; one entry per Meta day with 24 hour keys. **The hours are US Pacific time**: for an audience in Europe the minimum falls at hours 18-20 (the European night) and the peak around 12 (European evening). Today's entry is empty. |
| Still served, deprecated | `profile_views` and `website_clicks` still answer on v26 but Meta deprecated them in 2025; the server doesn't use them. `impressions` is refused. |

### Demographics

| Fact | Detail |
|---|---|
| Metrics | `follower_demographics` and `engaged_audience_demographics` (`reached_audience_demographics` is refused). |
| `timeframe` | Required. `this_week` and `this_month` work for both; followers also accept `last_14_days`, `last_30_days`, `last_90_days` and `prev_month`, but engaged refuses them ("no longer supported"). |
| Breakdowns | `age`, `gender`, `city`, `country`, and the undocumented two-way `age,gender`. `age,city` fails with code 1. |
| Answer shape | `dimension_keys` lists only the breakdowns (no `timeframe`); the parser also handles the documented form that starts with `timeframe`. Top 45 values only. |
| The app's age chart | Counts only adults whose gender is known. Over everyone, shares were off by up to 1.4 points; over F + M (`age,gender`) they matched the app within 0.2 points. Hence `app_share_pct`. Gender in the app is women vs men, unknown left out (`share_of_known_pct`). City and country shares in the app are shares of all followers. |

### Posts (`/me/media`, `/{media-id}/insights`)

| Fact | Detail |
|---|---|
| Reel metrics | `views`, `reach`, `likes`, `comments`, `shares`, `saved`, `total_interactions`, `reposts`, `ig_reels_avg_watch_time` (ms), `ig_reels_video_view_total_time` (ms), `reels_skip_rate` (%). Refused: `follows`, `profile_visits`, `profile_activity`, `navigation`, `replies`; `crossposted_views` and `facebook_views` fail unless the reel was shared to Facebook. |
| Feed and story metrics | From Meta's reference (not checked live: the account checked had only reels): feed adds `follows`, `profile_visits`, `profile_activity`; stories have `replies` and `navigation` (breakdown `story_navigation_action_type`). Stories under 5 viewers return error 10. |
| Lifetime only | Media insights are lifetime totals, whatever `period` says. A post from a year ago still answers. |
| Fields | `like_count`, `comments_count`, `shortcode`, `is_comment_enabled`, `is_shared_to_feed` work; `alt_text` and `collaborators` aren't returned for reels; `media_url` is omitted for videos with licensed audio (so thumbnails use `thumbnail_url`). |
| Stories | `/me/stories` lists only the last 24 hours; there is no API access to expired stories (only a webhook). |
| Collaboration posts | A collaboration post owned by another account appears in the app's content list but in neither `/media` nor `/tags`. |

### Other endpoints

| Endpoint | Detail |
|---|---|
| Comments | `/{media-id}/comments` works when the token has `instagram_manage_comments` (unified login apps include it by default). 50 per page, top-level only; replies through `replies{…}`. |
| Tags | `/me/tags` works (posts by others that tag the account). |
| Business Discovery | Works with graph.facebook.com tokens (unified login, Facebook Login), not with Instagram Login. Public fields only. |
| Hashtag search | Refused: it needs Meta's "Instagram Public Content Access" feature, which requires App Review. Not offered by this server. |
| Rate limits | Business Use Case limits, reported in `X-Business-Use-Case-Usage` (a full check run used about 1 %). Throttling is error 80002 (also 4, 17, 32, 613). |

## Design choices

1. **Many focused, read-only tools, like youtube-studio-mcp.** Each answers one kind of
   question; parameters instead of near-duplicate tools; a raw `query_instagram_api` escape hatch
   for anything else; `get_instagram_report` for a one-call snapshot. No tool writes anything to
   Instagram.
2. **Tool names carry `instagram`** so they never collide with another server's (youtube-studio-mcp
   has `get_auth_status` and `get_performance_summary` too).
3. **snake_case outputs, raw API names only in the escape hatch.** Units are in key names
   (`*_pct`, `*_s`, `*_h`); stable codes (`reel`, `followers`) plus readable labels.
4. **Every result echoes its `date_range` with the timezone**, and the server instructions tell the
   agent to quote it.
5. **Long ranges are read in windows of at most 29 days** (always under Meta's limit, even across
   a daylight-saving change) and summed. `reach` and `accounts_engaged` count unique accounts, so
   they are `null` for longer ranges, with a note; summing them would overcount.
6. **The report timezone** is `INSTAGRAM_MCP_TIMEZONE`, else the computer's, else UTC. Day
   boundaries are midnights in that timezone, sent to Meta as Unix times. Meta's own daily
   buckets (Pacific) are only used for online followers, whose hours are converted.
7. **Rolling periods end yesterday**, the latest complete day. Meta's numbers can still move for 48
   hours; custom ranges may end today (a partial day).
8. **Trends are one request per bucket** (all metrics in one call, plus one for follows), capped
   at 400 requests, with a message suggesting a coarser granularity.
9. **Refused metrics are dropped, not fatal.** If Meta refuses a post metric as an invalid
   parameter, it is dropped for that post type for the rest of the call and reported in `notes`;
   after a rate-limit error the remaining posts are skipped. Fallbacks are always reported, never
   silent.
10. **Requests run four at a time** (the HTTP client is thread-safe), so a 90-day summary takes
    about 15 s instead of a minute.
11. **`get_instagram_report` degrades section by section** into `unavailable`, and only fails
    when the account itself can't be read. Its shape is versioned (`schema_version`) and pinned by
    a test against the documented sample.
12. **Thumbnails are embedded** as 160 px WebP data URIs (under 15 KB): CDN links expire within
    days and chat sandboxes often can't download them. Reels and videos use their cover, never the
    video; carousels their first child. Downloads go only to `*.cdninstagram.com` / `*.fbcdn.net`,
    are capped at 10 MB and never carry the token.
13. **`save_to`** writes `.csv` or `.json` in `INSTAGRAM_MCP_OUTPUT_DIR` (default
    `~/Documents/instagram-mcp`) and refuses the token directory.
14. **The escape hatch is GET-only and sanitised**: relative paths only, no `access_token`,
    `method` or `appsecret_proof` parameters, and Meta's `paging.next` URLs (which embed the token)
    are replaced by cursors.

## Auth and tokens

15. **Both kinds of token work, and the token picks the API.** `IG…` tokens (Instagram Login) go
    to graph.instagram.com; `EAA…` tokens (unified login, Facebook Login) to graph.facebook.com.
    New Meta apps get unified login since late 2026.
16. **`me` becomes the account ID on graph.facebook.com**, where `/me` is the system user. The
    client does the substitution, so tools and the fake keep `me/...` paths.
17. **`auth` finds the account** with `/me/assigned_instagram_accounts`, then a Facebook Page's
    `instagram_business_account`; `--account` names it when Meta lists none or several.
18. **The token's permissions come from the login configuration**, not from *Permissions and
    features*: a token generated with Meta's default configuration lacks
    `instagram_manage_insights`. `auth` lists the permissions Meta granted when insights fail.
19. **Token lifetime.** Instagram Login tokens last 60 days and are refreshed automatically once
    they are a day old and have less than 50 days left (after a failed refresh, the next try waits
    an hour). Unified login tokens can't be refreshed through the API; `/debug_token` is refused
    to them, so their expiry is recorded as unknown unless the configuration says "never".
20. **No sign-in tool.** Signing in means handling a secret token, which must not travel through a
    chat, so it only happens in a terminal (`instagram-mcp auth`).
21. **Secrets stay out of logs and messages.** Tokens are redacted from Meta's error messages;
    a logging filter on httpx masks `access_token` (and other secret parameters) in every request
    line, whatever the host program's logging setup, and the CLI also keeps httpx at WARNING; and
    pagination uses cursors rather than Meta's `next` URLs.
22. **`--oauth`** (Instagram Login apps only) uses a paste-back redirect (`https://localhost/`) and
    reads the app secret from the environment or a hidden prompt, never storing it.

## Open questions

- **Feed and story metrics** come from Meta's reference: the account checked had only reels. If Meta refuses one, the server drops it and says so in `notes`.
- **Instagram Login hosts**: stories, tags and comments are documented for Facebook Login and
  unified login; on graph.instagram.com they are expected to work with the
  `instagram_business_*` permissions, but weren't checked live.
- **Graph API version**: v26.0 by default; Meta's pages already mention v27.0. Override with
  `INSTAGRAM_MCP_API_VERSION`.
