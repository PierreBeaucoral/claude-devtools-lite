#!/usr/bin/env python3
"""
claude-devtools-lite — local, read-only inspector for Claude Code sessions.

Reads ~/.claude/projects/<slug>/*.jsonl transcripts (plus subagent transcripts
and project memory) and serves a single-page dashboard on localhost.

Zero dependencies: Python 3.9+ standard library only.
Never writes to ~/.claude — strictly read-only.

Usage:
    python3 server.py [--port 3456] [--root ~/.claude]
"""
import argparse
import base64
import json
import os
import re
import secrets
import select
import shutil
import signal
import struct
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# POSIX pseudo-terminals power the embedded terminal. macOS/Linux have them;
# Windows does not (ConPTY needs a third-party package), so there the
# dashboard runs fully except for the terminal pane, which reports why.
try:
    import fcntl
    import pty
    import termios
    HAS_PTY = True
except ImportError:                                   # Windows
    fcntl = pty = termios = None
    HAS_PTY = False

# Windows: ConPTY gives the same capability through kernel32 (ctypes only)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import winconpty  # noqa: E402

HAS_TERMINAL = HAS_PTY or winconpty.unsupported_reason() is None

HERE = Path(__file__).resolve().parent
CLAUDE_ROOT = Path(os.environ.get("CLAUDE_ROOT", str(Path.home() / ".claude")))

# --- private data dir ------------------------------------------------------
# Token and state live OUTSIDE the source folder: the source folder is a git
# repo, and one `git add -f` would publish the token permanently.
if sys.platform == "darwin":
    APP_DIR = Path.home() / "Library" / "Application Support" / "claude-devtools"
elif os.name == "nt":
    APP_DIR = Path(os.environ.get("APPDATA",
                                  Path.home() / "AppData" / "Roaming")) / "claude-devtools"
else:
    APP_DIR = Path(os.environ.get("XDG_CONFIG_HOME",
                                  Path.home() / ".config")) / "claude-devtools"
APP_DIR.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(APP_DIR, 0o700)
except OSError:
    pass

# --- auth token: protects every /api endpoint from other local users -------
# Persisted (0600) so bookmarks keep working across restarts. The app exchanges
# it for a same-site cookie at /launch; the UI also sends X-Devtools-Token.
TOKEN_FILE = APP_DIR / "token"
LEGACY_TOKEN_FILE = HERE / ".token"


def load_token():
    for f in (TOKEN_FILE, LEGACY_TOKEN_FILE):
        try:
            tok = f.read_text().strip()
        except OSError:
            continue
        if re.fullmatch(r"[a-f0-9]{32,64}", tok):
            if f is LEGACY_TOKEN_FILE:      # migrate out of the repo
                TOKEN_FILE.write_text(tok)
                os.chmod(TOKEN_FILE, 0o600)
                try:
                    LEGACY_TOKEN_FILE.unlink()
                except OSError:
                    pass
            return tok
    tok = secrets.token_hex(24)
    TOKEN_FILE.write_text(tok)
    os.chmod(TOKEN_FILE, 0o600)
    return tok


SERVER_TOKEN = None   # set in main()
# DNS-rebinding guard: loopback by default; main() adds an explicit --host
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

# --- small persistent state (usage baseline cache, layout) -----------------
STATE_FILE = APP_DIR / "state.json"
_LEGACY_STATE = HERE / ".state.json"
if not STATE_FILE.exists() and _LEGACY_STATE.exists():
    try:
        STATE_FILE.write_text(_LEGACY_STATE.read_text())
        _LEGACY_STATE.unlink()
    except OSError:
        pass
_state_lock = threading.Lock()


def state_read():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def state_write(patch):
    with _state_lock:
        st = state_read()
        st.update(patch)
        STATE_FILE.write_text(json.dumps(st))

MAX_RESULT_CHARS = 20_000     # per tool-result payload sent to the UI
MAX_TEXT_CHARS = 120_000      # per text/thinking block sent to the UI
SEARCH_MAX_RESULTS = 300
LIST_HEAD_BYTES = 512 * 1024  # how much of a big file to scan for list metadata
LIST_TAIL_BYTES = 256 * 1024

_meta_cache = {}              # path -> (mtime, size, meta dict)
_cache_lock = threading.Lock()


# ---------------------------------------------------------------- helpers

def projects_dir(root):
    return Path(root) / "projects"


def safe_project_path(root, slug):
    """Resolve a project slug to its directory, refusing path traversal."""
    if "/" in slug or "\\" in slug or slug.startswith("."):
        raise ValueError("bad project slug")
    p = projects_dir(root) / slug
    if not p.is_dir():
        raise FileNotFoundError(slug)
    return p


def iter_jsonl(path, max_bytes=None):
    """Yield parsed objects from a JSONL file, skipping malformed lines."""
    read = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            read += len(line)
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
            if max_bytes is not None and read > max_bytes:
                return


