# claude-devtools-lite

A local dashboard for inspecting **Claude Code** sessions — timelines, thinking blocks,
tool calls, diffs, token usage, subagents, and memory — with an **embedded terminal**,
a **live plan checklist**, a **file explorer**, and a **visual output pane**, laid out
like RStudio.

It reads your transcripts **read-only** and runs entirely on your machine.
**No dependencies, no build step, no API keys, no telemetry** — one Python file, one HTML
file, and the Python standard library. Works on **macOS, Linux, and Windows**.

```
┌────────────┬───────────────────────────┬──────────────────┐
│            │  SESSION                  │  TOKEN USE       │
│  projects  │  prompts, thinking,       │  5h block, P90   │
│  sessions  │  tool calls, diffs        │  limit, sparkline│
│  search    │                           ├──────────────────┤
│  memory    │                           │  PLAN / CONFIG   │
│            ├───────────────────────────┤  checklist, what │
│            │                           │  ~/.claude costs │
│            │  TERMINAL                 ├──────────────────┤
│            │  real claude CLI / shell  │  VIZ / FILES     │
│            │  tabs, resume a session   │  charts, graphs, │
│            │                           │  file explorer   │
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
- **LaTeX renders as math** — `$$…$$`, `\[…\]`, `\(…\)` and `$…$` are typeset with
  KaTeX, so derivations and estimators read like a paper, not like source. Prices
  (`$5`) and shell variables (`$HOME`) are left alone
- **Markdown tables render as tables**, including the ragged ones Claude often emits
  (a `|---|---|` line shorter than its own header); math inside cells is typeset too
- **Subagents appear where they were launched**: each `Task` call carries an expandable
  card showing the agent type, its task, whether it failed, and what it cost — entries,
  tool calls, output tokens, peak context, wall time. Expanding nests the agent's
  transcript inline, fetched on demand, so you keep your place in the parent session
- **Real diffs** for `Edit`/`Write` calls, rendered from the recorded patch hunks
- **Context-window chart**: one bar per API request, with automatic **compaction
  detection** (red bars where the context dropped sharply)
- Token totals per session, deduplicated by request ID, plus a tool-call histogram
- **Subagent transcripts** open in the same viewer, and the header chips are named by
  agent type rather than uuid
- **The sidebar filters as you type** (project paths, and session titles in opened
  projects); **Enter** runs the full-text search across every session, and results jump
  to the matching entry. Esc clears the filter
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

**Plan pane**
- A third right-hand quadrant showing the project's plan as a **live checklist**.
  It reads the first plan file it finds: `.claude/plan.md`, then the newest
  `quality_reports/plans/*.md`, then `PLAN.md` / `TODO.md` / `TASKS.md` / `ROADMAP.md`
- **Ticking a box rewrites the marker in the file** — so the plan is a shared artefact:
  Claude Code writes it, you tick it, the next session reads the ticks back. `[~]` and
  `[/]` render as *in progress*
- Follows the selected project **and the active terminal tab**, so switching consoles
  switches plans; re-reads the file every few seconds, so edits Claude makes show up live
- A file picker appears when a project has several plans; **＋ create** scaffolds
  `.claude/plan.md` when it has none

**Config inventory (⚙ Config tab)**
- What is actually installed in `~/.claude` — agents, skills, commands, rules, hooks,
  plugins, MCP servers — with the project's own `.claude/` alongside it when it has one
- Splits **resident** from **on demand**: `CLAUDE.md` and `rules/` (subfolders included,
  as Claude Code loads them) are pasted into every
  request, and so is one description line per agent/skill/command — their bodies are not.
  So a 45 kB command is nearly free until you invoke it, while a 10 kB rules file is a tax
  on every turn. The pane totals both and sorts each group heaviest-first
- **Flags hooks nothing points at**: files sitting in `~/.claude/hooks/` that no
  `settings.json` event references show in amber, and hooks registered from outside that
  folder still get a row
- **MCP servers are found where Claude Code actually keeps them** — `~/.claude.json`, both
  the global list and the per-project one — not `settings.json`, which usually has none
- Metadata only — never file contents, and never MCP server args or env, which routinely
  hold API keys (there is a test asserting this). Token figures are estimated at ~4 bytes each: rank with them, don't budget
- Click any row to open the file in the Files preview

**End-of-session retrospective**
- When a Claude terminal closes — including when you quit the app — the dashboard can
  run [`/improve`](https://github.com/TerenceBristol/claude-improve) over the transcript
  that just ended and drop a dated report in `~/.claude/improve-reports/<project>/`
- **Read-only by construction** (`--allowedTools Read Grep Glob`) and explicitly told to
  propose rather than apply, so it never edits your `CLAUDE.md` behind your back
- Rate-limited to one run per project per 3 hours, skipped for sessions under 20 KB, and
  guarded against recursing into its own session
- The newest report opens from the **🔎** button in the plan pane. Turn the whole thing
  off with `CDL_IMPROVE=0` in the environment
- Needs the command installed once:
  `mkdir -p ~/.claude/commands && curl -o ~/.claude/commands/improve.md https://raw.githubusercontent.com/TerenceBristol/claude-improve/main/improve.md`

**Viz inbox and file explorer**
- A watched folder: any `.html`, `.png`, `.svg`, `.md`, `.pdf`, `.csv` written there
  appears within 5 seconds and renders automatically. Tell a running Claude session
  *"write the chart to $CLAUDE_DEVTOOLS_VIZ_DIR"* and watch it appear.
- Projects with a [graphify](https://github.com/anthropics/skills) knowledge graph
  (`graphify-out/graph.html`) display it automatically; projects without one get a
  button that launches the skill
- A Files pane that follows the selected project, previews files, copies paths, opens a
  shell in any folder, or points the viz watcher at it
- It also follows the **active terminal tab**: switch between two Claude sessions and
  the explorer jumps to that session's project root — or back to wherever you had
  browsed to in it
- **Click a preview to expand it**: the pane goes full-screen (Esc, or ⛶, restores it),
  which is where a knowledge graph or a wide figure is actually usable

**Figure comments** (after [exhibit-review](https://github.com/paulgp/exhibit-review))
- On any image in the Viz pane, **💬 Comment** opens it full-size: click a spot or drag a
  box, then write what should change. Marks are numbered, and each comment is *open*,
  *resolved* or *wontfix*
- Comments save automatically to `.review/<figure>.json` next to the figure, with
  coordinates as fractions of the image (so they survive a re-render at another size) and
  the image's sha256. The figure itself is never written
- **Send to Claude** types a prompt into the active Claude tab (you press Enter), or starts
  a session in the project: find the script behind the figure, apply the open comments,
  re-render, mark them resolved in the JSON
- When the image changes after comments were saved, a **figure regenerated** badge warns
  that old marks may no longer line up. Saves refuse to overwrite a newer revision (for
  example one Claude just wrote)

**Themes**
- **◐** in the sidebar switches the whole app, terminal included: GitHub Dark (default),
  GitHub Light, Solarized Dark / Light, Dracula, Monokai, Tomorrow Night, Cobalt — the
  editor themes RStudio users know. The choice is remembered per browser
- Claude Code picks its own colours: with a light theme here, run `/theme light` there

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
| Filter the sidebar | Type in the search box — the tree narrows as you type |
| Search everything | Type in the search box, press Enter, click a result to jump to it |
| Open the CLI in a project | Hover a project → **⌨** |
| Start a session anywhere | **+ claude** → pick a project, `~`, or **browse…** for any folder |
| Resume a session | Open it → **⌨ resume in CLI** |
| Show a figure from a session | Have it write into `$CLAUDE_DEVTOOLS_VIZ_DIR` |
| Comment on a figure | Show it in the Viz pane → **💬 Comment** → click or drag, type, **Send to Claude** |
| Change the theme | **◐** in the sidebar |
| Tick off a plan step | Click it in the **PLAN** pane — the markdown file is updated |
| Point the plan pane elsewhere | Use its file picker, or **＋ create `.claude/plan.md`** |
| Read the last retrospective | **🔎** in the PLAN pane header |
| See what's loaded into every turn | **⚙ Config** tab in the PLAN pane |
| Expand a preview | Click the preview itself (Esc restores) |
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

Keep the working plan in `.claude/plan.md` as markdown checkboxes (`- [ ] step`). The
dashboard's PLAN pane renders it and writes ticks back into it, so re-read it before
planning and update it as steps complete.

Figure feedback lives in `.review/<figure>.json` next to a figure (coordinates are
fractions of the image, origin top-left). Before regenerating a figure, read its open
comments; after applying one, set its `status` to `"resolved"` in that file.
```

## Security

The dashboard can spawn shells, so it is built to be safe on a shared machine:

- Binds to **127.0.0.1** only. It **never modifies your transcripts, settings or
  memory**. It writes in exactly three places: its own state file, the plan checkbox you
  click (see below), and `~/.claude/improve-reports/` when a retrospective runs
- **Plan writes are narrow**: only a file the pane discovered for the open project, only
  the `[ ]` / `[x]` marker on one line, and only when the line's text still matches what
  the UI displayed — a stale click is refused rather than applied to the wrong task
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
| `vendor/` | xterm.js 5.5.0 + fit addon and KaTeX 0.16.11 with its woff2 fonts (both MIT), vendored for offline use |
| `tests/` | `pytest tests/` for the server; `node tests/test_frontend.js` for the UI |

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
(MIT) and [KaTeX](https://github.com/KaTeX/KaTeX) (MIT). Not affiliated with Anthropic.
