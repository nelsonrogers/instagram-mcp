# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-13

First public version.

### Added

- Local, read-only stdio MCP server for Instagram Business and Creator accounts, with 15 tools:
  - `get_instagram_auth_status`, `get_instagram_profile`;
  - `get_instagram_performance_summary` (period totals vs the previous period, by content type and
    by followers vs non-followers), `get_instagram_metrics_over_time` (day, week or month
    buckets, estimated follower count), `get_instagram_audience` (age, gender, age × gender,
    cities, countries of followers or engaged accounts), `get_instagram_online_followers`;
  - `list_instagram_media`, `get_instagram_media_insights`, `get_instagram_top_media`,
    `list_instagram_stories`, `list_instagram_comments`, `list_instagram_tagged_media`;
  - `get_instagram_public_account` (Business Discovery);
  - `get_instagram_report` (one-call snapshot, `schema_version` 1, documented in docs/report.md);
  - `query_instagram_api` (raw read-only Graph API requests).
- Named periods and custom dates up to two years back, read in windows under Meta's 30-day limit;
  a configurable report timezone (`INSTAGRAM_MCP_TIMEZONE`); `save_to` for CSV/JSON files.
- Four MCP prompts: Instagram check-up, Post deep dive, Audience portrait, Benchmark accounts.
- Tokens from Meta's **API setup with unified login** (graph.facebook.com), which new Meta apps
  get since late 2026, and from the older **Instagram Login** (graph.instagram.com).
  `instagram-mcp auth` recognises the token, finds the Instagram account and, when insights are
  refused, lists the permissions Meta granted.
- CLI: `auth`, `status`, `check` (every tool against your account), `report`, `refresh`,
  `logout`.
- A rule-checking fake of the Graph API that mirrors the behaviour observed on a real account,
  generated tool docs and report sample, both checked by tests.

[Unreleased]: https://github.com/nelsonrogers/instagram-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nelsonrogers/instagram-mcp/releases/tag/v0.1.0