def tail_lines(path, n_bytes):
    """Return the last complete lines within n_bytes of the end of a file."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > n_bytes:
            f.seek(size - n_bytes)
            f.readline()  # drop the partial first line
        return [ln.decode("utf-8", "replace") for ln in f.read().splitlines()]


def truncate(s, limit):
    if s is None:
        return None
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n… [truncated, {len(s):,} chars total]"


def block_text(content):
    """Flatten a message content (str or list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "tool_result":
                    parts.append(block_text(b.get("content")))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(p for p in parts if p)
    return ""


# ---------------------------------------------------------------- session listing

def session_meta(path):
    """Cheap metadata for the session list: title, times, model, counts."""
    st = path.stat()
    key = str(path)
    with _cache_lock:
        hit = _meta_cache.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]

    meta = {
        "id": path.stem,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "title": None,
        "first_ts": None,
        "last_ts": None,
        "model": None,
        "version": None,
        "cwd": None,
        "gitBranch": None,
        "user_msgs": 0,
        "assistant_msgs": 0,
        "has_subagents": (path.parent / path.stem / "subagents").is_dir(),
    }

    small = st.st_size <= LIST_HEAD_BYTES + LIST_TAIL_BYTES

    def absorb(o):
        t = o.get("type")
        ts = o.get("timestamp")
        if ts:
            if meta["first_ts"] is None or ts < meta["first_ts"]:
                meta["first_ts"] = ts
            if meta["last_ts"] is None or ts > meta["last_ts"]:
                meta["last_ts"] = ts
        if t == "custom-title" and o.get("customTitle"):
            meta["title"] = o["customTitle"]
        elif t == "summary" and o.get("summary") and not meta["title"]:
            meta["title"] = o["summary"]
        elif t == "user" and not o.get("isSidechain"):
            meta["user_msgs"] += 1
            meta["cwd"] = meta["cwd"] or o.get("cwd")
            meta["version"] = meta["version"] or o.get("version")
            meta["gitBranch"] = meta["gitBranch"] or o.get("gitBranch")
            if meta["title"] is None:
                txt = block_text(o.get("message", {}).get("content"))
                if txt and not txt.startswith(("<local-command", "<command-name")):
                    meta["title"] = txt.strip()[:120]
        elif t == "assistant" and not o.get("isSidechain"):
            meta["assistant_msgs"] += 1
            m = o.get("message", {})
            meta["model"] = m.get("model") or meta["model"]

    if small:
        for o in iter_jsonl(path):
            absorb(o)
    else:
        for o in iter_jsonl(path, max_bytes=LIST_HEAD_BYTES):
            absorb(o)
        for ln in tail_lines(path, LIST_TAIL_BYTES):
            try:
                absorb(json.loads(ln))
            except json.JSONDecodeError:
                continue
        # counts are partial for big files; mark that
        meta["counts_partial"] = True

    if not meta["title"]:
        meta["title"] = "(untitled session)"
    with _cache_lock:
        _meta_cache[key] = (st.st_mtime, st.st_size, meta)
    return meta


def list_projects(root):
    out = []
    pdir = projects_dir(root)
    if not pdir.is_dir():
        return out
    for d in sorted(pdir.iterdir()):
        if not d.is_dir():
            continue
        sessions = list(d.glob("*.jsonl"))
        if not sessions and not (d / "memory").is_dir():
            continue
        newest = max((s.stat().st_mtime for s in sessions), default=d.stat().st_mtime)
        # authoritative cwd from the newest session, not the lossy slug
        cwd = None
        for s in sorted(sessions, key=lambda p: p.stat().st_mtime, reverse=True)[:3]:
            for o in iter_jsonl(s, max_bytes=64 * 1024):
                if o.get("cwd"):
                    cwd = o["cwd"]
                    break
            if cwd:
                break
        graph = None
        if cwd:
            g = Path(cwd) / "graphify-out" / "graph.html"
            try:
                if g.is_file():
                    graph = str(g)
            except OSError:
                pass
        out.append({
            "slug": d.name,
            "path": cwd or d.name.replace("-", "/"),
            "sessions": len(sessions),
            "mtime": newest,
            "has_memory": (d / "memory" / "MEMORY.md").is_file(),
            "graph": graph,
        })
    out.sort(key=lambda p: p["mtime"], reverse=True)
    return out


def project_cwd(root, slug):
    """Real working directory of a project, read from its newest session."""
    d = safe_project_path(root, slug)
    sessions = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for s in sessions[:5]:
        for o in iter_jsonl(s, max_bytes=64 * 1024):
            if o.get("cwd"):
                return o["cwd"]
    return None


# ---------------------------------------------------------------- session parsing

def summarize_tool_result(name, tur, block_content):
    """Produce a compact, human-useful string for a tool result."""
    if isinstance(tur, dict):
        if "stdout" in tur or "stderr" in tur:  # Bash
            parts = []
            if tur.get("stdout"):
                parts.append(tur["stdout"])
            if tur.get("stderr"):
                parts.append("[stderr]\n" + tur["stderr"])
            return "\n".join(parts) or "(no output)"
        if tur.get("type") == "text" and isinstance(tur.get("file"), dict):  # Read
            return tur["file"].get("content", "")
        if "oldString" in tur or "structuredPatch" in tur:  # Edit / Write
            return None  # rendered as a diff from the input side
        if "plan" in tur:
            return tur.get("plan")
        if "results" in tur:  # WebSearch etc.
            try:
                return json.dumps(tur["results"], indent=2, ensure_ascii=False)
            except (TypeError, ValueError):
                pass
        if "result" in tur and isinstance(tur["result"], str):  # WebFetch
            return tur["result"]
    txt = block_text(block_content)
    if txt:
        return txt
    if tur is not None:
        try:
            return json.dumps(tur, indent=2, ensure_ascii=False)[:MAX_RESULT_CHARS]
        except (TypeError, ValueError):
            return str(tur)
    return None


