# claude-devtools-lite

A local dashboard for inspecting **Claude Code** sessions — timelines, thinking blocks,
tool calls, diffs, token usage, subagents, and memory — with an **embedded terminal**,
a **file explorer**, and a **visual output pane**, laid out like RStudio.

It reads `~/.claude` **read-only** and runs entirely on your machine.
**No dependencies, no build step, no API keys, no telemetry** — one Python file, one HTML
file, and the Python standard library. Works on **macOS, Linux, and Windows**.

```
┌────────────┬───────────────────────────┬──────────────────┐
│            │  SESSION                  │  TOKEN USE       │
│  projects  │  prompts, thinking,       │  5h block, P90   │
│  sessions  │  tool calls, diffs        │  limit, sparkline│
│  search    ├───────────────────────────┼──────────────────┤
│  memory    │  TERMINAL                 │  VIZ / FILES     │
│            │  real claude CLI / shell  │  charts, graphs, │
│            │  tabs, resume a session   │  file explorer   │
└────────────┴───────────────────────────┴──────────────────┘
```

Every pane can be **maximized (⛶)** or **resized by dragging** the splitters; the layout
is saved between sessions.

## Why

Claude Code writes a rich JSONL transcript for every session, but the CLI shows you a
condensed view of it. This reconstructs what actually happened: which files were read,
what each tool returned, what the model was thinking, how the context window filled up
and compacted, how many tokens each session burned — and lets you jump straight back
into any session in an embedded terminal.

## Features

**Session inspection**
- Every project and session under `~/.claude/projects/`, with real working-directory
  paths (decoded from the transcripts, not the lossy folder slugs)
- Full timeline: user prompts, assistant messages (rendered markdown), collapsible
  **thinking** blocks, and every **tool call** paired with its result
- **Real diffs** for `Edit`/`Write` calls, rendered from the recorded patch hunks
- **Context-window chart**: one bar per API request, with automatic **compaction
  detection** (red bars where the context dropped sharply)
- Token totals per session, deduplicated by request ID, plus a tool-call histogram
- **Subagent transcripts** open in the same viewer
- **Full-text search** across every session; results jump to the matching entry
- Project **memory** files rendered in place
- Big transcripts (20 MB+, thousands of entries) load lazily and stay responsive

**Token usage**
- Current 5-hour block with reset countdown, output tokens today and over 7 days,
  an hourly sparkline, and a by-model breakdown
- A limit bar showing the current block against the **P90 of your own historical
  blocks** (the [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor)
  approach). Your plan's real quota is not recorded locally — this is a measured
  baseline, not an official limit. Use `/status` in the CLI for the authoritative number.

