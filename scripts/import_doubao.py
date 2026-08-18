#!/usr/bin/env python3
"""
Doubao (豆包) session importer for Agent Context Sync.

Doubao stores sessions in the cloud (doubao.com / App / Electron desktop),
with NO stable local session store comparable to codex / hermes / workbuddy,
and no official API to pull personal chat history. So it can't be a normal
local-store adapter. Instead this script turns *exported* Doubao content into
canonical sessions (``doubao:`` prefix, ``agent_type="doubao"``) and pushes
them straight to the sync server via POST /push. Once in the pool, the session
is visible in the Web UI and is pulled into every connected agent's local
store -- a one-way "Doubao -> shared pool" import.

Get Doubao data any of these ways:
  * Web  (doubao.com): a批量导出 browser script/extension gives a JSON array
        [{"role":"user"|"assistant","content":"...","timestamp":"..."}]
        (best fidelity), or markdown/text.
  * App: "导出文件" -> Word/PDF, then copy text into a .md/.txt file.
  * Manual: copy the conversation and paste it (--paste).

Usage:
  python scripts/import_doubao.py --file out.json [--title "标题"] [--model x]
  python scripts/import_doubao.py --file chat.md
  python scripts/import_doubao.py --paste < chat.txt
  python scripts/import_doubao.py --file chat.json --api-key ws_xxx \
        --server http://localhost:8765 --device-id my-pc

Auth/defaults come from env:
  HERMES_SYNC_SERVER  (default http://localhost:8765)
  HERMES_SYNC_API_KEY (required unless --api-key)
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# Alias the agent name so messages must not collide with other agents.
AGENT = "doubao"


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------
def to_epoch(ts) -> float | None:
    """Accept epoch (int/float/str) or '%Y-%m-%d %H:%M:%S' / ISO -> epoch secs."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip()
    if not s:
        return None
    if s.replace(".", "", 1).isdigit():
        return float(s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt).timestamp()
        except ValueError:
            continue
    # ISO with 'T' and optional timezone offset
    try:
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Parsers: input source -> list[(role, content, timestamp)]
# ---------------------------------------------------------------------------
def _norm_role(role: str) -> str | None:
    r = (role or "").strip().lower()
    if r in ("user", "我", "用户", "me", "human"):
        return "user"
    if r in ("assistant", "助手", "豆包", "ai", "bot", "ai助手"):
        return "assistant"
    if r in ("system", "系统"):
        return "system"
    return None


def parse_json(text: str) -> list[tuple | None]:
    """Parse Doubao web-export JSON.

    Accepts a top-level array of {role, content, timestamp} or a list of
    sessions {title, messages:[...]}. Returns (triple|None) per message.
    """
    data = json.loads(text)
    out = []
    if isinstance(data, dict):
        data = data.get("messages") or data.get("data") or []
    for item in data:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("messages"), list):  # nested session wrapper
            for m in item["messages"]:
                if isinstance(m, dict):
                    out.append(_triple(m))
            continue
        out.append(_triple(item))
    return out


def _triple(m: dict):
    role = _norm_role(m.get("role") or m.get("sender") or "")
    content = m.get("content") or m.get("text") or ""
    if isinstance(content, list):  # some exports nest [{type, text}]
        content = "\n".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("text"))
    if not role or not isinstance(content, str) or not content.strip():
        return None
    return (role, content.strip(), to_epoch(m.get("timestamp")))


def parse_markdown(text: str) -> list[tuple | None]:
    """Parse markdown like:

        ## 用户 - 2025-06-15 14:30:22
        内容...
        ## 助手 - 2025-06-15 14:30:25
        内容...
    """
    out = []
    cur_role, cur_ts = None, None
    body: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^\s*#{1,3}\s+(用户|助手|我|豆包|ai|ai助手|system|系统|user|assistant)\s*[-–—]?\s*(.*)$",
                     line, re.I)
        if m:
            if cur_role and body:
                r = _norm_role(cur_role)
                if r:
                    out.append((r, "\n".join(body).strip(), cur_ts))
                body = []
            cur_role = m.group(1)
            ts = to_epoch(m.group(2).strip())
            cur_ts = ts if ts is not None else cur_ts
            body = []
        else:
            body.append(line)
    if cur_role and body:
        r = _norm_role(cur_role)
        if r:
            out.append((r, "\n".join(body).strip(), cur_ts))
    return out


