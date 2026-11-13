"""Settings and file locations, resolved from environment variables.

Every setting can be given as an ``INSTAGRAM_MCP_<NAME>`` environment variable, so MCP clients
(ChatGPT desktop, Claude Desktop, ...) can configure the server from their ``env`` block.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "INSTAGRAM_MCP_"
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VERSION_RE = re.compile(r"^v\d+\.\d+$")

# Graph API version (both hosts): the newest one in Meta's version table in
# November 2026 (the Instagram reference pages already mention v27.0). Meta keeps each version
# for about two years; override with INSTAGRAM_MCP_API_VERSION.
DEFAULT_API_VERSION = "v26.0"


def env(name: str) -> str | None:
    """Return a stripped ``INSTAGRAM_MCP_<name>`` value, or None when unset/blank."""
    value = os.environ.get(ENV_PREFIX + name)
    if value is None or not value.strip():
        return None
    return value.strip()


def default_config_dir() -> Path:
    """Where the token lives: ``~/.config/instagram-mcp`` (``%APPDATA%\\instagram-mcp`` on Windows)."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "instagram-mcp"


def default_output_dir() -> Path:
    """Where ``save_to`` files go when the caller gives a bare filename."""
    documents = Path.home() / "Documents"
    return (documents if documents.is_dir() else Path.home()) / "instagram-mcp"


def validate_profile(profile: str) -> str:
    if not _PROFILE_RE.match(profile):
        raise ValueError(f"Invalid profile name {profile!r}: use 1-64 letters, digits, '-' or '_'.")
    return profile


def validate_api_version(version: str) -> str:
    if not _VERSION_RE.match(version):
        raise ValueError(f"Invalid Graph API version {version!r}: expected something like 'v26.0'.")
    return version


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration."""

    config_dir: Path = field(default_factory=default_config_dir)
    output_dir: Path = field(default_factory=default_output_dir)
    profile: str = "default"
    api_version: str = DEFAULT_API_VERSION
    # IANA name for the report's days; None means the computer's timezone.
    timezone: str | None = None
    # Only used by `instagram-mcp auth --oauth`; never written to disk.
    app_id: str | None = None
    app_secret: str | None = None
    redirect_uri: str | None = None

    @property
    def token_file(self) -> Path:
        return self.config_dir / "tokens" / f"{self.profile}.json"

    @classmethod
    def from_env(cls, *, profile: str | None = None) -> Settings:
        config_dir = env("HOME")
        output_dir = env("OUTPUT_DIR")
        return cls(
            config_dir=Path(config_dir).expanduser() if config_dir else default_config_dir(),
            output_dir=Path(output_dir).expanduser() if output_dir else default_output_dir(),
            timezone=env("TIMEZONE"),
            profile=validate_profile(profile or env("PROFILE") or "default"),
            api_version=validate_api_version(env("API_VERSION") or DEFAULT_API_VERSION),
            app_id=env("APP_ID"),
            app_secret=env("APP_SECRET"),
            redirect_uri=env("REDIRECT_URI"),
        )