**Embedded terminal**
- Real PTY streamed to [xterm.js](https://xtermjs.org/): full TUI, colors, resizing
- Launch `claude` in any project's directory, or **resume any session** you're viewing
  (`claude --resume <id>`) with one click
- Up to 6 tabs. Quitting closes sessions **gracefully** (SIGHUP on POSIX,
  `CTRL_CLOSE_EVENT` on Windows) so Claude Code's `SessionEnd` hooks run before exit
- Terminals export `CLAUDE_DEVTOOLS_UI=1`, `CLAUDE_DEVTOOLS_VIZ_DIR`, and
  `CLAUDE_DEVTOOLS_URL`, so a session can tell it's running inside the dashboard

**Viz inbox and file explorer**
- A watched folder: any `.html`, `.png`, `.svg`, `.md`, `.pdf`, `.csv` written there
  appears within 5 seconds and renders automatically. Tell a running Claude session
  *"write the chart to $CLAUDE_DEVTOOLS_VIZ_DIR"* and watch it appear.
- Projects with a [graphify](https://github.com/anthropics/skills) knowledge graph
  (`graphify-out/graph.html`) display it automatically; projects without one get a
  button that launches the skill
- A Files pane that follows the selected project, previews files, copies paths, opens a
  shell in any folder, or points the viz watcher at it

## Install and run

Requires **Python 3.9+** and an existing Claude Code installation (`~/.claude`).

```bash
git clone https://github.com/PierreBeaucoral/claude-devtools-lite.git
cd claude-devtools-lite
python3 server.py
```

Open the URL it prints (it includes a one-time token). That's the whole setup — but each
platform also has a double-click launcher:

### macOS

```bash
bash packaging/macos/build-app.sh
```

Builds `Claude DevTools.app` — a native window (WebKit wrapper, ~90 KB, no Electron)
with a Dock icon and ⌘Q. Drag it to `/Applications`. It starts the server if needed and
never spawns a duplicate.

### Linux

```bash
launchers/linux/install.sh
```

Adds "Claude DevTools" to your application menu (per-user, no `sudo`). Opens an app-mode
browser window (Chrome/Chromium/Brave/Edge) or your default browser. Full feature parity
with macOS.

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File launchers\windows\install.ps1
```

Creates Desktop and Start-menu shortcuts, or double-click
`launchers\windows\Claude DevTools.cmd`.

The embedded terminal works on **Windows 10 1809+** through ConPTY, driven via `ctypes`
— still no third-party packages. Verify it on your machine:

```
python tools\selftest_windows.py
```

On older Windows builds the server detects the missing API, explains it in the terminal
pane, and everything else keeps working.

## Usage

| Action | How |
|---|---|
| Browse a project | Click it in the sidebar; sessions expand underneath |
| Inspect a session | Click a session — timeline, chart, and token totals load |
| Hide noise | Toggle **thinking** / **tool calls** / **system** above the timeline |
| Search everything | Type in the search box, press Enter, click a result to jump to it |
| Open the CLI in a project | Hover a project → **⌨** |
| Start a session anywhere | **+ claude** → pick a project, `~`, or **browse…** for any folder |
| Resume a session | Open it → **⌨ resume in CLI** |
| Show a figure from a session | Have it write into `$CLAUDE_DEVTOOLS_VIZ_DIR` |
| Maximize a pane | **⛶** in its header (click again to restore) |
| Resize panes | Drag the splitters; sizes persist |
| Quit | **⏻** in the sidebar (or ⌘Q in the macOS app) |

Green dots mark projects whose transcripts changed since you last opened them, and a
toast appears when a background session finishes something.

### Making Claude aware of the dashboard

Add this to your `~/.claude/CLAUDE.md` so sessions launched from the terminal pane push
their visual output to the viz inbox on their own:

```markdown
## claude-devtools-lite UI awareness

When `CLAUDE_DEVTOOLS_UI=1` is set, this session runs inside the claude-devtools-lite
dashboard. To show the user a visual output (figure, chart, HTML report), also write a
self-contained file into `$CLAUDE_DEVTOOLS_VIZ_DIR` — it renders automatically in the
Viz pane. Prefer inline-only `.html`, `.png`, or `.svg`, with descriptive filenames.
```

## Security

The dashboard can spawn shells, so it is built to be safe on a shared machine:

- Binds to **127.0.0.1** only, and **never writes** to `~/.claude`
- Every `/api` route requires a **token** (generated once, stored `0600` in your OS's
  app-data directory, outside this repo). The launchers hand it to the browser via a
  same-site cookie — it never appears in a URL, and the request log redacts it
- **CSRF guard** (JSON content type + origin allowlist) and a **Host allowlist**
  (DNS-rebinding protection)
- File browsing is confined to `$HOME`, blocks path traversal and symlink escapes, and
  **refuses credential-shaped files** (`.env*`, `*secret*`, `*token*`, `id_rsa`,
  `*.pem`, `.netrc`, `hosts.yml`, …)
- HTML previews render in a **sandboxed iframe** with an opaque origin, so a previewed
  file cannot reach the dashboard's API or your token

**Do not run this with `--host 0.0.0.0`.** That would offer a shell to your network; the
server prints a warning if you try.

## Development

```bash
python3 -m pytest tests/ -q      # 27 tests
```

They cover the transcript-parsing invariants (usage dedup by request ID, tool pairing,
patch rendering, sidechains, compaction detection), 5-hour block grouping, path-safety
guards, the secret deny-list, the HTTP auth/CSRF/Host layer, and the Windows backend
helpers.

| File | Role |
|---|---|
| `server.py` | HTTP server, JSONL parsing, usage aggregation, PTY terminals |
| `winconpty.py` | Windows ConPTY transport (ctypes, no dependencies) |
| `index.html` | Single-page UI (vanilla JS, no framework) |
| `native/main.swift` | macOS standalone window (WebKit) |
| `launchers/`, `packaging/` | Per-platform launchers and app builders |
| `vendor/` | xterm.js 5.5.0 + fit addon (MIT), vendored for offline use |

## Prior art

Inspired by [claude-devtools](https://github.com/matt1398/claude-devtools) (Electron, far
more featureful) and [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor)
(the P90 usage-baseline idea). This one is deliberately tiny: two main files, standard
library only, hackable in an afternoon.

## Support

If this tool saves you time, you can [buy me a coffee via PayPal](https://www.paypal.me/pb63000).
Entirely optional — bug reports and pull requests are just as welcome.

## License

MIT — see [LICENSE](LICENSE). Bundles [xterm.js](https://github.com/xtermjs/xterm.js)
(MIT). Not affiliated with Anthropic.
