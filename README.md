# Instagram MCP

[![CI](https://github.com/nelsonrogers/instagram-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/nelsonrogers/instagram-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

**Give your AI assistant your Instagram Insights.** A local, read-only
[Model Context Protocol](https://modelcontextprotocol.io) server for creators: your agent
(Claude, ChatGPT desktop, Cursor, VS Code, …) can read your account's performance, trends,
audience, posts, reels, stories and comments, straight from Meta's official Instagram API, and
analyse them however you like.

> "How did my Instagram do last month compared to the month before, and why?"
> "Which reels kept people watching longest this quarter, and what do they have in common?"
> "Who follows me vs who actually engages: age, gender, cities?"
> "When are my followers online? When should I post?"
> "Summarise the questions people ask in my comments."
> "Build a report of the last 28 days with charts."

It runs on your computer with your own Meta app and token. Your data goes from Meta to your
machine to your AI app, and nowhere else. It never posts, comments or changes anything. It is the
Instagram companion of [youtube-studio-mcp](https://github.com/nelsonrogers/youtube-studio-mcp).

---

## What it can do

| Area | Tools |
|---|---|
| **Overview** | `get_instagram_performance_summary`: views, reach, accounts engaged, interactions (likes, comments, shares, saves, replies, reposts), profile link taps, follows, unfollows and net follows for any period up to 2 years, vs the previous period, split by content type (reels, posts, carousels, stories) and followers vs non-followers |
| **Trends** | `get_instagram_metrics_over_time`: any of those metrics by day, week or month, with an estimated follower count per bucket for growth charts |
| **Audience** | `get_instagram_audience`: age, gender, age × gender, top cities and countries of your followers, or of the accounts that engaged with you; `get_instagram_online_followers`: when your followers are online, by hour and weekday |
| **Content** | `list_instagram_media` (your whole history, filters, sorting, insights, thumbnails), `get_instagram_media_insights` (every metric for given posts: watch time and skip rate for reels, profile visits for posts, taps for stories), `get_instagram_top_media` (rank the posts of a period), `list_instagram_stories` (live stories with insights) |
| **Community** | `list_instagram_comments` (comments and replies), `list_instagram_tagged_media` (posts that tag you) |
| **Benchmarks** | `get_instagram_public_account`: public stats and recent posts of other Business or Creator accounts |
| **Reports** | `get_instagram_report`: everything above for a period in one stable JSON snapshot ([format](docs/report.md)) |
| **Anything else** | `query_instagram_api`: any read-only Graph API request for your account |

Full reference: [docs/tools.md](docs/tools.md). Every tool is annotated read-only, every result
states its exact date range and timezone, and anything Instagram can't provide is reported
instead of invented. Lists can be saved as CSV or JSON (`save_to`).

## Requirements

- An Instagram **professional** account (Creator or Business).
- A free [Meta developer account](https://developers.facebook.com) and a
  [business portfolio](https://business.facebook.com) (both take a minute to create).
- [uv](https://docs.astral.sh/uv/) (`brew install uv` on macOS) and Python 3.11+ (uv installs it
  if needed). macOS, Linux and Windows are supported.

## Quick start

```bash
uv tool install git+https://github.com/nelsonrogers/instagram-mcp
instagram-mcp auth      # paste the token from your Meta app (see below)
instagram-mcp check     # calls every tool against your account and prints the numbers
```

Then [connect your MCP client](#connect-your-mcp-client).

---

## One-time Meta app setup

About 15 minutes. Nothing here is ever published: the app stays in **development mode** (no
privacy policy, domain or website needed), and your Instagram account is added to it as a tester.
Meta's Standard Access covers accounts that have a role on the app, so **no App Review** is needed.
The person who creates the app and the person who owns the Instagram account can be the same or
different people.

> **Meta changed this in late 2026.** New apps no longer offer *API setup with Instagram login*;
> they offer **API setup with unified login** (recommended by Meta, no Facebook Page needed, but
> the app must belong to a business portfolio) and *API setup with Facebook login* (needs a
> Facebook Page linked to the Instagram account). The steps below use unified login. The server
> accepts tokens from all three setups and tells them apart by itself. Meta's dashboard labels
> still move: whatever the screens say, the goal is a token for your account with
> **`instagram_basic`** and **`instagram_manage_insights`** (plus `instagram_manage_comments` for
> the comment tools).

### 1. Create the app

1. <https://developers.facebook.com/apps> → **Create app**.
2. App name: anything, e.g. `My Instagram insights`; contact email: yours.
3. Use case: **Manage messaging & content on Instagram** (the "Instagram API" use case).
4. Business portfolio: **select your portfolio**. The unified login setup only appears for apps
   that belong to one.
5. Finish with **Create app**. Leave it in **Development** mode; don't publish it.

### 2. Set up unified login and its permissions

In the app: **Use cases** → *Manage messaging & content on Instagram* → **Customize**.

1. In the left menu, open **API setup with unified login** (not *API setup with Facebook login*,
   which needs a Facebook Page linked to the account).
2. Open **Permissions and features** and add `instagram_manage_insights`. Check that
   `instagram_basic` and `instagram_manage_insights` both show *Ready for testing*.
3. **The login configuration decides what the token can do**, not *Permissions and features*.
   Meta's default configuration asks for `instagram_basic`, messaging, comments, publishing and
   ads permissions, but **not** `instagram_manage_insights`: a token generated with it can list
   posts but gets *"(#10) Application does not have permission"* on every insight. Open the
   configuration (on *API setup with unified login*, or **Facebook Login for Business →
   Configurations**), **Edit permissions**, tick `instagram_manage_insights`, keep
   `instagram_basic` and `instagram_manage_comments`, and untick what this server never uses
   (ads, publishing, messaging). If it offers a token expiration, choose **Never**. If it asks for
   a redirect URL, `https://localhost/` will do.

### 3. Add the Instagram account as a tester

On **API setup with unified login**, section **Generate access tokens**:

1. **Add account** → your Instagram username. Meta invites the account as an **Instagram Tester**.
2. Accept the invite on a computer, at instagram.com, signed in as that account: **Settings →
   Website permissions → Apps and websites → Tester invites → Accept**
   (<https://www.instagram.com/accounts/manage_access/> usually opens that page). The invite only
   appears after step 1, and only on the account that was invited.

### 4. Generate the token

Back on **Generate access tokens**, click **Generate token** next to the account (you may have to
sign in to Instagram and approve the permissions), tick the box and **copy** the token. Meta shows
it only once.

Don't paste the token into a chat, an email, a note, an issue or a file in a repository.

### 5. Save the token

```bash
instagram-mcp auth
```

Paste the token at the prompt (it is not shown on screen). The command recognises the kind of
token, finds the Instagram account it can read, checks that insights work (and if not, lists the
permissions Meta actually granted), and saves the token in
`~/.config/instagram-mcp/tokens/default.json` (readable only by your user, outside every
repository). If it says Meta didn't list any account, pass the account's ID yourself:
`instagram-mcp auth --account 1784…`.

<details>
<summary>Apps created before late 2026 (API setup with Instagram login)</summary>

Their dashboards still show *API setup with Instagram login*, with the permissions
`instagram_business_basic`, `instagram_business_manage_insights` and (for comments)
`instagram_business_manage_comments`. The token from **Generate access tokens → Generate token**
(it starts with `IG`) is saved the same way, with `instagram-mcp auth`. It lasts 60 days and the
server renews it automatically (see [Token lifetime](#token-lifetime)). Looking up other accounts
(`get_instagram_public_account`) isn't available with these tokens.

To sign in through the browser instead of copying a dashboard token:

```bash
instagram-mcp auth --oauth
```

This needs the **Instagram app ID** and **Instagram app secret** shown on the *API setup with
Instagram login* page (not the Meta App ID). It asks for them (the secret is hidden; it is used
once and never saved), or reads `INSTAGRAM_MCP_APP_ID` / `INSTAGRAM_MCP_APP_SECRET`. First register
`https://localhost/` under **Business login settings → OAuth redirect URIs** (or pass another
registered URI with `--redirect-uri`). After you approve, the browser lands on that address, which
doesn't load; paste the whole address back into the terminal.
</details>

---

## Connect your MCP client

Use the full path printed by `which instagram-mcp` (usually `~/.local/bin/instagram-mcp`).

**ChatGPT desktop app:** **Settings → MCP servers → Add server** → **STDIO**, name `instagram`,
command: the full path to `instagram-mcp`. Save, then **Restart**. It is stored in
`~/.codex/config.toml`:

```toml
[mcp_servers.instagram]
command = "/Users/YOU/.local/bin/instagram-mcp"
```

**Claude Desktop, Cursor and other clients:**

```json
{ "mcpServers": { "instagram": { "command": "/Users/YOU/.local/bin/instagram-mcp" } } }
```

**Claude Code:** `claude mcp add instagram -- instagram-mcp`.

To set the timezone (or any other setting), add an `env` block, e.g.
`"env": { "INSTAGRAM_MCP_TIMEZONE": "Europe/Paris" }`.

The server also offers ready-made prompts (*Instagram check-up*, *Post deep dive*, *Audience
portrait*, *Benchmark accounts*) in clients that show MCP prompts.

## Token lifetime

`instagram-mcp status` shows the account, the API it uses and when the token expires.

- **Unified login tokens** (they start with `EAA`, host graph.facebook.com) belong to the business
  portfolio, not to a person. Their lifetime is set in the app's configuration, often **never**.
  They can't be renewed through the API: if one expires or stops working, generate a new one
  (step 4) and run `instagram-mcp auth` again.
- **Instagram Login tokens** (older apps; they start with `IG`) last **60 days**. The server
  renews them automatically whenever it is used and the token is about 10 days old or more;
  `instagram-mcp refresh` renews it now. After more than 60 days without use, generate a new
  token and run `instagram-mcp auth`.

## CLI

```text
instagram-mcp [serve]      run the MCP server over stdio (what MCP clients launch)
instagram-mcp auth         save a token from the Meta App Dashboard (--token-stdin, --account, --oauth)
instagram-mcp status       account, token expiry, settings
instagram-mcp check        call every tool against your account and print the results
instagram-mcp report       write a full report as JSON (--period, --start-date, --end-date, --thumbnails, --out)
instagram-mcp refresh      renew an Instagram Login token now
instagram-mcp logout       delete the stored token
```

## Configuration

All optional, as environment variables (also usable in an MCP client's `env` block).

| Variable | Default | Purpose |
|---|---|---|
| `INSTAGRAM_MCP_TIMEZONE` | the computer's | IANA timezone for days and times, e.g. `Europe/Paris` |
| `INSTAGRAM_MCP_OUTPUT_DIR` | `~/Documents/instagram-mcp` | Where `save_to` files go |
| `INSTAGRAM_MCP_HOME` | `~/.config/instagram-mcp` | Where the token lives |
| `INSTAGRAM_MCP_PROFILE` | `default` | Token profile, for a second account (`auth --profile NAME`) |
| `INSTAGRAM_MCP_API_VERSION` | `v26.0` | Graph API version |
| `INSTAGRAM_MCP_APP_ID` / `_APP_SECRET` / `_REDIRECT_URI` | | Only for `auth --oauth`; never stored |
| `INSTAGRAM_MCP_LOG_LEVEL` | `WARNING` | Logging, always to stderr |

## Good to know

- **Periods.** Named periods (`last_7_days` … `last_365_days`, `this_month`, `last_month`,
  `this_year`, `last_year`, `all_available`) end yesterday; custom dates can go back two years,
  Meta's limit. Meta's numbers can still change for 48 hours.
- **Reach over long periods.** Meta reads at most 30 days per request. Totals like views and likes
  are added up over longer periods, but reach and accounts engaged count unique accounts and can't
  be, so they are `null` beyond 29 days (use weekly trends instead).
- **Post metrics are lifetime totals**, since publication. Meta doesn't give `follows` or profile
  visits for reels, and lists stories only during their 24 hours.
- **Audience.** Demographics need at least 100 followers (or engaged accounts) and cover the top
  45 cities and countries. `app_share_pct` and `share_of_known_pct` reproduce the Instagram app's
  own charts, which leave out followers of unknown gender.
- **Not available through Meta's API:** hashtag search (needs Meta App Review), expired stories,
  and collaboration posts owned by another account.

How the numbers are computed, and what was checked against a real account:
[docs/decisions.md](docs/decisions.md).

## Privacy & security

- Read-only: the server only calls GET endpoints for profile fields, insights, media, comments
  and tags, and every tool is annotated read-only. If the token carries more permissions
  (messaging, publishing), they are never used.
- Runs locally over stdio, with no telemetry or third-party service.
- The token is stored outside any repository with owner-only permissions (`0600` in a `0700`
  folder). It never appears in logs, error messages or tool results.
- Comment text and other people's captions are returned to your agent as data: they could contain
  instructions aimed at AI agents, so review what your agent proposes.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Troubleshooting

1. `instagram-mcp check` calls every tool and prints what came back, with warnings for empty
   answers.
2. *"(#10) Application does not have permission"*: the token lacks `instagram_manage_insights`
   (setup step 2.3); generate a new token. For comments, the same with
   `instagram_manage_comments`.
3. Sign-in problems: `instagram-mcp status`, then `instagram-mcp auth` with a new token.
4. Numbers differ slightly from the app: Meta's data can lag up to 48 hours, and the app may use
   another timezone; set `INSTAGRAM_MCP_TIMEZONE`.

## Development

```bash
git clone https://github.com/nelsonrogers/instagram-mcp && cd instagram-mcp
uv sync
uv run pytest                     # against a rule-checking fake of the Graph API, no account needed
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run python scripts/generate_docs.py   # after changing a tool or the report
```

```text
cli.py ─► server.py ─► tools/ account · analytics · content · discovery · report  (all read-only)
          prompts.py   MCP prompt templates
          selfcheck.py `instagram-mcp check`: every tool through an in-process MCP client
insights.py    Meta's API → snake_case: totals, breakdowns, demographics, posts
periods.py     named periods, timezone, 29-day request windows, buckets
context.py     shared arguments, errors, save_to
thumbnails.py  160 px WebP data URIs
instagram.py   Graph client: versioned GETs, error translation, retries, cursors
auth.py        token store (0600), both token kinds, auto-refresh, OAuth
config.py      INSTAGRAM_MCP_* settings
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE). Not affiliated with, endorsed or sponsored by Meta or Instagram. Instagram is a
trademark of Meta Platforms, Inc.
