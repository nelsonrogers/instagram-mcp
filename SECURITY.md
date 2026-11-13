# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately** through
[GitHub security advisories](https://github.com/nelsonrogers/instagram-mcp/security/advisories/new)
rather than public issues. You'll get an acknowledgement within a few days; fixes are released as
soon as practical and credited unless you prefer otherwise.

Only the latest release is supported.

## How the server protects your account

- **Local only.** The server talks to MCP clients over stdio and to Meta's APIs over HTTPS. It
  never opens a network port (the optional `--oauth` sign-in uses a paste-back redirect, not a
  local listener).
- **Your own Meta app and token.** No shared credentials; the token never leaves your machine.
- **Token storage.** The token is written atomically with `0600` permissions inside a `0700`
  directory (`~/.config/instagram-mcp`, or the folder set by `INSTAGRAM_MCP_HOME`).
  `instagram-mcp logout` deletes it. The app secret used by `--oauth` is read from the
  environment or a hidden prompt and never written to disk.
- **No token in logs or messages.** Tokens and secrets are redacted from Meta's error messages,
  HTTP request logging is kept at WARNING, and pagination follows cursors instead of Meta's `next`
  URLs, which embed the token.
- **Read-only.** The server only sends GET requests (profile fields, insights, media, comments,
  tags). Even if the token carries more permissions (Meta adds messaging and publishing to
  unified login apps by default), no write endpoint is ever called. Untick what you don't need in
  the app's login configuration.
- **Raw queries are sanitised.** `query_instagram_api` accepts relative Graph API paths only,
  refuses `access_token`, `appsecret_proof` and `method` parameters, and replaces Meta's paging
  URLs (which embed the token) with cursors.
- **Thumbnail downloads** only go to Meta's CDN (`*.cdninstagram.com`, `*.fbcdn.net`) over HTTPS,
  are capped at 10 MB, and never carry the token.
- **File output.** `save_to` and `instagram-mcp report --out` only write `.csv`/`.json` files and
  refuse to write inside the token directory.

## Prompt injection

Comments, captions of posts that tag you, and other accounts' public posts are written by other
people. They are returned to your agent as data and could contain instructions aimed at AI
agents. The server has no write tools, but review what your agent proposes before acting on it.
