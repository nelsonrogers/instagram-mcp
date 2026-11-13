## What and why

<!-- What does this change, and which creator workflow does it help? -->

## Checklist

- [ ] `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy` pass
- [ ] New behaviour is tested against the fake API (`tests/fakes.py`), with Meta's documented constraints encoded there
- [ ] `get_instagram_report` only gained keys (no renames/removals within a schema version)
- [ ] Docs regenerated (`uv run python scripts/generate_docs.py`) and updated (README, `docs/report.md`, `docs/decisions.md` for facts about Meta's API)
- [ ] No tokens, secrets or real account data in code, fixtures, logs or screenshots
- [ ] CHANGELOG.md updated under *Unreleased*