def parse_text(text: str) -> list[tuple | None]:
    """Heuristic for plain copied chat: line-wise, a line starting with a
    role label opens a new message, subsequent lines append to it. Best
    effort; a single unlabeled blob is kept as one user turn."""
    out = []
    cur_role, body = None, []
    for line in text.splitlines():
        m = re.match(r"^(用户|我|助手|豆包|ai|system|系统)[:：]\s*(.*)$",
                     line, re.I)
        if m:
            r = _norm_role(m.group(1))
            if cur_role and body:
                out.append((cur_role, "\n".join(body).strip(), None))
            cur_role = r
            body = [m.group(2)] if m.group(2) else []
        elif cur_role:
            body.append(line)
    if cur_role and body:
        out.append((cur_role, "\n".join(body).strip(), None))
    if not out and text.strip():
        out.append(("user", text.strip(), None))
    return out


def detect(ext: str, text: str):
    # JSON is detected first regardless of extension: paste via --paste and
    # anything starting with [ or { that loads is a Doubao JSON export.
    if ext in (".json",) or text.lstrip().startswith(("[", "{")):
        try:
            return parse_json(text)
        except json.JSONDecodeError:
            if ext in (".json",):
                raise SystemExit("Could not parse file as JSON. Is it a Doubao JSON export?")
            # not JSON: fall through to text heuristics
    if (ext in (".md", ".markdown")
            or re.search(r"^\s*#{1,3}\s*(用户|助手|我|豆包|ai|system|系统)"
                         r"\s*[-–—]?\s*\d", text, re.I | re.M)):
        return parse_markdown(text)
    return parse_text(text)


# ---------------------------------------------------------------------------
# Canonical session build
# ---------------------------------------------------------------------------
def build_session(triples: list, title: str | None, model: str | None) -> dict:
    """triples: (role, content, timestamp). Produce one canonical session dict.

    A stable, deterministic id is derived from title + earlier timestamps so
    re-importing the same file is idempotent on the server (session upsert by
    canonical id + message dedupe by (session_id, role, timestamp)).
    """
    started = next((t for _, _, t in triples if t), time.time())
    seed = (title or (triples[0][1] if triples else "") or str(started)).strip()
    slug = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    sid = f"{AGENT}:{slug}"
    messages = []
    for role, content, ts in triples:
        if not ts:
            ts = started + len(messages) * 1.0  # sequential fallback
        messages.append({
            "session_id": sid,
            "role": role,
            "content": content,
            "timestamp": float(ts),
        })
    s = {
        "id": sid,
        "started_at": float(started),
        "agent_type": AGENT,
        "title": title or (triples[0][1][:40] if triples else "Doubao import"),
        "message_count": len(messages),
        "messages": messages,
    }
    if model:
        s["model"] = model
    return s


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------
def push(server: str, api_key: str, device_id: str, sessions: list[dict]) -> dict:
    url = f"{server}/push"
    body = json.dumps({"device_id": device_id, "sessions": sessions}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "User-Agent": "doubao-importer/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()
        except Exception:
            pass
        raise SystemExit(f"Push failed (HTTP {e.code}): {detail}")
    except Exception as e:
        raise SystemExit(f"Push failed: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Import Doubao sessions into AgentCtxSync")
    ap.add_argument("--file", help="Path to a Doubao export (.json/.md/.txt)")
    ap.add_argument("--paste", action="store_true",
                    help="Read raw text/JSON from stdin")
    ap.add_argument("--title", help="Session title")
    ap.add_argument("--model", help="Model name to tag the session (e.g. Doubao-pro)")
    ap.add_argument("--server", default=os.environ.get("HERMES_SYNC_SERVER",
                                                       "http://localhost:8765"))
    ap.add_argument("--api-key", default=os.environ.get("HERMES_SYNC_API_KEY", ""))
    ap.add_argument("--device-id", default=f"doubao-{os.getlogin() or 'local'}")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse + build canonical session but do not push")
    args = ap.parse_args()

    if not args.file and not args.paste:
        ap.error("provide --file PATH or --paste (read from stdin)")

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
        ext = os.path.splitext(args.file)[1].lower()
    else:
        text = sys.stdin.read()
        ext = ".txt"

    triples = [t for t in detect(ext, text) if t]
    if not triples:
        raise SystemExit("No conversational messages parsed. Check the export format.")

    session = build_session(triples, args.title, args.model)
    n_user = sum(1 for t in triples if t[0] == "user")
    n_asst = sum(1 for t in triples if t[0] == "assistant")
    print(f"Parsed {len(triples)} messages ({n_user} user / {n_asst} assistant)")
    print(f"Canonical session id : {session['id']}")
    print(f"Title                : {session['title']}")

    if args.dry_run or not args.api_key:
        if not args.api_key:
            print("\nNo API key (set HERMES_SYNC_API_KEY or --api-key); dry-run only.")
        print(json.dumps(session, ensure_ascii=False, indent=2))
        return

    result = push(args.server, args.api_key, args.device_id, [session])
    print(f"\nPushed to {args.server}: {json.dumps(result, ensure_ascii=False)}")
    print("Session is now in the shared pool. Connected agents will pull it"
          " on their next sync.")


if __name__ == "__main__":
    main()
