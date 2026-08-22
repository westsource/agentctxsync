# Contributing to Agent Context Sync

Thanks for taking the time to contribute! This project syncs AI agent sessions
(Hermes, DeepSeek Harness, opencode, Reasonix, OpenClaw) across devices and agents,
and every contribution makes it better.

## Code of conduct

Be respectful and constructive. This is a small, friendly project — treat
others the way you want to be treated.

## Project layout

```
server/                  FastAPI backend: Web UI, REST/Sync APIs, i18n
  ├─ server.py           main application (tables are auto-migrated at startup)
  ├─ translations.py     zh-CN / en translation dictionaries
  ├─ agents.py           agent registry used by the help/download pages
  ├─ templates/          Jinja2 templates (Tailwind classes, CSS variables)
  └─ static/             vendored assets (tailwind.js, alpine.min.js, favicon)
mcp/                     MCP client (distributed to agents, auto-updating)
  ├─ server.py           stdio MCP server: sync_status/pull/push/full tools
  ├─ updater.py          client auto-update engine (SHA256-verified)
  ├─ adapters/           per-agent local-store adapters (base + 5 agents)
  └─ tests/              pytest suite for adapters and updater
scripts/                 deploy / migration / e2e helpers
docs/                    ADDING_AGENT.md, server-deployment.md
```

## Development setup

Prerequisites: Python 3.11+, PostgreSQL (local instance is fine).

```bash
python -m venv .venv
.venv/Scripts/python -m pip install fastapi uvicorn psycopg2-binary jinja2 markdown python-multipart

# create a database and set required env vars
export HERMES_SYNC_PG_DSN=postgresql://user:pass@localhost:5432/agentctxsync
export HERMES_SYNC_MASTER_KEY=change-me
export HERMES_SYNC_JWT_SECRET=$(openssl rand -hex 32)

.venv/Scripts/python server/server.py    # serves http://127.0.0.1:8765
```

The server auto-creates its schema on startup (idempotent `ALTER TABLE ...
ADD COLUMN IF NOT EXISTS` migrations — keep new columns in that pattern so
existing deployments upgrade in place).

## Making changes

- Keep changes focused; one logical change per PR.
- Python: simple, boring code over clever abstractions. PEP 8, no unused
  imports, no dead code.
- Templates: reuse existing Tailwind classes and the CSS variables in
  `landing.html`/`base.html`; do not introduce a second styling convention.
- i18n: every new user-facing string needs **both** a `zh-CN` and an `en`
  key in `server/translations.py`. Keys must stay paired; the UI is bilingual.
- Client changes: if the MCP client code changes, bump `CLIENT_VERSION` in
  **both** `server/server.py` and `mcp/server.py` so connected agents
  auto-update after the server deploy.

## Adding a new agent

Implement an adapter in `mcp/adapters/` (copy `_template.py`), register it in
`mcp/adapters/__init__.py`, and document the store layout in
[docs/ADDING_AGENT.md](docs/ADDING_AGENT.md). The server and sync engine need
zero changes — IDs, prefixes and write constraints are adapter concerns.

## Testing

```bash
.venv/Scripts/python -m pytest mcp/tests -q
```

Server-side: no unit suite yet; verify with a smoke run (`/health`, log in,
create a workspace) against a local PostgreSQL before opening a PR.

## Commits

- Imperative summary line under 72 chars (`feat:`, `fix:`, `docs:`, `refactor:`).
- Body explains **what** changed and **why** (not how).
- Never commit secrets: `.env*`, real passwords, API keys. The repo is public.

## Push rules

- **`output/` must never be committed or pushed to any remote.** It holds
  local promo articles, screenshots and generation scripts (gitignored).
  Before pushing, check `git status` for `output/` files; do not rely on
  `git add -A` to skip them.
- **Production deployment information (server addresses, credentials, API
  keys, DSNs, systemd/nginx specifics) must never enter this repository**
  (see the local `AGENTS.md` for the full list). Use RFC 5737 test
  addresses and placeholders in code/docs.
- Before any push: `git status` + `git diff --stat` — review exactly what
  is staged.

## Pull requests

1. Fork and branch (`main`).
2. Make your change, run the tests, keep the diff small.
3. Open the PR with a description of the problem and the approach.
4. A maintainer will review; expect a round of feedback. Small, well-scoped
   PRs merge fastest.

## Reporting issues

Include: reproduction steps, expected vs actual behavior, server version
(git hash or `CLIENT_VERSION`), client agent and its version, and any relevant
log lines (`mcp-stderr.log`, server journal).
