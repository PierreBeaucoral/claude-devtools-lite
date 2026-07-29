# claude-devtools-lite

A personal, zero-dependency equivalent of [claude-devtools](https://github.com/matt1398/claude-devtools):
a local web dashboard that inspects your Claude Code sessions by reading `~/.claude/` — **read-only, offline, no API keys, nothing installed**.

Built for macOS with Python 3.9+ standard library only (one `server.py` + one `index.html`).

## Run

**Double-click `Claude DevTools.app`** (Desktop, `/Applications`, or Spotlight). It's a
**standalone native window** — a compiled WebKit wrapper (`native/main.swift`), not a
browser tab. It starts the Python server if needed, authenticates via the cookie
handoff (`/launch?k=<token>`), and opens the dashboard in its own window with a Dock
icon, Cmd+Q, and Cmd+C/V.

Quitting: **Cmd+Q** (or the **⏻** button) closes every embedded terminal **gracefully**
— SIGHUP first, so Claude Code sessions run their Stop/SessionEnd hooks (e.g. dream
memory consolidation), with a ~25 s grace before any force-kill. Cmd+Q also stops the
server *if this app instance started it*; if the server was already running (e.g.
started manually), the app leaves it alone. Rebuild after editing the Swift source:

```bash
cd ~/Desktop/claude-devtools-lite/native && swiftc -O main.swift -o ClaudeDevTools -framework Cocoa -framework WebKit
```

then copy `ClaudeDevTools` into `Claude DevTools.app/Contents/MacOS/`. You can still use
any browser instead: run the server and open the `/launch?k=…` URL it prints.

### Linux

```bash
launchers/linux/install.sh     # adds "Claude DevTools" to your app menu (no sudo)
```

Or run `launchers/linux/claude-devtools.sh` directly. It starts the server,
opens an app-mode browser window (Chrome/Chromium/Brave/Edge) or your default
browser, and authenticates via the cookie handoff. **Full feature parity with
macOS**, embedded terminal included — Linux has POSIX pseudo-terminals.
Token/state live in `~/.config/claude-devtools/`.

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File launchers\windows\install.ps1
```

Creates Desktop and Start-menu shortcuts; or double-click
`launchers\windows\Claude DevTools.cmd`. Token/state live in
`%APPDATA%\claude-devtools\`.

**One limitation:** the embedded terminal pane is unavailable on Windows. It
needs a POSIX pseudo-terminal, and Windows' ConPTY equivalent would require a
third-party package (`pywinpty`), which would break the zero-dependency
guarantee. The server detects this, reports it in the terminal pane, and hides
the terminal buttons — every other pane (sessions, token use, viz, files
explorer, search, memory) works normally. Run `claude` in Windows Terminal
alongside the dashboard.

### Any platform, from a terminal

```bash
python3 server.py
```

Then open <http://127.0.0.1:3456>. Options: `--port`, `--host`, `--root` (defaults to
`~/.claude`, or set `CLAUDE_ROOT`). The app bundle hardcodes the `server.py` path — if
you move the folder, update `Claude DevTools.app/Contents/MacOS/launcher`.

Handy alias for `~/.zshrc`:

```bash
alias devtools='python3 ~/Desktop/claude-devtools-lite/server.py'
```

From a Claude Code session in `~/Desktop`, the browser pane can also start it via the
`devtools` entry in `Desktop/.claude/launch.json`.

## Layout (RStudio-style)

Four panes in a 2×2 workspace, plus the project sidebar. Every pane header has a
**⛶** button that maximizes it to the full window (click again to restore) —
same interaction as RStudio's pane zoom. **◫** toggles the right column.

| | left | right |
|---|---|---|
| **top** | Session viewer | Token use |
| **bottom** | Terminal console | Viz / Files |

## What it shows

- **Project & session browser** — every project under `~/.claude/projects/`, with real
  paths (decoded from the session records, not the lossy slug), session titles, dates, sizes.
- **Full timeline per session** — user prompts, assistant replies (markdown), collapsible
  **thinking** blocks, and every **tool call** with its input and paired result
  (Bash stdout/stderr, file reads, WebFetch/WebSearch results, …).
- **Real diffs** — `Edit`/`Write` calls render their `structuredPatch` hunks with +/− highlighting.
- **Context-window chart** — one bar per API request (input + cache read + cache write),
  with automatic **compaction detection** (red bars on >35 % context drops). Hover for details.
- **Token totals** — requests, output tokens, cache read/write, peak context, per session.
- **Tool histogram** — which tools were called and how often.
- **Subagent transcripts** — agent files under `<session>/subagents/` open in the same viewer.
- **Project memory** — `memory/MEMORY.md` and all memory files, rendered.
- **Full-text search** — across every session of every project; results click through to the session.
- **Copy buttons** on every block; toggles to hide thinking / tools / system noise.
- Big sessions (20 MB+, 3000+ entries) load lazily in chunks of 150 entries.
- **New-activity badges** — a green dot on any project whose transcripts changed since
  you last opened it (watermark kept in the browser's localStorage; refreshes every 30 s).
- **Token-use panel** (upper right, toggle with ◫) — computed live from your local logs:
  current 5-hour block with reset countdown and progress bar, output tokens today and
  over 7 days, an output-per-hour sparkline for the last 24 h, and a by-model breakdown.
  A **limit bar** shows the current block as a % of the **P90 of all your historical
  5-hour blocks** (the [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor)
  approach — a background scan of your full history at startup, ratcheted in `.state.json`;
  green < 65 %, orange < 90 %, red above). It's a measured baseline, not the official
  quota — for that use `/status` in the CLI.
- **Viz inbox** (lower right) — a watched folder (`viz/` next to `server.py`). Any
  `.html`, image, `.svg`, `.md`, `.pdf`, `.csv`… file written there appears in the panel
  within 5 s and the newest one auto-renders in a sandboxed iframe. Tell a Claude Code
  session *"write the chart to ~/Desktop/claude-devtools-lite/viz/"* and it shows up
  while the session is still running.
- **Graphify integration** — if the selected project has `graphify-out/graph.html`
  (a [/graphify](../.claude/skills/graphify) knowledge graph), it's pinned and displayed
  in the viz pane automatically when you click the project. Projects without one show a
  **"build one (/graphify)"** button that opens `claude` in the project folder with the
  `/graphify` prompt pre-filled.
- **Files explorer** (Files tab of the lower-right pane, like RStudio's Files pane) —
  browses your home directory; **follows the selected project** (clicking a project in
  the sidebar jumps the explorer to that project's real working directory). Per row:
  **copy** (full path to clipboard) and **→ CLI** (pastes the path into the active
  terminal, opening a shell in that folder if none is running). The breadcrumb offers
  **⌨ shell here** and **👁 watch** (points the Viz panel at the current folder;
  click again to return to the default inbox). Restricted to `$HOME`.
- **Embedded terminal** — a real PTY streamed to xterm.js in the console pane:
  - **⌨ CLI** button (top of sidebar) → `claude` in `$HOME`; `+ shell` → your login shell
  - hover a project → **⌨** → `claude` launched in that project's working directory
  - open a session → **⌨ resume in CLI** → `claude --resume <session-id>` in the right cwd
  - up to 6 tabs, full TUI support (colors, cursor, alternate screen)
  - every terminal exports `CLAUDE_DEVTOOLS_UI=1`, `CLAUDE_DEVTOOLS_VIZ_DIR`, and
    `CLAUDE_DEVTOOLS_URL`, and `~/.claude/CLAUDE.md` has a matching section — so any
    Claude Code session launched from here knows it's inside this UI and writes visual
    outputs to the viz inbox on its own.

## Extras

- **Deep links** — the URL hash tracks `#p=<project>&s=<session>`; reloads and bookmarks
  restore your place. Search results open the session **scrolled to the matching entry**
  (auto-expanded and briefly highlighted).
- **Activity toasts** — while you work, a clickable toast appears when another project's
  transcripts change (a background Claude session finished something).
- **Tests** — `python3 -m pytest tests/ -q` (15 tests): usage dedup by requestId, tool
  pairing, structuredPatch, sidechain handling, compaction detection, 5h-block grouping,
  path-safety guards, and the HTTP auth/CSRF layer against a live ephemeral server.
- **Git** — the folder is a repo; commit after changes.

## Safety / design notes

- The server binds to `127.0.0.1` by default and **never writes** to `~/.claude`.
- **Auth token**: every `/api` endpoint requires a token (401 otherwise). It's generated
  once into `.token` (mode 0600) and passed to the browser via the URL fragment by the
  app launcher, then kept in localStorage. This protects against *other local users* on
  the machine; the CSRF guard below protects against hostile web pages. If a browser
  ever shows the 401 message, relaunch via `Claude DevTools.app`.
- The terminal endpoints can execute commands, so POSTs are CSRF-guarded: they require
  `Content-Type: application/json` (forcing a browser preflight) and reject foreign
  `Origin` headers. Do **not** expose this server beyond localhost (`--host 0.0.0.0`
  would hand a shell to your network).
- Terminal output is buffered server-side (512 kB scrollback per terminal) so
  reconnecting clients resume from an offset without duplication.
- Viz-inbox HTML renders in a sandboxed iframe (`allow-scripts` only, no same-origin)
  with a CSP that blocks outbound network requests — an untrusted file can draw, but
  it cannot touch the dashboard's APIs or phone home.
- Session-list metadata is cached in memory keyed on file mtime+size; files over ~768 kB
  are scanned head+tail only for listing (full parse happens when you open the session).
- Tool results are truncated at 20 k chars and text blocks at 120 k chars before being
  sent to the browser; truncation is labeled inline.
- Assistant usage is deduplicated by `requestId` (one API response is logged once per
  content block), so token totals are not double-counted.

## Files

| File | Role |
|------|------|
| `server.py` | HTTP server + JSONL parsing (projects, sessions, subagents, memory, search APIs) + PTY terminal backend |
| `index.html` | Single-page UI (vanilla JS, dark theme) |
| `vendor/` | xterm.js 5.5.0 + fit addon, vendored locally (fetched once from jsdelivr; works offline) |

## Differences vs. the original claude-devtools

Kept: session explorer, timeline reconstruction, thinking/tool inspection, diffs,
context/compaction visualization, subagent trees, memory viewer, search, copy/paste focus.

Dropped (deliberately, for a personal tool): Electron packaging, SSH remote sessions,
system notifications/regex triggers, multi-pane drag-and-drop tabs, per-category token
attribution of the system prompt (the raw logs don't label CLAUDE.md vs. skills tokens;
approximating it would violate "correctness first").
