"""The command line: report export, token saving (mocked Meta API), status."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from instagram_mcp import cli
from instagram_mcp.auth import TokenStore
from instagram_mcp.config import DEFAULT_API_VERSION as V
from instagram_mcp.config import Settings
from tests.fakes import FakeInstagramAPI

TOKEN = "IGAAdashboard-token"


@pytest.fixture
def env_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Settings:
    monkeypatch.setenv("INSTAGRAM_MCP_HOME", str(settings.config_dir))
    monkeypatch.setenv("INSTAGRAM_MCP_TIMEZONE", "Europe/Paris")
    return settings


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeInstagramAPI:
    api = FakeInstagramAPI()
    monkeypatch.setattr("instagram_mcp.instagram.GraphInstagramClient", lambda _settings: api)
    return api


def test_report_writes_the_json(
    env_settings: Settings, fake_client: FakeInstagramAPI, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "reports" / "instagram-report.json"
    assert cli.main(["report", "--out", str(out), "--thumbnails"]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == "1" and data["profile"]["handle"] == "@atelier.lune"
    assert len(data["posts"]) == 15 and data["posts"][0]["thumbnail"].startswith("data:image/webp;base64,")
    out_text = capsys.readouterr().out
    assert (
        "Saved the report for @atelier.lune" in out_text
        and "15 posts from 2026-10-14 to 2026-11-10" in out_text
    )


def test_report_period_and_relative_out(
    env_settings: Settings, fake_client: FakeInstagramAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert (
        cli.main(["report", "--out", "r.json", "--start-date", "2026-11-01", "--end-date", "2026-11-07"]) == 0
    )
    data = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert data["date_range"]["days"] == 7 and all(post.get("thumbnail") is None for post in data["posts"])
    assert fake_client.downloads == []


def test_report_to_stdout_and_bad_paths(
    env_settings: Settings, fake_client: FakeInstagramAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["report", "--out", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == "Instagram Graph API"
    assert cli.main(["report", "--out", "report.csv"]) == 1
    assert cli.main(["report", "--out", str(env_settings.config_dir / "x.json")]) == 1
    assert "token directory" in capsys.readouterr().err
    assert cli.main(["report", "--out", "-", "--period", "last_decade"]) == 1


def graph_handler(*, insights_status: int = 200) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["access_token"] == TOKEN
        if request.url.path == f"/{V}/me":
            return httpx.Response(
                200,
                json={
                    "id": "1",
                    "user_id": "17841400000000001",
                    "username": "atelier.lune",
                    "account_type": "Media_Creator",
                },
            )
        if request.url.path == f"/{V}/me/insights":
            if insights_status != 200:
                return httpx.Response(
                    insights_status, json={"error": {"message": "Insufficient permission", "code": 10}}
                )
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"error": {"message": "Unknown path", "code": 100}})

    return handler


def patch_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real_client = httpx.Client

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("instagram_mcp.cli.httpx.Client", factory)


def test_auth_saves_a_pasted_dashboard_token(
    env_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_http(monkeypatch, graph_handler())
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN + "\n"))
    assert cli.main(["auth", "--token-stdin"]) == 0
    token = TokenStore(env_settings.token_file).load()
    assert token is not None and token.access_token == TOKEN
    assert token.username == "atelier.lune" and token.user_id == "17841400000000001"
    assert token.expires_at_estimated and token.source == "dashboard"
    out = capsys.readouterr().out
    assert "Signed in as @atelier.lune (Media_Creator)" in out and TOKEN not in out


def test_auth_warns_when_insights_are_not_granted(
    env_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_http(monkeypatch, graph_handler(insights_status=403))
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN))
    assert cli.main(["auth", "--token-stdin"]) == 1
    assert "instagram_business_manage_insights" in capsys.readouterr().out


def test_auth_rejects_an_invalid_token(
    env_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_http(
        monkeypatch,
        lambda _r: httpx.Response(
            400, json={"error": {"message": "Invalid OAuth access token", "code": 190}}
        ),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN))
    assert cli.main(["auth", "--token-stdin"]) == 1
    assert "Invalid OAuth access token" in capsys.readouterr().err
    assert not env_settings.token_file.exists()


def test_status_and_logout(env_settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status"]) == 1
    assert json.loads(capsys.readouterr().out)["signed_in"] is False
    assert cli.main(["logout"]) == 0
    assert "No token stored" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["-v"], ["--profile", "work"], ["serve", "-v"], ["-v", "serve"]])
def test_no_command_runs_the_server(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(cli, "cmd_serve", lambda args: seen.append(args) or 0)
    monkeypatch.setattr("instagram_mcp.cli.build_parser", _parser_with_patched_serve(seen))
    assert cli.main(argv) == 0
    assert len(seen) == 1
    assert seen[0].verbose is ("-v" in argv)
    assert seen[0].profile == ("work" if "work" in argv else None)


def _parser_with_patched_serve(seen: list[Any]) -> Any:
    real = cli.build_parser

    def build() -> Any:
        parser = real()
        for action in parser._subparsers._group_actions:  # type: ignore[union-attr]
            action.choices["serve"].set_defaults(func=lambda args: seen.append(args) or 0)
        return parser

    return build


def test_bad_out_is_refused_before_any_api_call(
    env_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_api(_settings: Settings) -> None:
        raise AssertionError("the API must not be called for an invalid --out")

    monkeypatch.setattr("instagram_mcp.instagram.GraphInstagramClient", no_api)
    assert cli.main(["report", "--out", "report.txt"]) == 1


# --------------------------------------------------------------------------- unified login

UNIFIED = "EAAsystem-user-token"
IG_ID = "17841400000000001"


def unified_handler(
    *,
    assigned: bool = True,
    pages: bool = False,
    insights_status: int = 200,
    debug_token: bool = True,
    seen: list[str] | None = None,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "graph.facebook.com"
        assert request.url.params["access_token"] == UNIFIED
        path = request.url.path.removeprefix(f"/{V}")
        if seen is not None:
            seen.append(path)
        if path == "/me/assigned_instagram_accounts":
            data = [{"id": IG_ID, "username": "atelier.lune"}] if assigned else []
            return httpx.Response(200, json={"data": data, "paging": {"cursors": {}}})
        if path == "/me/accounts":
            if not pages:
                return httpx.Response(
                    400, json={"error": {"message": "nonexisting field (accounts)", "code": 100}}
                )
            page = {"id": "1", "instagram_business_account": {"id": IG_ID, "username": "atelier.lune"}}
            return httpx.Response(200, json={"data": [page, {"id": "2"}]})
        if path == "/debug_token":
            assert request.url.params["input_token"] == UNIFIED
            if not debug_token:  # what Meta answers for a system-user token
                message = "(#100) You must provide an app access token, or a user access token"
                return httpx.Response(400, json={"error": {"message": message, "code": 100}})
            scopes = ["instagram_basic", "instagram_manage_insights", "instagram_manage_messages"]
            return httpx.Response(
                200, json={"data": {"type": "SYSTEM_USER", "expires_at": 0, "scopes": scopes}}
            )
        if path == "/me/permissions":  # Meta's default unified login configuration
            granted = ["ads_management", "ads_read", "instagram_basic", "instagram_manage_messages"]
            return httpx.Response(
                200, json={"data": [{"permission": name, "status": "granted"} for name in granted]}
            )
        if path == f"/{IG_ID}":
            assert "account_type" not in request.url.params["fields"]  # not a field on graph.facebook.com
            return httpx.Response(200, json={"id": IG_ID, "username": "atelier.lune"})
        if path == f"/{IG_ID}/insights":
            if insights_status != 200:
                return httpx.Response(
                    insights_status, json={"error": {"message": "Permission denied", "code": 10}}
                )
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"error": {"message": f"Unknown path {path}", "code": 100}})

    return handler


def test_auth_saves_a_unified_login_token(
    env_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_http(monkeypatch, unified_handler())
    monkeypatch.setattr("sys.stdin", io.StringIO(UNIFIED + "\n"))
    assert cli.main(["auth", "--token-stdin"]) == 0
    token = TokenStore(env_settings.token_file).load()
    assert token is not None and token.graph == "facebook" and token.user_id == IG_ID
    assert token.username == "atelier.lune" and token.expires_at is None and not token.expires_at_estimated
    assert "instagram_manage_insights" in token.permissions
    out = capsys.readouterr().out
    assert "Signed in as @atelier.lune (unified login, graph.facebook.com)" in out
    assert "never expires" in out and UNIFIED not in out


def test_auth_finds_the_account_through_facebook_pages(
    env_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    patch_http(monkeypatch, unified_handler(assigned=False, pages=True, seen=seen))
    monkeypatch.setattr("sys.stdin", io.StringIO(UNIFIED))
    assert cli.main(["auth", "--token-stdin"]) == 0
    token = TokenStore(env_settings.token_file).load()
    assert token is not None and token.user_id == IG_ID
    assert seen[:2] == ["/me/assigned_instagram_accounts", "/me/accounts"]


def test_auth_asks_for_the_account_when_meta_lists_none(
    env_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_http(monkeypatch, unified_handler(assigned=False))
    monkeypatch.setattr("sys.stdin", io.StringIO(UNIFIED))
    assert cli.main(["auth", "--token-stdin"]) == 1
    assert "--account" in capsys.readouterr().err and not env_settings.token_file.exists()

    monkeypatch.setattr("sys.stdin", io.StringIO(UNIFIED))
    assert cli.main(["auth", "--token-stdin", "--account", IG_ID]) == 0
    token = TokenStore(env_settings.token_file).load()
    assert token is not None and token.user_id == IG_ID


def test_auth_unified_login_without_insights(
    env_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_http(monkeypatch, unified_handler(insights_status=403))
    monkeypatch.setattr("sys.stdin", io.StringIO(UNIFIED))
    assert cli.main(["auth", "--token-stdin"]) == 1
    out = capsys.readouterr().out
    assert "Add instagram_manage_insights to the app's login configuration" in out


def test_auth_names_the_permissions_meta_granted(
    env_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Meta refuses /debug_token to system-user tokens; /me/permissions still says what's granted."""
    patch_http(monkeypatch, unified_handler(insights_status=403, debug_token=False))
    monkeypatch.setattr("sys.stdin", io.StringIO(UNIFIED))
    assert cli.main(["auth", "--token-stdin"]) == 1
    out = capsys.readouterr().out
    assert "Meta issued it without instagram_manage_insights. It has: ads_management, ads_read" in out
    assert "doesn't say when this token expires" not in out
    token = TokenStore(env_settings.token_file).load()
    assert token is not None and token.expires_at is None and token.expires_at_estimated
    assert token.permissions == ["ads_management", "ads_read", "instagram_basic", "instagram_manage_messages"]


def test_refresh_explains_unified_login_tokens(
    env_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_http(monkeypatch, unified_handler())
    monkeypatch.setattr("sys.stdin", io.StringIO(UNIFIED))
    assert cli.main(["auth", "--token-stdin"]) == 0
    assert cli.main(["refresh"]) == 1
    assert "can't be renewed through the API" in capsys.readouterr().err
