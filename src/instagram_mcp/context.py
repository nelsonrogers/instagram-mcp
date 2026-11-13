"""State shared by all tools, plus helpers for arguments, errors and output files."""

from __future__ import annotations

import csv
import functools
import json
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Annotated, Any, ParamSpec, TypeVar

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from instagram_mcp.auth import AuthError
from instagram_mcp.config import Settings
from instagram_mcp.instagram import InstagramAPI, InstagramAPIError
from instagram_mcp.periods import (
    DateRange,
    Period,
    PeriodError,
    detect_timezone,
    load_timezone,
    resolve_period,
)
from instagram_mcp.thumbnails import ThumbnailError

P = ParamSpec("P")
R = TypeVar("R")

PROFILE_CACHE_SECONDS = 300

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

# --------------------------------------------------------------------------- shared argument types

PeriodArg = Annotated[
    Period | None,
    Field(
        description=(
            "Named date range. Rolling ranges end yesterday (the latest complete day) in the report "
            "timezone; Instagram keeps account insights for 2 years ('all_available'). Ignored when "
            "start_date is set."
        )
    ),
]
StartDateArg = Annotated[
    str | None,
    Field(description="Custom range start, YYYY-MM-DD (at most 2 years back). Overrides `period`."),
]
EndDateArg = Annotated[
    str | None,
    Field(
        description="Custom range end, YYYY-MM-DD (inclusive). Defaults to yesterday; today gives a partial day."
    ),
]
SaveToArg = Annotated[
    str | None,
    Field(
        description=(
            "Optional file to also save the rows to (.csv or .json). A bare filename goes in the output "
            "directory (INSTAGRAM_MCP_OUTPUT_DIR); absolute paths are allowed."
        )
    ),
]


@dataclass
class AppContext:
    settings: Settings
    api: InstagramAPI
    _tz: tzinfo | None = field(default=None, repr=False)
    _profiles: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- time ------------------------------------------------------------------------------

    @property
    def tz(self) -> tzinfo:
        """The report timezone: INSTAGRAM_MCP_TIMEZONE, else the computer's, else UTC."""
        if self._tz is None:
            self._tz = load_timezone(self.settings.timezone or detect_timezone())
        return self._tz

    def resolve(
        self,
        period: str | None,
        start_date: str | None,
        end_date: str | None,
        *,
        default: str = "last_28_days",
    ) -> DateRange:
        return resolve_period(period, start_date, end_date, tz=self.tz, default=default)

    def local_time(self, instant: datetime | None) -> str | None:
        return instant.astimezone(self.tz).isoformat() if instant else None

    # -- account ---------------------------------------------------------------------------

    def profile(self, fields: str, *, fresh: bool = False) -> dict[str, Any]:
        """The connected account's profile fields (cached for a few minutes per field list)."""
        with self._lock:
            cached = self._profiles.get(fields)
        if cached and not fresh and time.monotonic() - cached[0] < PROFILE_CACHE_SECONDS:
            return cached[1]
        profile = self.api.get("me", {"fields": fields})
        with self._lock:
            self._profiles[fields] = (time.monotonic(), profile)
        return profile

    # -- files -----------------------------------------------------------------------------

    def output_path(self, filename: str, *, allowed: Sequence[str], default_suffix: str) -> Path:
        """Resolve where to write an output file, creating parent directories."""
        path = Path(filename).expanduser()
        if not path.is_absolute():
            path = self.settings.output_dir / path
        if not path.suffix:
            path = path.with_suffix(default_suffix)
        if path.suffix.lower() not in allowed:
            raise ToolError(f"Unsupported file type {path.suffix!r}; use one of: {', '.join(allowed)}.")
        resolved = path.resolve()
        config_dir = self.settings.config_dir.resolve()
        if resolved == config_dir or config_dir in resolved.parents:
            raise ToolError("Refusing to write inside the token directory; choose another location.")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def save_rows(self, rows: Sequence[dict[str, Any]], filename: str) -> str:
        path = self.output_path(filename, allowed=(".csv", ".json"), default_suffix=".csv")
        if path.suffix.lower() == ".json":
            path.write_text(
                json.dumps(list(rows), indent=2, ensure_ascii=False, default=str), encoding="utf-8"
            )
            return str(path)
        columns: list[str] = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        k: (json.dumps(v, ensure_ascii=False) if isinstance(v, list | dict) else v)
                        for k, v in row.items()
                    }
                )
        return str(path)

    def save_json(self, payload: Any, filename: str) -> str:
        path = self.output_path(filename, allowed=(".json",), default_suffix=".json")
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return str(path)


def tool_errors(fn: Callable[P, R]) -> Callable[P, R]:
    """Turn anticipated failures into ``ToolError`` so the agent sees a useful message.

    Unexpected exceptions are re-raised; the SDK logs them and reports a generic error.
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except (AuthError, InstagramAPIError, PeriodError, ThumbnailError) as exc:
            raise ToolError(str(exc)) from exc
        except ValueError as exc:
            raise ToolError(f"Invalid input: {exc}") from exc

    return wrapper
