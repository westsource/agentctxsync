# Agent Context Sync

> **简体中文**: [README.zh-CN.md](README.zh-CN.md) · **English**: this document

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/westsource/agentctxsync/actions/workflows/ci.yml/badge.svg)](https://github.com/westsource/agentctxsync/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](mcp/)

> Official website: https://www.agentctxsync.com

**Continue exactly where you left off — on any device, with any AI agent.**

Agent Context Sync keeps Hermes, DeepSeek Harness, opencode, Reasonix, OpenClaw, WorkBuddy, Oh My Pi and every machine you work on in sync through one shared conversation pool — no more copy-pasting history. Start a conversation on one device and continue it on another, or hand it from one agent to the next. Every conversation is backed up to a server you control, so your history stays safe even if a device is lost or damaged.

## The problems it solves

- **Context breaks on every switch.** Changing device or agent means copy-pasting history and re-explaining background. Agent Context Sync keeps the whole session pool available and automatically in sync — sit down and pick up where you left off.
- **Local data can be lost.** One crash, one accidental deletion, one dead drive — years of conversations can vanish. Every session is synced to a server you control, so whatever goes wrong locally, your history is always recoverable.
- **Main threads and subtasks get tangled.** Delegated sub-agent conversations tend to drown the main thread. Agent Context Sync folds them into the parent session with a badge, keeping the main line clean and every branch traceable.
- **Sharing shouldn't mean exposing everything.** Profiles, projects and devices each keep their own place and sync across machines; a team shares one pool, yet admins handle only users, invites and workspaces — never able to read anyone's sessions.

## Value

Two things this tool buys you — and builds toward a third in the summary below.

### 1. Seamless continuity — any device, any agent

Your conversations live in a single shared pool, so the session you started on one device is already waiting when you sit down at another — no matter which agent it belongs to.

- **Any device**: a new device pulls your complete history on first pairing — no copy-pasting, no re-explaining background
- **Any agent**: Hermes, DeepSeek Harness, opencode, Reasonix, OpenClaw, WorkBuddy, and Oh My Pi all read and write the same pool, so you can hand a thread from one agent to another
- **Wherever it lives**: sub-agents fold into their parent session (with a badge), and profiles + projects sync across machines without contamination

### 2. Server-side backup — your conversations can't be lost

Every session is pushed to your self-hosted server, so a single-machine crash, a cleared profile, or a corrupted local store can never take your history with it.

- **Self-hosted and yours**: data lives on a server you control, not a third-party cloud
- **Survives anything local**: even if a device or profile is wiped, the full history is recoverable from the server
- **Export, import, restore**: one-click export (Markdown / JSON.gz) and import, plus a soft-delete trash so even mistakes are reversible

### How it changes your day-to-day

| Without Agent Context Sync | With Agent Context Sync |
|------|------|
| Every agent and every device is its own silo | All agents + devices share one auto-synced session pool |
| Switching devices means copying and pasting history | New device pulls your history automatically on first pairing |
| A lost or wiped device means lost conversations | History is backed up on the server and always recoverable |
| Sub-agent threads drown the main conversation | Sub-agents fold into the parent session with a badge |
| Profiles / projects fragmented across machines | Single client covers all profiles; sessions and projects sync with zero server changes |
| Teammates sharing = everyone sees everything | Tenant isolation: admins manage infrastructure, can't read your data |

## Supported Agents

Covers the AI agents you actually use — **Hermes, DeepSeek Harness, opencode, Reasonix, OpenClaw, WorkBuddy, Oh My Pi**.

Each Agent deploys its own client and points at the same Workspace, and they sync with each other. The per-agent storage layout, canonical ids and write constraints are in [docs/SUPPORTED_AGENTS.md](docs/SUPPORTED_AGENTS.md); adding a new Agent only requires implementing one adapter ([docs/ADDING_AGENT.md](docs/ADDING_AGENT.md)) — zero server or sync-engine changes.

## Screenshots

![Dashboard — workspace overview, sync status, quota and recent sessions](docs/screenshots/02-dashboard.png)

![All sessions — unified cross-workspace list with search and filters](docs/screenshots/03-all-sessions.png)

> **Global search** — cross-workspace full-text search over session titles and message content, scoped to your own workspaces (admins included), with deep-links that jump to the exact message ([docs/SEARCH.md](docs/SEARCH.md)).

![Workspace detail — session list, projects and sync devices](docs/screenshots/04-workspace.png)

![Session viewer — Markdown rendering with code blocks](docs/screenshots/05-session-viewer.png)

## Quick Start

> All `<SERVER_IP>` below are placeholders — replace them with the address of your actual deployment.

### 1. Deploy the server

```bash
# Upload to the target server and run
scp -r server/ scripts/ root@<SERVER_IP>:/tmp/hermes-sync/
ssh root@<SERVER_IP>
cd /tmp/hermes-sync/server
bash ../scripts/deploy-server.sh
```

After deployment:
- API: `http://<SERVER_IP>:8765/health`
- Web UI: `http://<SERVER_IP>:8765/web/`
- On first start, a default admin `admin` is created automatically (random password printed in the server logs; **forced change on first login**) along with its default workspace (including the API Key — see the server logs)

> More detailed server deployment, operations, and backup instructions: [docs/server-deployment.md](docs/server-deployment.md).

### 2. Register a user and create a Workspace (Web UI)

1. Open `http://<SERVER_IP>:8765/web/` and click Register — registration is open by default (self-hosted math CAPTCHA, invite code optional)
2. A "Default Workspace" is created automatically after successful registration; create more with "+ Create" on the overview page
3. Copy the API Key from the Workspace detail page (format `ws_xxx`)

### 3. Install the MCP client (per agent)

**Method A (recommended)**: log in to the Web UI → Setup Help (`/web/help`) → download the archive for your Agent. Unpack and register it following the install instructions inside — replace `<YOUR_API_KEY>` with the workspace API Key on the help page (the package no longer pre-fills the Key, so forwarding it leaks nothing). Restart the Agent when done.

**Method B (manual)**:

```bash
# Choose an agent (hermes | deepseek-harness | opencode | reasonix | openclaw | workbuddy | omp), default hermes
export HERMES_SYNC_AGENT=deepseek-harness

# Set the workspace API key (format ws_xxx)
export HERMES_SYNC_API_KEY=ws_yourkeyhere

# One-click deploy (note: the script's default server address is a placeholder — set HERMES_SYNC_SERVER to your actual deployment)
bash scripts/deploy-local-mcp.sh
```

Each Agent deploys its own instance (one `HERMES_SYNC_AGENT` value + independent lock files); point them all at the same Workspace API Key and they sync with each other. Client behavior: one incremental pull ~8s after startup (pushing local data as a bootstrap on first pairing), then auto-sync every 300 seconds (`HERMES_SYNC_INTERVAL`).

## Sync Tools

| Tool | Description |
|------|------|
| `sync_status` (alias `hermes_sync_status`) | View sync status (remote session/message counts, per-device last sync time) |
| `sync_pull` (alias `hermes_sync_pull`) | Pull sessions from remote to local (`limit`, default 50; `full` ignores the watermark) |
| `sync_push` (alias `hermes_sync_push`) | Push local sessions to remote (auto-batching avoids large-request timeouts) |
| `sync_full` (alias `hermes_sync_full`) | Full sync (push first, then pull) |
| `project_push` | Push projects from all local profiles' projects.db to remote (same-name merges handled by the server) |
| `project_pull` | Pull projects from remote into the local projects.db (applies remap, routes per profile) |

`sync_*` are neutral names common to all Agents; `hermes_sync_*` are compatibility aliases. Background engine details: [docs/OPERATIONS.md](docs/OPERATIONS.md#sync-tools-background-behavior).

## Learn More

- [docs/OPERATIONS.md](docs/OPERATIONS.md) — quota, client auto-update, multi-profile & project sync, data retention, server migration, SQLite-lock compatibility, troubleshooting
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — server & local environment variable reference
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system architecture, multi-tenancy model, database schema, API reference
- [docs/server-deployment.md](docs/server-deployment.md) — deployment, operations, backup, quota SQL
- [docs/SUPPORTED_AGENTS.md](docs/SUPPORTED_AGENTS.md) — per-agent storage, ids and write constraints
- [docs/ADDING_AGENT.md](docs/ADDING_AGENT.md) — adding a new Agent adapter
- [docs/SEARCH.md](docs/SEARCH.md) — global search (cross-workspace, tenant-isolated, message deep-link)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — how to set up a dev environment, code style, i18n rules, and the PR process.

## License

[MIT](LICENSE) © 2026 道荣（黄超）、露（张渊） · [中文版](LICENSE.zh-CN.md)