"""Command-line entry point: run the MCP server, save a token, export a report."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import secrets
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from instagram_mcp import __version__
from instagram_mcp.auth import (
    AUTH_COMMAND,
    FACEBOOK,
    GRAPH_HOSTS,
    HTTP_TIMEOUT,
    INSTAGRAM,
    LONG_LIVED_LIFETIME,
    PERMISSIONS_BY_GRAPH,
    SCOPES,
    AuthError,
    StoredToken,
    TokenManager,
    TokenStore,
    authorize_url,
    code_from_redirect,
    exchange_code,
    exchange_for_long_lived,
    utcnow,
)
from instagram_mcp.config import Settings, env
from instagram_mcp.instagram import PERMISSION_ERROR_CODES, InstagramAPIError, translate_error

logger = logging.getLogger("instagram_mcp")

DEFAULT_REDIRECT_URI = "https://localhost/"
PROFESSIONAL_ACCOUNT_TYPES = {"BUSINESS", "MEDIA_CREATOR"}


def _configure_logging(verbose: bool = False) -> None:
    level = "DEBUG" if verbose else (env("LOG_LEVEL") or "WARNING").upper()
    logging.basicConfig(
        stream=sys.stderr,  # stdout is reserved for MCP JSON-RPC messages
        level=getattr(logging, level, logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full request URLs at INFO, and Graph API URLs carry the access token.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.from_env(profile=getattr(args, "profile", None))


# --------------------------------------------------------------------------- commands


def cmd_serve(args: argparse.Namespace) -> int:
    from instagram_mcp.server import build_server

    build_server(_settings(args)).run("stdio")
    return 0


def detect_graph(access_token: str) -> str | None:
    """Instagram Login tokens start with "IG"; Facebook ones (unified login) with "EAA"."""
    if access_token.startswith("IG"):
        return INSTAGRAM
    if access_token.startswith("EA"):
        return FACEBOOK
    return None


def _get(
    http: httpx.Client, url: str, params: dict[str, Any], access_token: str, graph: str
) -> dict[str, Any]:
    try:
        response = http.get(url, params={**params, "access_token": access_token}, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise AuthError(f"Could not reach Instagram ({type(exc).__name__}).") from exc
    if response.status_code >= 400:
        raise translate_error(response, (access_token,), graph=graph)
    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthError("Meta sent an unreadable answer.") from exc
    return payload if isinstance(payload, dict) else {}


def probe_token(
    http: httpx.Client, settings: Settings, access_token: str, *, graph: str = INSTAGRAM, node: str = "me"
) -> dict[str, Any]:
    """Read the account behind a token, and check that insights are readable."""
    base = f"{GRAPH_HOSTS[graph]}/{settings.api_version}"
    fields = "user_id,username,account_type" if graph == INSTAGRAM else "id,username"
    me = _get(http, f"{base}/{node}", {"fields": fields}, access_token, graph)
    try:
        insights = http.get(
            f"{base}/{node}/insights",
            params={
                "metric": "views",
                "period": "day",
                "metric_type": "total_value",
                "access_token": access_token,
            },
            timeout=HTTP_TIMEOUT,
        )
        error = (
            None if insights.status_code < 400 else translate_error(insights, (access_token,), graph=graph)
        )
    except httpx.HTTPError as exc:
        error = InstagramAPIError(f"could not reach Instagram ({type(exc).__name__})")
    me["_insights_error"] = str(error) if error else None
    me["_insights_denied"] = isinstance(error, InstagramAPIError) and error.code in PERMISSION_ERROR_CODES
    return me


def find_instagram_accounts(http: httpx.Client, base: str, access_token: str) -> list[dict[str, str]]:
    """The Instagram accounts a graph.facebook.com token can read.

    Unified login: the accounts assigned to the system user. Facebook Login: the accounts linked
    to the person's Facebook Pages. A rejected token raises AuthError.
    """
    found: list[Any] = []
    try:
        data = _get(
            http, f"{base}/me/assigned_instagram_accounts", {"fields": "id,username"}, access_token, FACEBOOK
        )
        found = list(data.get("data") or [])
    except InstagramAPIError as exc:
        logger.debug("assigned_instagram_accounts: %s", exc)
    if not found:
        try:
            data = _get(
                http,
                f"{base}/me/accounts",
                {"fields": "instagram_business_account{id,username}"},
                access_token,
                FACEBOOK,
            )
            found = [page.get("instagram_business_account") for page in data.get("data") or []]
        except InstagramAPIError as exc:
            logger.debug("me/accounts: %s", exc)
    return [
        {"id": str(item["id"]), "username": str(item.get("username") or "")}
        for item in found
        if isinstance(item, dict) and item.get("id")
    ]


def choose_account(accounts: list[dict[str, str]], wanted: str | None) -> str:
    """The Instagram account ID to read: the only one, or the one named with ``--account``."""
    names = ", ".join(f"@{a['username']} ({a['id']})" for a in accounts) or "none"
    if wanted:
        wanted = wanted.strip().lstrip("@")
        for account in accounts:
            if wanted in (account["id"], account["username"]):
                return account["id"]
        if wanted.isdigit():
            return wanted  # an ID Meta didn't list: the profile check below tells whether it works
        raise AuthError(f"This token can't see @{wanted}. Accounts it can read: {names}.")
    if len(accounts) == 1:
        return accounts[0]["id"]
    if not accounts:
        raise AuthError(
            "Meta didn't say which Instagram account this token can read. Check that the Instagram "
            "account was added under 'Generate access tokens' (and, if that's done, add business_management to "
            "the configuration's permissions), or pass the account's ID with --account <ID>."
        )
    raise AuthError(f"This token can read several Instagram accounts ({names}): choose one with --account.")


def token_details(
    http: httpx.Client, base: str, access_token: str
) -> tuple[datetime | None, bool, list[str]]:
    """``(expires_at, expiry_unknown, permissions)`` from ``/debug_token`` (best effort).

    Meta refuses ``/debug_token`` to system-user tokens ("You must provide an app access token"),
    so their permissions come from ``/me/permissions`` and their expiry stays unknown.
    """
    try:
        data = _get(http, f"{base}/debug_token", {"input_token": access_token}, access_token, FACEBOOK)
    except (AuthError, InstagramAPIError) as exc:
        logger.debug("debug_token: %s", exc)
        return None, True, granted_permissions(http, base, access_token)
    raw_info = data.get("data")
    info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
    scopes = [str(scope) for scope in info.get("scopes") or []]
    try:
        expires = int(info["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None, True, scopes
    if expires == 0:  # Meta's way of saying "never"
        return None, False, scopes
    return datetime.fromtimestamp(expires, UTC), False, scopes


def granted_permissions(http: httpx.Client, base: str, access_token: str) -> list[str]:
    """The permissions a graph.facebook.com token was granted (``/me/permissions``), or []."""
    try:
        data = _get(http, f"{base}/me/permissions", {}, access_token, FACEBOOK)
    except (AuthError, InstagramAPIError) as exc:
        logger.debug("me/permissions: %s", exc)
        return []
    return [
        str(item["permission"])
        for item in data.get("data") or []
        if isinstance(item, dict) and item.get("permission") and item.get("status") == "granted"
    ]


def _connect(
    raw: str, graph: str, args: argparse.Namespace, settings: Settings, http: httpx.Client
) -> tuple[StoredToken, dict[str, Any]]:
    now = utcnow()
    if graph == INSTAGRAM:
        token = StoredToken(
            access_token=raw,
            obtained_at=now,
            expires_at=now + LONG_LIVED_LIFETIME,
            expires_at_estimated=True,
        )
        return token, probe_token(http, settings, raw)
    base = f"{GRAPH_HOSTS[FACEBOOK]}/{settings.api_version}"
    account_id = choose_account(find_instagram_accounts(http, base, raw), args.account)
    expires_at, unknown, permissions = token_details(http, base, raw)
    token = StoredToken(
        access_token=raw,
        obtained_at=now,
        expires_at=expires_at,
        expires_at_estimated=unknown,
        user_id=account_id,
        permissions=permissions,
        graph=FACEBOOK,
    )
    return token, probe_token(http, settings, raw, graph=FACEBOOK, node=account_id)


def _pasted_token(
    raw: str, args: argparse.Namespace, settings: Settings, http: httpx.Client
) -> tuple[StoredToken, dict[str, Any]]:
    detected = detect_graph(raw)
    candidates = [detected] if detected else [INSTAGRAM, FACEBOOK]
    for graph in candidates[:-1]:
        try:
            return _connect(raw, graph, args, settings, http)
        except AuthError as exc:
            logger.debug("Not a %s token: %s", graph, exc)
    return _connect(raw, candidates[-1], args, settings, http)


def _read_token(args: argparse.Namespace) -> str:
    if args.token_stdin:
        return sys.stdin.read().strip()
    print("In the Meta App Dashboard: your app > Use cases > Customize > API setup with unified login")
    print("> 'Generate access tokens' > Generate token (next to the Instagram account). Copy it.")
    return getpass.getpass("Paste the access token (it won't be shown): ").strip()


def _oauth_token(args: argparse.Namespace, settings: Settings, http: httpx.Client) -> StoredToken:
    app_id = settings.app_id or input("Instagram app ID: ").strip()
    app_secret = settings.app_secret or getpass.getpass("Instagram app secret (it won't be shown): ").strip()
    if not app_id or not app_secret:
        raise AuthError("Both the Instagram app ID and app secret are needed for --oauth.")
    redirect_uri = args.redirect_uri or settings.redirect_uri or DEFAULT_REDIRECT_URI
    state = secrets.token_urlsafe(16)
    url = authorize_url(app_id, redirect_uri, state)
    print(f"Opening Instagram sign-in. Requested permissions: {', '.join(SCOPES)}.")
    print(f"If your browser does not open, visit:\n\n  {url}\n")
    if not args.no_browser:
        try:
            webbrowser.open(url, new=1, autoraise=True)
        except Exception:  # pragma: no cover - depends on the desktop
            logger.info("Could not open a browser", exc_info=True)
    print(f"After you approve, the browser goes to {redirect_uri} (the page may fail to load: that's fine).")
    pasted = input("Paste the full address from the browser's address bar: ")
    code = code_from_redirect(pasted, state)
    short_token, user_id, permissions = exchange_code(
        http, app_id=app_id, app_secret=app_secret, redirect_uri=redirect_uri, code=code
    )
    token = exchange_for_long_lived(http, short_token, app_secret, utcnow())
    token.user_id, token.permissions, token.source = user_id, permissions, "oauth"
    return token


def cmd_auth(args: argparse.Namespace) -> int:
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    settings = _settings(args)
    with httpx.Client(headers={"User-Agent": "instagram-mcp"}) as http:
        if args.oauth:
            token = _oauth_token(args, settings, http)
            me = probe_token(http, settings, token.access_token)
        else:
            raw = _read_token(args)
            if not raw:
                print("No token given.", file=sys.stderr)
                return 1
            token, me = _pasted_token(raw, args, settings, http)
    token.username = me.get("username")
    token.user_id = token.user_id or (str(me["user_id"]) if me.get("user_id") else None)
    TokenStore(settings.token_file).save(token)
    kind = me.get("account_type") or (
        "unknown account type" if token.graph == INSTAGRAM else "unified login, graph.facebook.com"
    )
    print(f"\nSigned in as @{token.username} ({kind}).")
    print(f"Profile '{settings.profile}', token saved to {settings.token_file}")
    print(_expiry_note(token))
    if me.get("account_type") and str(me["account_type"]).upper() not in PROFESSIONAL_ACCOUNT_TYPES:
        print("\nWarning: this is not a professional (creator or business) account; insights won't work.")
    if me.get("_insights_denied"):
        insights = PERMISSIONS_BY_GRAPH[token.graph][1]
        print(f"\nWarning: insights are not readable with this token: {me['_insights_error']}")
        if token.permissions and insights not in token.permissions:
            print(f"Meta issued it without {insights}. It has: {', '.join(token.permissions)}.")
        if token.graph == INSTAGRAM:
            print(f"Generate the token again with the {insights} permission ticked.")
        else:
            print(
                f"Add {insights} to the app's login configuration (API setup with unified login > "
                "configuration > Edit permissions, or Facebook Login for Business > Configurations), "
                "then generate the token again. Adding it under 'Permissions and features' alone "
                "doesn't put it in the token."
            )
        return 1
    if me.get("_insights_error"):
        print(f"\nNote: a test call to insights failed ({me['_insights_error']}). The token is saved;")
        print("run `instagram-mcp check` to see which sections work.")
    print("\nYou're all set. Try: instagram-mcp check, or instagram-mcp report --out instagram-report.json")
    return 0


def _expiry_note(token: StoredToken) -> str:
    if token.refreshable and token.expires_at:
        about = "about " if token.expires_at_estimated else ""
        return (
            f"Valid until {about}{token.expires_at.date().isoformat()}; it is renewed automatically "
            "whenever the server or `instagram-mcp report` runs (at least once every 60 days)."
        )
    renew = f"generate a new one in the Meta App Dashboard and run `{AUTH_COMMAND}` again."
    if token.expires_at:
        return (
            f"Valid until {token.expires_at.date().isoformat()}. It can't renew itself: before then, {renew}"
        )
    if token.expires_at_estimated:
        return f"Meta didn't say when this token expires. If it stops working, {renew}"
    return f"Meta says this token never expires. If it stops working (e.g. access was removed), {renew}"


def _output_path(out: str, settings: Settings) -> Path:
    """``--out`` relative to the current directory; must be .json and outside the token folder."""
    path = Path(out).expanduser().resolve()
    if path.suffix.lower() != ".json":
        raise ValueError(f"--out must be a .json file (got {path.name}).")
    config_dir = settings.config_dir.resolve()
    if path == config_dir or config_dir in path.parents:
        raise ValueError("Refusing to write inside the token directory; choose another location.")
    return path


def cmd_report(args: argparse.Namespace) -> int:
    from instagram_mcp.context import AppContext
    from instagram_mcp.instagram import GraphInstagramClient
    from instagram_mcp.tools.report import build_report

    settings = _settings(args)
    path = None if args.out == "-" else _output_path(args.out, settings)  # checked before any API call
    ctx = AppContext(settings=settings, api=GraphInstagramClient(settings))
    rng = ctx.resolve(args.period, args.start_date, args.end_date)
    report = build_report(ctx, rng, include_thumbnails=args.thumbnails)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if path is None:
        print(text, flush=True)
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    dates = report["date_range"]
    print(
        f"Saved the report for {report['profile'].get('handle')} to {path}: {len(report['posts'])} posts "
        f"from {dates['start_date']} to {dates['end_date']}, {path.stat().st_size // 1024} KB.",
        flush=True,
    )
    if report["unavailable"]:
        print("Unavailable sections:")
        for key, reason in sorted(report["unavailable"].items()):
            print(f"  - {key}: {reason}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from instagram_mcp.instagram import GraphInstagramClient
    from instagram_mcp.tools.account import auth_status

    settings = _settings(args)
    info = {"version": __version__, "api_version": settings.api_version}
    info.update(auth_status(GraphInstagramClient(settings), settings))
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0 if info.get("signed_in") else 1


def cmd_refresh(args: argparse.Namespace) -> int:
    settings = _settings(args)
    with httpx.Client(headers={"User-Agent": "instagram-mcp"}) as http:
        token = TokenManager(settings, http).refresh()
    until = token.expires_at.date().isoformat() if token.expires_at else "no set date"
    print(f"Refreshed. The token for @{token.username} is now valid until {until}.")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if TokenStore(settings.token_file).delete():
        print(f"Deleted the stored token for profile '{settings.profile}'.")
    else:
        print(f"No token stored for profile '{settings.profile}'.")
    print(
        "To revoke access completely, remove the app in Instagram: Settings > Website permissions > "
        "Apps and websites (or reset the token in the Meta App Dashboard)."
    )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    import anyio

    from instagram_mcp.selfcheck import run_checks

    return anyio.run(run_checks, _settings(args))


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="instagram-mcp",
        description="Read-only MCP server for Instagram analytics. Without a command it serves over stdio.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging to stderr.")
    # Shared by every command, so `instagram-mcp -v` / `--profile X` also work without one
    # (they then go to `serve`). SUPPRESS keeps a top-level -v from being reset by the subcommand.
    profile = argparse.ArgumentParser(add_help=False)
    profile.add_argument(
        "--profile", help="Named account profile (default: 'default' or INSTAGRAM_MCP_PROFILE)."
    )
    profile.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="Debug logging to stderr."
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    serve = sub.add_parser("serve", parents=[profile], help="Run the MCP server over stdio (default).")
    serve.set_defaults(func=cmd_serve)

    auth = sub.add_parser(
        "auth", parents=[profile], help="Save an Instagram access token (pasted from the Meta App Dashboard)."
    )
    auth.add_argument(
        "--token-stdin", action="store_true", help="Read the token from stdin instead of a prompt."
    )
    auth.add_argument(
        "--account",
        help="When the token can read several Instagram accounts (or Meta doesn't list them): "
        "the username or account ID to use.",
    )
    auth.add_argument(
        "--oauth",
        action="store_true",
        help="Apps with the older 'API setup with Instagram login' only: sign in through the browser "
        "with the Instagram app ID/secret instead of pasting a dashboard token.",
    )
    auth.add_argument(
        "--redirect-uri",
        help=f"With --oauth: a redirect URI registered in the app (default {DEFAULT_REDIRECT_URI}).",
    )
    auth.add_argument("--no-browser", action="store_true", help="With --oauth: print the URL only.")
    auth.set_defaults(func=cmd_auth)

    report = sub.add_parser(
        "report", parents=[profile], help="Write a full report (the get_instagram_report tool) as JSON."
    )
    report.add_argument(
        "--out", default="instagram-report.json", help="Output .json path, or '-' for stdout."
    )
    report.add_argument(
        "--period", default="last_28_days", help="Named period, e.g. last_28_days, last_month."
    )
    report.add_argument("--start-date", help="Custom range start, YYYY-MM-DD (overrides --period).")
    report.add_argument("--end-date", help="Custom range end, YYYY-MM-DD (default: yesterday).")
    report.add_argument("--thumbnails", action="store_true", help="Embed each post's image (~5 KB per post).")
    report.set_defaults(func=cmd_report)

    status = sub.add_parser("status", parents=[profile], help="Show the account, token expiry and settings.")
    status.set_defaults(func=cmd_status)

    refresh = sub.add_parser(
        "refresh", parents=[profile], help="Renew an Instagram Login token now for another 60 days."
    )
    refresh.set_defaults(func=cmd_refresh)

    logout = sub.add_parser("logout", parents=[profile], help="Delete the stored token.")
    logout.set_defaults(func=cmd_logout)

    check = sub.add_parser("check", parents=[profile], help="Call every tool against your account.")
    check.set_defaults(func=cmd_check)
    return parser


COMMANDS = {"serve", "auth", "report", "status", "refresh", "logout", "check"}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not COMMANDS & set(arguments) and not {"-h", "--help", "--version"} & set(arguments):
        arguments = ["serve", *arguments]  # no command: run the server (what MCP clients launch)
    args = build_parser().parse_args(arguments)
    _configure_logging(args.verbose)
    try:
        return int(args.func(args))
    except (AuthError, InstagramAPIError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
