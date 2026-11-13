# Contributing

Thanks for helping creators get more out of their data! Issues and pull requests are welcome.

## Setup

```bash
git clone https://github.com/nelsonrogers/instagram-mcp && cd instagram-mcp
uv sync
uv run pytest
```

You don't need an Instagram account or a Meta app to develop: the test suite runs every tool
against `tests/fakes.py`, a deterministic fake of the Instagram Graph API (both
graph.instagram.com and graph.facebook.com) that serves a fictional account. The fake also
**rejects the request shapes the real API rejects** (metrics deprecated in 2025, a missing
`metric_type`, ranges over 30 days, metrics a media type doesn't support). If you rely on a new
endpoint or metric, encode its documented constraints in the fake too.

To try changes against your own account, save a token (`uv run instagram-mcp auth`) and run
`uv run instagram-mcp check`, or open the MCP Inspector:
`npx @modelcontextprotocol/inspector uv run instagram-mcp`.

## Before opening a PR

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
uv run python scripts/generate_docs.py   # if you changed a tool or get_instagram_report
```

## Guidelines

- **Tool descriptions are the UI.** Agents pick tools from their descriptions and parameter docs:
  say what the tool answers, units, and caveats, and point to sibling tools. Prefer a parameter
  over a near-duplicate tool. Every tool name contains `instagram`.
- **Outputs are snake_case** with units in key names (`*_pct`, `*_s`), stable codes plus labels,
  and the exact `date_range`. Raw Meta names belong only in `query_instagram_api`.
- **Probe Meta live before trusting its docs**, then encode what you saw in `tests/fakes.py`
  and `docs/decisions.md`. Fallbacks are reported in `notes`, never silent.
- **Never write to stdout** in server code (it carries JSON-RPC). Use `logging` (stderr).
- **Anticipated failures raise `ToolError`** (via `@tool_errors`) with an actionable message; the
  user is often non-technical.
- **Report contract:** within a `schema_version`, only add keys to `get_instagram_report`, never
  rename or remove them. Update [docs/report.md](docs/report.md) and regenerate the sample.
- **Read-only.** The server never changes anything on the account.
- **Record API facts and educated guesses** in [docs/decisions.md](docs/decisions.md).
- **No secrets or personal data in issues, fixtures or logs.** Tokens, app secrets, real account
  names and real exports stay out of the repository; test data is fictional.
- Add an entry to `CHANGELOG.md` under *Unreleased*.

## Releasing (maintainers)

1. Update `version` in `pyproject.toml` and move *Unreleased* in `CHANGELOG.md` under the version.
2. Commit, tag `vX.Y.Z`, push the tag. The release workflow tests, builds and creates the GitHub
   release with the wheel and sdist attached.