def parse_session(path, include_sidechain=False):
    """Parse a transcript into UI-ready timeline entries + usage series.

    Subagent transcripts mark every record isSidechain=true, so those are
    parsed with include_sidechain=True; main transcripts skip sidechain
    records (they live in their own files in recent Claude Code versions).
    """
    entries = []            # ordered timeline
    tool_index = {}         # tool_use_id -> entry
    usage_by_request = {}   # requestId -> usage (dedupe multi-block responses)
    context_series = []     # one point per API request
    seen_requests = set()
    title = None
    model = None
    sidechain_count = 0

    for o in iter_jsonl(path):
        t = o.get("type")
        ts = o.get("timestamp")

        if t == "custom-title" and o.get("customTitle"):
            title = o["customTitle"]
            continue
        if t == "summary" and o.get("summary"):
            title = title or o["summary"]
            continue
        if t in ("queue-operation", "last-prompt", "attachment", "file-history-snapshot"):
            continue
        if o.get("isSidechain") and not include_sidechain:
            sidechain_count += 1
            continue

        if t == "user":
            msg = o.get("message", {})
            content = msg.get("content")
            # tool results come back as user messages
            if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                        continue
                    entry = tool_index.get(b.get("tool_use_id"))
                    if entry is None:
                        continue
                    tur = o.get("toolUseResult")
                    if isinstance(tur, dict) and "structuredPatch" in tur:
                        entry["patch"] = tur.get("structuredPatch")
                        entry["file_path"] = tur.get("filePath")
                    res = summarize_tool_result(entry["name"], tur, b.get("content"))
                    entry["result"] = truncate(res, MAX_RESULT_CHARS)
                    entry["is_error"] = bool(b.get("is_error"))
                    if isinstance(tur, dict) and tur.get("agentId"):
                        entry["agent_id"] = tur.get("agentId")
                continue
            txt = block_text(content)
            if not txt:
                continue
            kind = "command" if txt.startswith(("<local-command", "<command-name")) else "user"
            sysrem = "<system-reminder>" in txt
            if title is None and kind == "user" and not sysrem:
                title = txt.strip()[:120]
            entries.append({"kind": kind, "ts": ts, "text": truncate(txt, MAX_TEXT_CHARS),
                            "system_reminder": sysrem, "uuid": o.get("uuid")})

        elif t == "assistant":
            msg = o.get("message", {})
            model = msg.get("model") or model
            rid = o.get("requestId")
            usage = msg.get("usage")
            if rid and usage and rid not in seen_requests:
                seen_requests.add(rid)
                usage_by_request[rid] = usage
                ctx = (usage.get("input_tokens", 0)
                       + usage.get("cache_read_input_tokens", 0)
                       + usage.get("cache_creation_input_tokens", 0))
                context_series.append({
                    "ts": ts, "context": ctx,
                    "output": usage.get("output_tokens", 0),
                    "cache_read": usage.get("cache_read_input_tokens", 0),
                    "cache_creation": usage.get("cache_creation_input_tokens", 0),
                    "input": usage.get("input_tokens", 0),
                })
            for b in msg.get("content", []) or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and b.get("text"):
                    entries.append({"kind": "assistant", "ts": ts,
                                    "text": truncate(b["text"], MAX_TEXT_CHARS)})
                elif bt == "thinking" and b.get("thinking"):
                    entries.append({"kind": "thinking", "ts": ts,
                                    "text": truncate(b["thinking"], MAX_TEXT_CHARS)})
                elif bt == "tool_use":
                    entry = {"kind": "tool", "ts": ts, "name": b.get("name", "?"),
                             "input": b.get("input", {}), "id": b.get("id"),
                             "result": None, "is_error": False}
                    try:  # keep giant inputs (Write content) bounded
                        raw = json.dumps(entry["input"], ensure_ascii=False)
                        if len(raw) > MAX_RESULT_CHARS:
                            entry["input_truncated"] = True
                            entry["input"] = {
                                k: (truncate(v, 4000) if isinstance(v, str) else v)
                                for k, v in entry["input"].items()}
                    except (TypeError, ValueError):
                        entry["input"] = {}
                    entries.append(entry)
                    if b.get("id"):
                        tool_index[b["id"]] = entry

        elif t == "system":
            txt = o.get("content") or o.get("text") or ""
            sub = o.get("subtype", "")
            if "compact" in sub or "compact" in str(txt)[:200].lower():
                entries.append({"kind": "compact", "ts": ts,
                                "text": "Context compaction boundary"})
            elif txt:
                entries.append({"kind": "system", "ts": ts,
                                "text": truncate(block_text(txt) or str(txt), 4000)})

    # mark probable compactions from context drops (>35% between requests)
    for i in range(1, len(context_series)):
        prev, cur = context_series[i - 1], context_series[i]
        if prev["context"] > 60_000 and cur["context"] < prev["context"] * 0.65:
            cur["compaction"] = True

    totals = {
        "requests": len(usage_by_request),
        "output_tokens": sum(u.get("output_tokens", 0) for u in usage_by_request.values()),
        "input_tokens": sum(u.get("input_tokens", 0) for u in usage_by_request.values()),
        "cache_read": sum(u.get("cache_read_input_tokens", 0) for u in usage_by_request.values()),
        "cache_creation": sum(u.get("cache_creation_input_tokens", 0)
                              for u in usage_by_request.values()),
        "peak_context": max((p["context"] for p in context_series), default=0),
    }

    # tool call histogram
    tool_counts = {}
    for e in entries:
        if e["kind"] == "tool":
            tool_counts[e["name"]] = tool_counts.get(e["name"], 0) + 1

    # subagent transcripts on disk
    subagents = []
    subdir = path.parent / path.stem / "subagents"
    if subdir.is_dir():
        for f in sorted(subdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
            subagents.append({"file": f.name, "size": f.stat().st_size,
                              "mtime": f.stat().st_mtime})

    return {
        "id": path.stem,
        "title": title,
        "model": model,
        "entries": entries,
        "context_series": context_series,
        "totals": totals,
        "tool_counts": tool_counts,
        "subagents": subagents,
        "sidechain_msgs": sidechain_count,
    }


# ---------------------------------------------------------------- search

def search_all(root, query, project=None, limit=SEARCH_MAX_RESULTS):
    q = query.lower()
    results = []
    pdir = projects_dir(root)
    dirs = [safe_project_path(root, project)] if project else \
        sorted((d for d in pdir.iterdir() if d.is_dir()),
               key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs:
        for f in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if q not in line.lower():
                            continue
                        try:
                            o = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if o.get("type") not in ("user", "assistant"):
                            continue
                        txt = block_text(o.get("message", {}).get("content"))
                        if not txt or q not in txt.lower():
                            continue
                        i = txt.lower().find(q)
                        lo, hi = max(0, i - 120), min(len(txt), i + len(q) + 160)
                        results.append({
                            "project": d.name,
                            "session": f.stem,
                            "type": o.get("type"),
                            "ts": o.get("timestamp"),
                            "snippet": ("…" if lo else "") + txt[lo:hi] + ("…" if hi < len(txt) else ""),
                        })
                        if len(results) >= limit:
                            return results
            except OSError:
                continue
    return results


# ---------------------------------------------------------------- memory

def read_memory(root, slug):
    d = safe_project_path(root, slug) / "memory"
    if not d.is_dir():
        return {"files": []}
    files = []
    for f in sorted(d.glob("*.md")):
        try:
            files.append({"name": f.name,
                          "content": truncate(f.read_text(encoding="utf-8", errors="replace"),
                                              MAX_TEXT_CHARS)})
        except OSError:
            continue
    files.sort(key=lambda x: (x["name"] != "MEMORY.md", x["name"]))
    return {"files": files}


# ---------------------------------------------------------------- usage aggregation
#
# Token usage across ALL projects, computed from the transcripts themselves.
# Claude limits work in ~5-hour blocks anchored to first activity, so we
# reconstruct the current block ccusage-style (new block after a >5h-from-
# block-start request, start floored to the hour).

from datetime import datetime, timezone

_usage_cache = {}  # path -> (mtime, size, [(epoch, out, model), ...])
BLOCK_HOURS = 5


def _ts_epoch(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def file_usage_records(path):
    st = path.stat()
    key = str(path)
    with _cache_lock:
        hit = _usage_cache.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
    recs, seen = [], set()
    for o in iter_jsonl(path):
        if o.get("type") != "assistant":
            continue
        rid = o.get("requestId")
        u = o.get("message", {}).get("usage")
        if not rid or not u or rid in seen:
            continue
        seen.add(rid)
        ep = _ts_epoch(o.get("timestamp"))
        if ep is None:
            continue
        recs.append((ep, u.get("output_tokens", 0),
                     o.get("message", {}).get("model") or "?"))
    with _cache_lock:
        _usage_cache[key] = (st.st_mtime, st.st_size, recs)
    return recs


def blocks_from_records(recs):
    """Group sorted (epoch, out, model) records into 5h blocks; return
    [(start, end, output_total), ...]."""
    blocks = []
    start = end = None
    total = 0
    for ep, out, _ in recs:
        if start is None or ep >= end:
            if start is not None:
                blocks.append((start, end, total))
            start = ep - (ep % 3600)
            end = start + BLOCK_HOURS * 3600
            total = 0
        total += out
    if start is not None:
        blocks.append((start, end, total))
    return blocks


def compute_baseline(root):
    """Scan ALL history once (background thread) for the Usage-Monitor-style
    baseline: max and P90 of output tokens per 5h block. Cached in .state.json
    keyed on the newest transcript mtime, and ratcheted up over time."""
    recs = []
    for f in projects_dir(root).rglob("*.jsonl"):
        try:
            recs.extend(file_usage_records(f))
        except OSError:
            continue
    recs.sort(key=lambda r: r[0])
    totals = sorted(b[2] for b in blocks_from_records(recs) if b[2] > 0)
    if not totals:
        return
    p90 = totals[max(0, int(len(totals) * 0.9) - 1)]
    prev = state_read()
    state_write({"baseline_max": max(totals[-1], prev.get("baseline_max", 0)),
                 "baseline_p90": max(p90, prev.get("baseline_p90", 0)),
                 "baseline_blocks": len(totals),
                 "baseline_at": time.time()})


def usage_summary(root):
    now = time.time()
    cutoff = now - 8 * 86400
    recs = []
    pdir = projects_dir(root)
    for f in pdir.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        recs.extend(r for r in file_usage_records(f) if r[0] >= cutoff)
    recs.sort(key=lambda r: r[0])

    # reconstruct 5h blocks over the last 8 days; keep the one containing `now`
    block_start = block_end = None
    for ep, _, _ in recs:
        if block_start is None or ep >= block_end:
            block_start = ep - (ep % 3600)          # floor to the hour
            block_end = block_start + BLOCK_HOURS * 3600
    block = None
    if block_start is not None and now < block_end:
        brecs = [r for r in recs if block_start <= r[0] < block_end]
        bymodel = {}
        for _, out, model in brecs:
            bymodel[model] = bymodel.get(model, 0) + out
        block = {"start": block_start, "end": block_end,
                 "output": sum(r[1] for r in brecs), "requests": len(brecs),
                 "by_model": bymodel}

    midnight = datetime.now().replace(hour=0, minute=0, second=0,
                                      microsecond=0).timestamp()
    today = [r for r in recs if r[0] >= midnight]
    week = [r for r in recs if r[0] >= now - 7 * 86400]
    wmodel = {}
    for _, out, model in week:
        wmodel[model] = wmodel.get(model, 0) + out
    hourly = [0] * 24                                # last 24h sparkline
    for ep, out, _ in recs:
        if ep >= now - 86400:
            hourly[int((ep - (now - 86400)) // 3600)] += out
    st = state_read()
    return {
        "block": block,
        "today": {"output": sum(r[1] for r in today), "requests": len(today)},
        "week": {"output": sum(r[1] for r in week), "requests": len(week)},
        "week_by_model": wmodel,
        "hourly": hourly,
        "baseline": {"max": st.get("baseline_max"), "p90": st.get("baseline_p90"),
                     "blocks": st.get("baseline_blocks")} if st.get("baseline_max") else None,
        "generated": now,
    }


# ---------------------------------------------------------------- viz inbox
#
# A watched folder Claude Code sessions can write into to "show" you output.
# Lives in this tool's own directory (never inside ~/.claude).

VIZ_DIR = HERE / "viz"
VIZ_TYPES = {".html": "text/html", ".htm": "text/html", ".svg": "image/svg+xml",
             ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf",
             ".md": "text/plain", ".txt": "text/plain", ".json": "text/plain",
             ".csv": "text/plain"}


FS_TEXT = {".r", ".py", ".jl", ".do", ".tex", ".bib", ".qmd", ".rmd", ".yml",
           ".yaml", ".toml", ".js", ".ts", ".sh", ".zsh", ".log", ".sql",
           ".org", ".ini", ".cfg", ".gitignore", ".env"}
SERVER_PORT = 3456  # overwritten in main()


def safe_home_path(raw):
    """Resolve a filesystem path, refusing anything outside $HOME."""
    home = Path.home().resolve()
    p = Path(raw or str(home)).expanduser().resolve()
    if p != home and home not in p.parents:
        raise ValueError("path outside home directory")
    return p


# Never serve credential-shaped files, even when their extension is otherwise
# previewable (.json/.yml/.toml/…). Deliberately over-broad: a blocked
# "tokens_analysis.json" is a smaller cost than a leaked API key.
SENSITIVE_SUBSTRINGS = ("credential", "secret", "password", "passwd", "apikey",
                        "api_key", "private_key", "privatekey", "token",
                        "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
SENSITIVE_EXACT = (".netrc", ".npmrc", ".pypirc", ".git-credentials",
                   ".htpasswd", "hosts.yml", "hosts.yaml", "auth.json",
                   "credentials", ".env")
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks",
                      ".asc", ".gpg", ".kdbx")


def is_sensitive(path):
    """True if this filename looks like it holds secrets."""
    name = Path(path).name.lower()
    if name in SENSITIVE_EXACT or name.startswith(".env"):
        return True
    if name.endswith(SENSITIVE_SUFFIXES):
        return True
    return any(s in name for s in SENSITIVE_SUBSTRINGS)


def viz_list(dir_override=None):
    d = safe_home_path(dir_override) if dir_override else VIZ_DIR
    if not d.is_dir():
        return []
    out = []
    for f in d.iterdir():
        if f.is_file() and f.suffix.lower() in VIZ_TYPES:
            out.append({"name": f.name, "size": f.stat().st_size,
                        "mtime": f.stat().st_mtime,
                        "kind": VIZ_TYPES[f.suffix.lower()]})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:50], d


def fs_listing(raw_path):
    d = safe_home_path(raw_path)
    if not d.is_dir():
        raise FileNotFoundError(raw_path)
    dirs, files = [], []
    for f in sorted(d.iterdir(), key=lambda p: p.name.lower()):
        if f.name.startswith(".") and f.name not in (".claude",):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if f.is_dir():
            dirs.append({"name": f.name, "dir": True})
        else:
            ext = f.suffix.lower()
            sens = is_sensitive(f.name)
            files.append({"name": f.name, "dir": False, "size": st.st_size,
                          "mtime": st.st_mtime, "sensitive": sens,
                          "viewable": (not sens)
                                      and (ext in VIZ_TYPES or ext in FS_TEXT)})
    home = str(Path.home().resolve())
    return {"path": str(d), "home": home,
            "parent": str(d.parent) if str(d) != home else None,
            "entries": (dirs + files)[:600]}


# ---------------------------------------------------------------- embedded terminal
#
# Runs a real PTY (claude CLI or your shell) and streams it to the browser
# over Server-Sent Events; input comes back via POST. Localhost only.

class Term:
    """Terminal session: shared scrollback + streaming; the transport
    (POSIX pty or Windows ConPTY) is supplied by a subclass."""

    def __init__(self, argv, cwd, cols=100, rows=30, extra_env=None):
        self.id = secrets.token_hex(8)
        self.label = Path(argv[0]).name + " · " + (Path(cwd).name or "/")
        self.argv, self.cwd = argv, cwd
        self.buf = bytearray()      # scrollback so re-attaching clients catch up
        self.cond = threading.Condition()
        self.alive = True
        env = {"TERM": "xterm-256color", "COLORTERM": "truecolor"}
        env.update(extra_env or {})
        self._spawn(argv, cwd, env, cols, rows)
        threading.Thread(target=self._pump, daemon=True).start()

    # -- transport hooks -------------------------------------------------
    def _spawn(self, argv, cwd, env, cols, rows):
        raise NotImplementedError

    def _read(self):
        """Blocking read; b'' means the terminal closed."""
        raise NotImplementedError

    def _write(self, data):
        raise NotImplementedError

    def _set_size(self, cols, rows):
        raise NotImplementedError

    def _hangup(self):
        """Ask the child to exit cleanly so its shutdown hooks run."""
        raise NotImplementedError

    def _terminate(self):
        raise NotImplementedError

    def _cleanup(self):
        pass

    # -- shared ----------------------------------------------------------
    def _pump(self):
        while True:
            try:
                data = self._read()
            except OSError:
                break
            if data is None:            # idle tick
                continue
            if not data:
                break
            with self.cond:
                self.buf.extend(data)
                if len(self.buf) > 512 * 1024:      # cap scrollback
                    del self.buf[: len(self.buf) - 512 * 1024]
                self.cond.notify_all()
        with self.cond:
            self.alive = False
            self.cond.notify_all()

    def write(self, data: bytes):
        try:
            self._write(data)
        except OSError:
            pass

    def resize(self, cols, rows):
        try:
            self._set_size(max(2, int(cols)), max(2, int(rows)))
        except (OSError, ValueError):
            pass

    def close_gracefully(self, grace=25.0):
        """Hang up first — Claude Code treats it as the terminal closing and
        runs its Stop/SessionEnd hooks — escalating only if it outlives the
        grace period. Blocks until the child is gone."""
        try:
            if self.alive:
                self._hangup()
                deadline = time.time() + grace
                while self.alive and time.time() < deadline:
                    time.sleep(0.15)
            if self.alive:
                self._terminate()
                deadline = time.time() + 3.0
                while self.alive and time.time() < deadline:
                    time.sleep(0.15)
        finally:
            self._cleanup()

    def kill(self):
        """Non-blocking graceful close (used by the per-tab close button)."""
        threading.Thread(target=self.close_gracefully, daemon=True).start()


class PosixTerm(Term):
    """macOS / Linux: fork a real pty."""

    def _spawn(self, argv, cwd, env, cols, rows):
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(cwd)
            except OSError:
                pass
            for k, v in env.items():
                os.environ[k] = v
            try:
                os.execvp(argv[0], argv)
            except OSError as e:
                os.write(2, f"exec failed: {e}\r\n".encode())
                os._exit(127)
        self.pid, self.fd = pid, fd
        self._set_size(cols, rows)

    def _read(self):
        r, _, _ = select.select([self.fd], [], [], 1.0)
        if not r:
            return None                     # idle tick
        return os.read(self.fd, 65536)

    def _write(self, data):
        os.write(self.fd, data)

    def _set_size(self, cols, rows):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))

    def _signal(self, sig):
        try:
            os.killpg(self.pid, sig)        # child is its own session leader
            return True
        except (AttributeError, OSError, ProcessLookupError):
            try:
                os.kill(self.pid, sig)
                return True
            except (OSError, ProcessLookupError):
                return False

    def _hangup(self):
        self._signal(getattr(signal, "SIGHUP", signal.SIGTERM))

    def _terminate(self):
        self._signal(signal.SIGTERM)
        time.sleep(0.3)
        if self.alive:
            self._signal(getattr(signal, "SIGKILL", signal.SIGTERM))

    def _cleanup(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass


class WindowsTerm(Term):
    """Windows 10 1809+: attach the child to a ConPTY pseudo-console."""

    def _spawn(self, argv, cwd, env, cols, rows):
        self.proc = winconpty.ConPtyProcess(argv, cwd, env, cols, rows)
        self.pid = self.proc.pid

    def _read(self):
        data = self.proc.read()
        if not data and not self.proc.alive():
            return b""
        return data or None

    def _write(self, data):
        self.proc.write(data)

    def _set_size(self, cols, rows):
        self.proc.set_size(cols, rows)

    def _hangup(self):
        # CTRL_CLOSE_EVENT via closing the pseudo-console: the child gets a
        # chance to run cleanup handlers, like SIGHUP on POSIX
        self.proc.request_close()

    def _terminate(self):
        self.proc.terminate()

    def _cleanup(self):
        self.proc.request_close()
        self.proc.close_handles()


TERMS = {}
TERMS_LOCK = threading.Lock()


def default_shell():
    if os.name == "nt":
        return os.environ.get("COMSPEC") or "powershell.exe"
    return os.environ.get("SHELL", "/bin/bash")


def start_term(kind, cwd, session_id=None, prompt=None):
    if not HAS_TERMINAL:
        raise NotImplementedError(
            "no pseudo-terminal available: " + (winconpty.unsupported_reason()
                                                or "unknown reason"))
    cwd = cwd if cwd and os.path.isdir(cwd) else str(Path.home())
    shell = default_shell()
    claude = shutil.which("claude")
    if kind == "shell" or (kind in ("claude", "resume") and not claude):
        argv = [shell, "-l"]
    elif kind == "resume" and session_id:
        argv = [claude, "--resume", session_id]
    else:
        argv = [claude]
        if prompt:                      # e.g. "/graphify" from the viz pane
            argv.append(str(prompt)[:2000])
    # let Claude Code (and anything else) know it runs inside this dashboard
    env = {"CLAUDE_DEVTOOLS_UI": "1",
           "CLAUDE_DEVTOOLS_VIZ_DIR": str(VIZ_DIR),
           "CLAUDE_DEVTOOLS_URL": f"http://127.0.0.1:{SERVER_PORT}"}
    impl = PosixTerm if HAS_PTY else WindowsTerm
    t = impl(argv, cwd, extra_env=env)
    with TERMS_LOCK:
        # keep at most 6 terminals; reap dead ones
        for tid in [tid for tid, tt in TERMS.items() if not tt.alive]:
            del TERMS[tid]
        if len(TERMS) >= 6:
            raise RuntimeError("too many open terminals — close one first")
        TERMS[t.id] = t
    return t


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "claude-devtools-lite/1.0"
    root = CLAUDE_ROOT  # overridden in main()

    def log_message(self, fmt, *args):
        # never let a token reach the log: query-string tokens would otherwise
        # persist wherever stderr is redirected
        line = fmt % args
        line = re.sub(r"([?&](?:token|k)=)[A-Za-z0-9]+", r"\1[redacted]", line)
        sys.stderr.write("[devtools] %s\n" % line)

    def _host_ok(self):
        """Reject foreign Host headers (DNS-rebinding guard)."""
        h = self.headers.get("Host")
        if not h:
            return True                      # non-browser clients may omit it
        return h.rsplit(":", 1)[0].strip("[]").lower() in ALLOWED_HOSTS

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "cdl":
                return v
        return ""

    def _authed(self, qs):
        tok = (self.headers.get("X-Devtools-Token") or qs.get("token", [""])[0]
               or self._cookie_token())
        return bool(tok) and secrets.compare_digest(tok, SERVER_TOKEN or "")

    def do_GET(self):
        try:
            if not self._host_ok():
                self._err(403, "bad Host header")
                return
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            p = u.path

            if p.startswith("/api/") and not self._authed(qs):
                self._err(401, "missing or bad token — relaunch via Claude DevTools.app")
                return

            if p == "/launch":
                # the app launcher's entry point: exchange the token (query)
                # for a browser cookie, then land on the dashboard. A real
                # navigation — immune to fragment-only tab-reuse races.
                k = qs.get("k", [""])[0]
                if not (k and secrets.compare_digest(k, SERVER_TOKEN or "")):
                    self._err(403, "bad launch token")
                    return
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie",
                                 f"cdl={SERVER_TOKEN}; Path=/; SameSite=Strict; "
                                 f"Max-Age=31536000")
                self.end_headers()
                return

            if p in ("/", "/index.html"):
                body = (HERE / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if p.startswith("/vendor/"):
                name = p[len("/vendor/"):]
                if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
                    self._err(404, "not found")
                    return
                f = HERE / "vendor" / name
                if not f.is_file():
                    self._err(404, "not found")
                    return
                ctype = "text/css" if name.endswith(".css") else "application/javascript"
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(body)
                return

            if p == "/api/term/stream":
                self.stream_term(qs)
                return

            if p == "/api/usage":
                self._json(usage_summary(self.root))
                return

            if p == "/api/state":
                self._json({"layout": state_read().get("layout"),
                            "has_terminal": HAS_TERMINAL,
                            "terminal_blocked": (None if HAS_TERMINAL
                                                 else winconpty.unsupported_reason()),
                            "platform": sys.platform})
                return

            if p == "/api/viz":
                files, d = viz_list(qs.get("dir", [None])[0])
                self._json({"dir": str(d), "default_dir": str(VIZ_DIR), "files": files})
                return

            if p == "/api/fs":
                self._json(fs_listing(qs.get("path", [None])[0]))
                return

            if p == "/api/fs/file":
                raw = qs.get("path", [""])[0]
                f = safe_home_path(raw)
                ext = f.suffix.lower()
                if is_sensitive(f.name):
                    self._err(403, "refused: file looks like it contains secrets")
                    return
                if not f.is_file() or (ext not in VIZ_TYPES and ext not in FS_TEXT):
                    self._err(404, "not previewable")
                    return
                if f.stat().st_size > 20 * 1024 * 1024:
                    self._err(413, "file too large to preview")
                    return
                ctype = VIZ_TYPES.get(ext, "text/plain")
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if ext in (".html", ".htm"):
                    # previews render in a sandboxed iframe (opaque origin: no
                    # access to this server's API or cookies). Allow https so
                    # CDN-based pages (e.g. graphify's vis-network) render.
                    self.send_header("Content-Security-Policy",
                                     "default-src 'unsafe-inline' data: blob: https:")
                self.end_headers()
                self.wfile.write(body)
                return

            if p == "/api/viz/file":
                name = qs.get("name", [""])[0]
                if not re.fullmatch(r"[A-Za-z0-9 ._()-]+", name) or name.startswith("."):
                    self._err(400, "bad name")
                    return
                f = VIZ_DIR / name
                if not f.is_file() or f.suffix.lower() not in VIZ_TYPES:
                    self._err(404, "not found")
                    return
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type",
                                 VIZ_TYPES[f.suffix.lower()] + "; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                # HTML previews render inside a sandboxed iframe client-side;
                # CSP here additionally blocks outbound requests from them
                if f.suffix.lower() in (".html", ".htm"):
                    self.send_header("Content-Security-Policy",
                                     "default-src 'unsafe-inline' data: blob:; "
                                     "script-src 'unsafe-inline'; img-src data: blob:")
                self.end_headers()
                self.wfile.write(body)
                return

            if p == "/api/projects":
                self._json(list_projects(self.root))
                return

            if p == "/api/sessions":
                slug = qs.get("project", [""])[0]
                d = safe_project_path(self.root, slug)
                metas = [session_meta(f) for f in d.glob("*.jsonl")]
                metas.sort(key=lambda m: m["mtime"], reverse=True)
                self._json(metas)
                return

            if p == "/api/session":
                slug = qs.get("project", [""])[0]
                sid = qs.get("id", [""])[0]
                if not re.fullmatch(r"[A-Za-z0-9_-]+", sid or ""):
                    self._err(400, "bad session id")
                    return
                f = safe_project_path(self.root, slug) / f"{sid}.jsonl"
                if not f.is_file():
                    self._err(404, "session not found")
                    return
                self._json(parse_session(f))
                return

            if p == "/api/subagent":
                slug = qs.get("project", [""])[0]
                sid = qs.get("session", [""])[0]
                agent = qs.get("agent", [""])[0]
                if not re.fullmatch(r"[A-Za-z0-9_-]+", sid or "") or \
                   not re.fullmatch(r"[A-Za-z0-9_.-]+\.jsonl", agent or ""):
                    self._err(400, "bad id")
                    return
                f = safe_project_path(self.root, slug) / sid / "subagents" / agent
                if not f.is_file():
                    self._err(404, "subagent not found")
                    return
                self._json(parse_session(f, include_sidechain=True))
                return

            if p == "/api/memory":
                slug = qs.get("project", [""])[0]
                self._json(read_memory(self.root, slug))
                return

            if p == "/api/search":
                q = qs.get("q", [""])[0]
                if len(q) < 2:
                    self._err(400, "query too short")
                    return
                proj = qs.get("project", [None])[0]
                self._json(search_all(self.root, q, proj))
                return

            self._err(404, "not found")
        except (FileNotFoundError, ValueError) as e:
            self._err(404, str(e))
        except BrokenPipeError:
            pass
        except Exception as e:  # keep the server alive; report the error
            self._err(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        try:
            if not self._host_ok():
                self._err(403, "bad Host header")
                return
            # CSRF guard: browsers can fire cross-origin "simple" POSTs at
            # localhost without preflight. Requiring a JSON content type forces
            # a preflight (which we never approve), and any Origin header must
            # be our own host. curl/local scripts just set the JSON header.
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype != "application/json":
                self._err(403, "Content-Type must be application/json")
                return
            origin = self.headers.get("Origin")
            if origin:
                host = urllib.parse.urlparse(origin).netloc.split(":")[0]
                if host not in ("127.0.0.1", "localhost", "[::1]"):
                    self._err(403, "cross-origin request refused")
                    return
            u = urllib.parse.urlparse(self.path)
            if not self._authed(urllib.parse.parse_qs(u.query)):
                self._err(401, "missing or bad token — relaunch via Claude DevTools.app")
                return
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            p = u.path

            if p == "/api/state":
                layout = body.get("layout")
                if isinstance(layout, dict):
                    clean = {k: float(v) for k, v in layout.items()
                             if k in ("col2", "rowL", "rowR", "sidebar")
                             and isinstance(v, (int, float))}
                    state_write({"layout": clean})
                self._json({"ok": True})
                return

            if p == "/api/shutdown":
                # close every terminal session gracefully IN PARALLEL so
                # Claude Code sessions get to run their SessionEnd hooks,
                # then exit once they are all down (or after the timeout).
                with TERMS_LOCK:
                    terms = list(TERMS.values())
                    TERMS.clear()
                threads = [threading.Thread(target=t.close_gracefully, daemon=True)
                           for t in terms]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join(timeout=30.0)
                self._json({"ok": True, "bye": True, "closed": len(terms)})
                threading.Timer(0.4, os._exit, [0]).start()
                return

            if p == "/api/term/start":
                kind = body.get("kind", "claude")
                cwd = None
                if body.get("project"):
                    cwd = project_cwd(self.root, body["project"])
                if body.get("cwd") and os.path.isdir(body["cwd"]):
                    cwd = body["cwd"]
                sid = body.get("session")
                if sid and not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
                    self._err(400, "bad session id")
                    return
                prompt = body.get("prompt")
                if prompt is not None and not isinstance(prompt, str):
                    prompt = None
                t = start_term(kind, cwd, sid, prompt=prompt)
                self._json({"id": t.id, "label": t.label, "cwd": t.cwd,
                            "argv": t.argv})
                return

            t = None
            tid = body.get("id", "")
            with TERMS_LOCK:
                t = TERMS.get(tid)
            if t is None:
                self._err(404, "terminal not found")
                return

            if p == "/api/term/input":
                t.write(base64.b64decode(body.get("data", "")))
                self._json({"ok": True})
                return
            if p == "/api/term/resize":
                t.resize(int(body.get("cols", 100)), int(body.get("rows", 30)))
                self._json({"ok": True})
                return
            if p == "/api/term/kill":
                t.kill()
                with TERMS_LOCK:
                    TERMS.pop(tid, None)
                self._json({"ok": True})
                return

            self._err(404, "not found")
        except NotImplementedError as e:
            self._err(501, str(e))
        except RuntimeError as e:
            self._err(429, str(e))
        except BrokenPipeError:
            pass
        except Exception as e:
            self._err(500, f"{type(e).__name__}: {e}")

    def stream_term(self, qs):
        """SSE stream of a terminal's output (base64 chunks)."""
        tid = qs.get("id", [""])[0]
        with TERMS_LOCK:
            t = TERMS.get(tid)
        if t is None:
            self._err(404, "terminal not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            pos = max(0, int(qs.get("from", ["0"])[0]))
        except ValueError:
            pos = 0
        pos = min(pos, len(t.buf))
        try:
            while True:
                with t.cond:
                    if pos >= len(t.buf) and t.alive:
                        t.cond.wait(timeout=15.0)
                    chunk = bytes(t.buf[pos:])
                    pos = len(t.buf)
                    alive = t.alive
                if chunk:
                    b64 = base64.b64encode(chunk).decode()
                    self.wfile.write(f"data: {b64}\n\n".encode())
                elif alive:
                    self.wfile.write(b": keepalive\n\n")  # comment frame
                self.wfile.flush()
                if not alive and pos >= len(t.buf):
                    self.wfile.write(b"event: exit\ndata: 0\n\n")
                    self.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def main():
    ap = argparse.ArgumentParser(description="claude-devtools-lite")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 3456)))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--root", default=str(CLAUDE_ROOT))
    args = ap.parse_args()

    global SERVER_PORT, SERVER_TOKEN, ALLOWED_HOSTS
    SERVER_PORT = args.port
    SERVER_TOKEN = load_token()
    ALLOWED_HOSTS = ALLOWED_HOSTS | {args.host.lower()}
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: binding beyond loopback exposes a shell to your "
              "network — anyone with the token gets code execution.",
              file=sys.stderr)
    Handler.root = Path(args.root).expanduser()
    if not projects_dir(Handler.root).is_dir():
        sys.exit(f"error: {Handler.root}/projects not found — is this a Claude Code machine?")
    VIZ_DIR.mkdir(exist_ok=True)
    threading.Thread(target=compute_baseline, args=(Handler.root,), daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"claude-devtools-lite → http://{args.host}:{args.port}/#t={SERVER_TOKEN}")
    print(f"  (root: {Handler.root}; token file: {TOKEN_FILE})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
