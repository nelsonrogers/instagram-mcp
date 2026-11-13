# Report format (`schema_version` 1)

`get_instagram_report` (MCP tool) and `instagram-mcp report` (CLI) return one JSON object with
everything about the account for a period. It suits monthly reports and dashboards, and
agents that want one call instead of several.

**Stability promise:** within a `schema_version`, keys are only ever *added*, never renamed or
removed. The tests compare the code's output with
[`examples/instagram-report.sample.json`](examples/instagram-report.sample.json) (a fictional
account), so the sample is always current.

```bash
instagram-mcp report --out instagram-report.json                    # last 28 days
instagram-mcp report --period last_month --thumbnails --out may.json
instagram-mcp report --start-date 2026-01-01 --end-date 2026-03-31 --out - | head
```

## Conventions

- Percentages (`*_pct`) are 0-100 with one decimal (two for engagement rates). Counts are raw
  integers. `*_s` are seconds, `*_h` hours.
- Days are in the report timezone (`INSTAGRAM_MCP_TIMEZONE`, else the computer's), echoed in every
  `date_range`. Timestamps are ISO 8601 with the timezone's offset.
- Anything Instagram can't provide is `null` (numbers), `[]` (lists) or listed under
  `unavailable` with a reason. The call itself fails only when the account can't be read at all
  (no token, rejected token).

## Top-level keys

| Key | Description |
|---|---|
| `schema_version` | `"1"` |
| `generated_at` | When the report was built. |
| `source` | `"Instagram Graph API"` |
| `date_range` | `{start_date, end_date, days, label, timezone}`: the period asked for. |
| `profile` | `id`, `username`, `handle`, `url`, `name`, `biography`, `website`, `followers`, `following`, `posts` and `profile_picture_url` (a temporary CDN link), read at call time. |
| `performance` | The output of `get_instagram_performance_summary` (see below), or `null` if it failed. |
| `audience` | Follower demographics, read at call time: `gender`, `age`, `cities` (top 10), `countries` (top 10). |
| `posts` | Every post and reel published in the period, newest first, with lifetime insights (see below). |
| `best_hours` | `{best_hours, best_slots, timezone}` from `get_instagram_online_followers` (last 28 days). |
| `unavailable` | `{section: reason}` for sections that failed (`performance`, `audience.<key>`, `posts`, `best_hours`). Empty when everything worked. |
| `notes` | Caveats worth passing on (fallbacks, refused metrics, Meta's data lag). |

## `performance`

| Key | Description |
|---|---|
| `date_range`, `previous_date_range` | The period and the one of equal length before it. |
| `totals`, `previous_totals` | Account totals: `views`, `reach`, `accounts_engaged`, `interactions`, `likes`, `comments`, `shares`, `saves`, `replies`, `reposts`, `profile_link_taps`, `follows`, `unfollows`, `net_follows`. `reach` and `accounts_engaged` are unique accounts and are `null` for periods over 29 days (Meta reads at most 30 days per request and unique counts can't be added up). |
| `change_pct` | % change per total, `null` when the previous value is 0 or missing. |
| `engagement_rate_pct` | `interactions / reach`, when reach is known. |
| `by_content_type` | `{reel, post, carousel, story, …: {views, reach, interactions, likes, comments, shares, saves}}` (Meta's `media_product_type` split). |
| `by_follower_status` | `{views, reach: {followers, non_followers, unknown, followers_share_pct}}`. |

## `audience` rows

| Key | Row fields |
|---|---|
| `gender` | `code` (`female`, `male`, `unknown`), `label`, `count`, `share_pct` (of everyone), `share_of_known_pct` (women vs men, as in the app). |
| `age` | `age` (`13-17` … `65+`), `count`, `share_pct` (of everyone), `app_share_pct` (adults of known gender only: the Instagram app's age chart). |
| `cities` | `city` (the app's name, e.g. `Paris`), `name` (Meta's, e.g. `Paris, Île-de-France`), `count`, `share_pct` (of all followers). |
| `countries` | `country` (ISO 3166-1 alpha-2), `count`, `share_pct` (of all followers). |

## `posts[]` items

| Key | Description |
|---|---|
| `id`, `permalink`, `shortcode` | Identifiers and the link on instagram.com. |
| `type` | `reel`, `image`, `video` (feed video), `carousel` or `story`. |
| `published_at` | Publication time in the report timezone. |
| `caption` | Full caption (`""` when there is none). |
| `likes`, `comments` | Public counts (`likes` is missing when the owner hides it). |
| `comments_enabled`, `shared_to_feed` | As Meta reports them. |
| `views`, `reach`, `saves`, `shares`, `reposts`, `interactions` | Lifetime insights. |
| `follows`, `profile_visits`, `profile_activity` | Feed posts and stories only: Meta refuses them for reels. |
| `avg_watch_time_s`, `total_watch_time_h`, `skip_rate_pct` | Reels only. |
| `replies`, `navigation` | Stories only (`navigation` = `{tap_forward, tap_back, tap_exit, swipe_forward}`). |
| `engagement_rate_pct` | `interactions / reach`. |
| `thumbnail` | With `include_thumbnails`: `data:image/webp;base64,…`, 160 px on the long side, under 15 KB (a reel's cover, never the video; a carousel's first image). `null` if the image couldn't be fetched. |
