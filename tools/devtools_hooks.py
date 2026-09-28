#!/usr/bin/env python3
"""claude-devtools-lite's two optional Claude Code tees. Stdlib only.

    devtools_hooks.py statusline        statusLine command: saves Claude Code's
                                        status JSON (official 5h / 7-day limit %,
                                        context %, cost) for the Usage pane, then
                                        runs your previous statusline unchanged
    devtools_hooks.py event             async hook: appends one METADATA line per
                                        event (never tool inputs or outputs) so
                                        the dashboard can show what each session
                                        is doing and when one waits for you
    devtools_hooks.py install-statusline | uninstall-statusline
    devtools_hooks.py install-events     | uninstall-events

The install commands edit ~/.claude/settings.json (a copy is saved next to it
first as settings.json.bak-devtools). The Add-ons pane runs them in a visible
terminal; nothing is installed behind your back.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if sys.platform == "darwin":
    APP_DIR = Path.home() / "Library" / "Application Support" / "claude-devtools"
elif os.name == "nt":
    APP_DIR = Path(os.environ.get("APPDATA",
                                  Path.home() / "AppData" / "Roaming")) / "claude-devtools"
else:
    APP_DIR = Path(os.environ.get("XDG_CONFIG_HOME",
                                  Path.home() / ".config")) / "claude-devtools"
STATUS_DIR = APP_DIR / "status"
EVENTS = APP_DIR / "events.jsonl"
INNER = APP_DIR / "statusline_inner.json"      # the statusline we wrap
EVENTS_MAX = 5_000_000
SETTINGS = Path(os.environ.get("CLAUDE_ROOT", Path.home() / ".claude")) / "settings.json"
MARK = "devtools_hooks.py"

# events worth a line; the rest would only add noise
EVENT_NAMES = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse",
               "PostToolUse", "PostToolUseFailure", "PermissionRequest",
               "Notification", "SubagentStart", "SubagentStop", "PreCompact",
               "PostCompact", "Stop", "StopFailure")
# metadata only: tool_input / tool_response / prompt are deliberately absent
KEEP = ("hook_event_name", "session_id", "cwd", "tool_name", "agent_type",
        "agent_id", "notification_type", "message", "matcher", "stop_hook_active")


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def statusline(stdin_text):
    """Save the status JSON, then hand the same stdin to the wrapped command."""
    try:
        data = json.loads(stdin_text)
        sid = "".join(c for c in str(data.get("session_id", "")) if c.isalnum() or c in "-_")
        if sid:
            data["_saved_at"] = time.time()
            _atomic_write(STATUS_DIR / f"{sid}.json", json.dumps(data))
    except (ValueError, OSError):
        pass                        # never break the user's statusline
    try:
        inner = json.loads(INNER.read_text(encoding="utf-8")).get("command")
    except (OSError, ValueError):
        inner = None
    if inner:
        r = subprocess.run(inner, shell=True, input=stdin_text, text=True,
                           capture_output=True)
        sys.stdout.write(r.stdout)
        return r.returncode
    try:                            # no previous statusline: a minimal one
        d = json.loads(stdin_text)
        five = ((d.get("rate_limits") or {}).get("five_hour") or {}).get("used_percentage")
        ctx = (d.get("context_window") or {}).get("used_percentage")
        parts = [str((d.get("model") or {}).get("display_name") or "")]
        if ctx is not None:
            parts.append(f"ctx {ctx:.0f}%")
        if five is not None:
            parts.append(f"5h {five:.0f}%")
        print(" · ".join(p for p in parts if p))
    except (ValueError, TypeError, AttributeError):
        pass
    return 0


def event(stdin_text):
    """One compact line per event; rotated at 5 MB (one old copy kept)."""
    try:
        d = json.loads(stdin_text)
    except ValueError:
        return 0
    line = {k: d[k] for k in KEEP if k in d}
    if isinstance(line.get("message"), str):
        line["message"] = line["message"][:200]
    line["ts"] = time.time()
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if EVENTS.exists() and EVENTS.stat().st_size > EVENTS_MAX:
            os.replace(EVENTS, EVENTS.with_suffix(".jsonl.1"))
        with open(EVENTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError:
        pass
    return 0


def _cmd(sub):
    q = lambda s: f'"{s}"' if " " in s else s
    return f"{q(sys.executable)} {q(str(Path(__file__).resolve()))} {sub}"


def _load_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _save_settings(st):
    if SETTINGS.exists():
        backup = SETTINGS.with_name("settings.json.bak-devtools")
        backup.write_text(SETTINGS.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"saved a copy of your settings: {backup}")
    _atomic_write(SETTINGS, json.dumps(st, indent=2) + "\n")


def install_statusline():
    st = _load_settings()
    cur = st.get("statusLine") or {}
    if MARK in str(cur.get("command", "")):
        print("statusline tee already installed")
        return 0
    if cur.get("command"):
        _atomic_write(INNER, json.dumps({"command": cur["command"]}))
        print(f"your statusline keeps working (wrapped): {cur['command']}")
    st["statusLine"] = dict(cur, type="command", command=_cmd("statusline"))
    _save_settings(st)
    print("installed: new Claude Code sessions report their limits to the dashboard")
    return 0


def uninstall_statusline():
    st = _load_settings()
    if MARK not in str((st.get("statusLine") or {}).get("command", "")):
        print("statusline tee not installed")
        return 0
    try:
        inner = json.loads(INNER.read_text(encoding="utf-8")).get("command")
    except (OSError, ValueError):
        inner = None
    if inner:
        st["statusLine"]["command"] = inner
    else:
        st.pop("statusLine", None)
    _save_settings(st)
    print("statusline restored")
    return 0


def install_events():
    st = _load_settings()
    hooks = st.setdefault("hooks", {})
    added = 0
    for ev in EVENT_NAMES:
        entries = hooks.setdefault(ev, [])
        if any(MARK in str(h.get("command", "")) for e in entries for h in e.get("hooks", [])):
            continue
        entries.append({"hooks": [{"type": "command", "command": _cmd("event"),
                                   "async": True, "timeout": 5}]})
        added += 1
    _save_settings(st)
    print(f"installed an async event hook on {added} event(s)")
    return 0


def uninstall_events():
    st = _load_settings()
    hooks = st.get("hooks") or {}
    for ev in list(hooks):
        kept = []
        for e in hooks[ev]:
            hs = [h for h in e.get("hooks", []) if MARK not in str(h.get("command", ""))]
            if hs:
                kept.append(dict(e, hooks=hs))
        if kept:
            hooks[ev] = kept
        else:
            del hooks[ev]
    _save_settings(st)
    print("event hook removed")
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "statusline":
        return statusline(sys.stdin.read())
    if cmd == "event":
        return event(sys.stdin.read())
    fn = {"install-statusline": install_statusline, "uninstall-statusline": uninstall_statusline,
          "install-events": install_events, "uninstall-events": uninstall_events}.get(cmd)
    if not fn:
        print(__doc__)
        return 2
    return fn()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
