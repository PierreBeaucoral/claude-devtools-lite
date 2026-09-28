"""Regression tests for claude-devtools-lite's server.

Run:  python3 -m pytest tests/ -q
Covers the JSONL parsing invariants (usage dedup, tool pairing, sidechains,
compaction), the 5h-block reconstruction, path-safety guards, and the HTTP
auth/CSRF layer against a live server on an ephemeral port.
"""
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("cdl_server", HERE.parent / "server.py")
srv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(srv)


# ---------------------------------------------------------------- fixtures

def rec_user(text, ts="2026-07-28T10:00:00.000Z", sidechain=False, uuid="u1"):
    return {"type": "user", "isSidechain": sidechain, "uuid": uuid, "timestamp": ts,
            "message": {"role": "user", "content": text}, "cwd": "/Users/x/proj",
            "version": "2.1.219", "gitBranch": "main"}


def rec_assistant(blocks, rid="req_1", ts="2026-07-28T10:00:05.000Z",
                  usage=None, sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain, "requestId": rid,
            "timestamp": ts,
            "message": {"role": "assistant", "model": "claude-fable-5",
                        "content": blocks,
                        "usage": usage or {"input_tokens": 10, "output_tokens": 100,
                                           "cache_read_input_tokens": 1000,
                                           "cache_creation_input_tokens": 200}}}


def rec_tool_result(tool_use_id, content, tur=None, ts="2026-07-28T10:00:10.000Z"):
    r = {"type": "user", "isSidechain": False, "timestamp": ts,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}]}}
    if tur is not None:
        r["toolUseResult"] = tur
    return r


def write_session(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


@pytest.fixture()
def session_file(tmp_path):
    f = tmp_path / "proj" / "abc123.jsonl"
    write_session(f, [
        {"type": "queue-operation", "operation": "enqueue"},
        rec_user("hello world"),
        rec_assistant([{"type": "thinking", "thinking": "let me think"}], rid="req_1"),
        # same requestId again (multi-block response) — usage must count ONCE
        rec_assistant([{"type": "text", "text": "the answer"}], rid="req_1"),
        rec_assistant([{"type": "tool_use", "id": "tu_1", "name": "Bash",
                        "input": {"command": "echo hi"}}], rid="req_2",
                      ts="2026-07-28T10:01:00.000Z"),
        rec_tool_result("tu_1", "ignored",
                        tur={"stdout": "hi", "stderr": "warn!", "interrupted": False}),
        rec_assistant([{"type": "tool_use", "id": "tu_2", "name": "Edit",
                        "input": {"file_path": "/a.py", "old_string": "x",
                                  "new_string": "y"}}], rid="req_3",
                      ts="2026-07-28T10:02:00.000Z"),
        rec_tool_result("tu_2", "ok",
                        tur={"filePath": "/a.py", "oldString": "x", "newString": "y",
                             "structuredPatch": [{"oldStart": 1, "oldLines": 1,
                                                  "newStart": 1, "newLines": 1,
                                                  "lines": ["-x", "+y"]}]}),
        rec_user("sidechain msg", sidechain=True),
        "{not valid json",
        {"type": "custom-title", "customTitle": "My Test Session", "leafUuid": "u1"},
    ])
    return f


# ---------------------------------------------------------------- parsing

def test_usage_deduped_by_request(session_file):
    s = srv.parse_session(session_file)
    assert s["totals"]["requests"] == 3          # req_1 counted once, not twice
    assert s["totals"]["output_tokens"] == 300
    assert s["totals"]["peak_context"] == 1210


def test_timeline_kinds_and_title(session_file):
    s = srv.parse_session(session_file)
    kinds = [e["kind"] for e in s["entries"]]
    assert kinds == ["user", "thinking", "assistant", "tool", "tool"]
    assert s["title"] == "My Test Session"       # custom-title wins over first prompt
    assert s["model"] == "claude-fable-5"
    assert s["sidechain_msgs"] == 1              # skipped from the main timeline


def test_tool_result_pairing(session_file):
    s = srv.parse_session(session_file)
    bash = next(e for e in s["entries"] if e["kind"] == "tool" and e["name"] == "Bash")
    assert "hi" in bash["result"] and "[stderr]" in bash["result"]
    edit = next(e for e in s["entries"] if e["kind"] == "tool" and e["name"] == "Edit")
    assert edit["patch"][0]["lines"] == ["-x", "+y"]
    assert edit["file_path"] == "/a.py"


def test_sidechain_included_for_subagents(tmp_path):
    f = tmp_path / "agent.jsonl"
    write_session(f, [rec_user("agent prompt", sidechain=True),
                      rec_assistant([{"type": "text", "text": "done"}], sidechain=True)])
    assert len(srv.parse_session(f)["entries"]) == 0            # main view: skipped
    s = srv.parse_session(f, include_sidechain=True)            # subagent view: kept
    assert [e["kind"] for e in s["entries"]] == ["user", "assistant"]
    assert s["title"] == "agent prompt"


def test_title_falls_back_to_first_prompt(tmp_path):
    f = tmp_path / "s.jsonl"
    write_session(f, [rec_user("first prompt here"),
                      rec_assistant([{"type": "text", "text": "hi"}])])
    assert srv.parse_session(f)["title"] == "first prompt here"


def test_compaction_detected_on_context_drop(tmp_path):
    big = {"input_tokens": 10, "output_tokens": 5,
           "cache_read_input_tokens": 200_000, "cache_creation_input_tokens": 0}
    small = {"input_tokens": 10, "output_tokens": 5,
             "cache_read_input_tokens": 40_000, "cache_creation_input_tokens": 0}
    f = tmp_path / "s.jsonl"
    write_session(f, [
        rec_assistant([{"type": "text", "text": "a"}], rid="r1", usage=big),
        rec_assistant([{"type": "text", "text": "b"}], rid="r2", usage=small,
                      ts="2026-07-28T11:00:00.000Z"),
    ])
    series = srv.parse_session(f)["context_series"]
    assert not series[0].get("compaction") and series[1].get("compaction")


def test_malformed_lines_skipped(session_file):
    # the "{not valid json" line must not break anything (implicitly covered
    # above, asserted explicitly here)
    assert srv.parse_session(session_file)["entries"]


# ---------------------------------------------------------------- usage blocks

def test_blocks_split_on_5h_gap():
    h = 3600
    recs = [(1000 * h, 50, "m"), (1000 * h + 2 * h, 30, "m"),   # block 1
            (1000 * h + 9 * h, 20, "m")]                        # >5h later: block 2
    blocks = srv.blocks_from_records(recs)
    assert len(blocks) == 2
    assert blocks[0][2] == 80 and blocks[1][2] == 20
    assert blocks[0][1] - blocks[0][0] == 5 * h


# ---------------------------------------------------------------- path safety

def test_safe_home_path_blocks_escape():
    with pytest.raises(ValueError):
        srv.safe_home_path("/etc")
    with pytest.raises(ValueError):
        srv.safe_home_path(str(Path.home()) + "/../../etc")
    assert srv.safe_home_path(str(Path.home())) == Path.home().resolve()


def test_safe_project_path_blocks_traversal(tmp_path):
    with pytest.raises(ValueError):
        srv.safe_project_path(tmp_path, "../evil")
    with pytest.raises(ValueError):
        srv.safe_project_path(tmp_path, ".hidden")


def test_fs_listing_hides_dotfiles_except_dot_claude(tmp_path, monkeypatch):
    home = Path.home()
    d = home / ".cdl-test-tmp"
    d.mkdir(exist_ok=True)
    try:
        (d / ".secret").write_text("x")
        (d / "visible.txt").write_text("x")
        names = [e["name"] for e in srv.fs_listing(str(d))["entries"]]
        assert "visible.txt" in names and ".secret" not in names
    finally:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


# ---------------------------------------------------------------- HTTP layer

@pytest.fixture(scope="module")
def http_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("claude-root")
    (root / "projects" / "-Users-x-proj").mkdir(parents=True)
    write_session(root / "projects" / "-Users-x-proj" / "s1.jsonl",
                  [rec_user("hello"), rec_assistant([{"type": "text", "text": "hi"}])])
    srv.Handler.root = root
    srv.SERVER_TOKEN = "a" * 48
    server = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def fetch(url, method="GET", headers=None, body=None):
    req = urllib.request.Request(url, method=method, data=body,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_api_requires_token(http_server):
    code, _ = fetch(http_server + "/api/projects")
    assert code == 401
    code, body = fetch(http_server + "/api/projects",
                       headers={"X-Devtools-Token": "a" * 48})
    assert code == 200 and b"-Users-x-proj" in body


def test_post_requires_json_content_type(http_server):
    # text/plain would be a CSRF-able "simple request" — must be refused even
    # with a valid token
    code, _ = fetch(http_server + "/api/term/start", method="POST",
                    headers={"X-Devtools-Token": "a" * 48,
                             "Content-Type": "text/plain"},
                    body=b'{"kind":"shell"}')
    assert code == 403


def test_post_rejects_foreign_origin(http_server):
    code, _ = fetch(http_server + "/api/term/start", method="POST",
                    headers={"X-Devtools-Token": "a" * 48,
                             "Content-Type": "application/json",
                             "Origin": "https://evil.example"},
                    body=b'{"kind":"shell"}')
    assert code == 403


def test_launch_exchanges_token_for_cookie(http_server):
    req = urllib.request.Request(http_server + "/launch?k=" + "a" * 48)
    # don't follow the redirect: inspect it
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(req, timeout=5)
        assert False, "expected 302"
    except urllib.error.HTTPError as e:
        assert e.code == 302
        assert e.headers["Location"] == "/"
        assert "cdl=" + "a" * 48 in e.headers["Set-Cookie"]
        assert "SameSite=Strict" in e.headers["Set-Cookie"]
    code, _ = fetch(http_server + "/launch?k=wrong")
    assert code == 403


def test_cookie_authenticates(http_server):
    code, body = fetch(http_server + "/api/projects",
                       headers={"Cookie": "cdl=" + "a" * 48})
    assert code == 200 and b"-Users-x-proj" in body
    code, _ = fetch(http_server + "/api/projects",
                    headers={"Cookie": "cdl=wrong"})
    assert code == 401


def test_sensitive_filenames_blocked():
    for name in (".env", ".env.local", "credentials", "aws-credentials.json",
                 "hosts.yml", "id_rsa", "server.pem", "my_secret.yaml",
                 ".netrc", "API_TOKEN.txt", "key.p12"):
        assert srv.is_sensitive(name), name
    for name in ("notes.md", "analysis.R", "graph.html", "settings.json",
                 "data.csv", "main.tex"):
        assert not srv.is_sensitive(name), name


def test_sensitive_file_refused_over_http(http_server, tmp_path):
    home = Path.home()
    f = home / ".cdl-test-credentials.json"
    f.write_text('{"api_key":"do-not-serve"}')
    try:
        code, body = fetch(http_server + "/api/fs/file?path=" +
                           urllib.parse.quote(str(f)),
                           headers={"X-Devtools-Token": "a" * 48})
        assert code == 403 and b"secrets" in body
        assert b"do-not-serve" not in body
    finally:
        f.unlink()


def test_sensitive_marked_unviewable_in_listing():
    home = Path.home()
    d = home / ".cdl-test-listing"
    d.mkdir(exist_ok=True)
    try:
        (d / "secret_keys.json").write_text("{}")
        (d / "report.md").write_text("hi")
        entries = {e["name"]: e for e in srv.fs_listing(str(d))["entries"]}
        assert entries["secret_keys.json"]["viewable"] is False
        assert entries["secret_keys.json"]["sensitive"] is True
        assert entries["report.md"]["viewable"] is True
    finally:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def test_foreign_host_header_refused(http_server):
    # DNS-rebinding guard: a rebound page presents its own hostname
    code, _ = fetch(http_server + "/api/projects",
                    headers={"X-Devtools-Token": "a" * 48, "Host": "evil.example"})
    assert code == 403
    port = http_server.rsplit(":", 1)[1]
    code, _ = fetch(http_server + "/api/projects",
                    headers={"X-Devtools-Token": "a" * 48,
                             "Host": "127.0.0.1:" + port})
    assert code == 200


def test_log_redacts_tokens(capsys):
    h = srv.Handler.__new__(srv.Handler)
    srv.Handler.log_message(h, '"GET /api/viz?token=%s HTTP/1.1"', "a" * 48)
    err = capsys.readouterr().err
    assert "a" * 48 not in err and "[redacted]" in err
    srv.Handler.log_message(h, '"GET /launch?k=%s HTTP/1.1"', "b" * 48)
    assert "b" * 48 not in capsys.readouterr().err


def test_token_and_state_live_outside_repo():
    repo = Path(srv.__file__).resolve().parent
    assert repo not in srv.TOKEN_FILE.parents, "token must not sit in the git repo"
    assert repo not in srv.STATE_FILE.parents, "state must not sit in the git repo"
    assert "claude-devtools" in str(srv.APP_DIR)


# ---------------------------------------------------------------- CLI discovery

def test_login_path_includes_user_bin_dirs(monkeypatch):
    """An app launched from Finder/.desktop inherits a minimal PATH; the login
    PATH must still surface the usual install dirs."""
    srv._env_cache.clear()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    p = srv.login_path().split(os.pathsep)
    assert "/usr/bin" in p
    for d in p:
        assert os.path.isdir(d)          # no phantom entries
    assert len(p) == len(set(p))         # deduplicated
    srv._env_cache.clear()


def test_find_claude_prefers_explicit_override(monkeypatch, tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    srv._env_cache.clear()
    monkeypatch.setenv("CLAUDE_BIN", str(fake))
    assert srv.find_claude() == str(fake)
    srv._env_cache.clear()


def test_claude_kind_never_falls_back_to_a_shell(monkeypatch):
    """Regression: `+ claude` used to open a plain shell when the binary was
    off-PATH, instead of reporting that it wasn't found."""
    srv._env_cache["claude"] = None
    srv._env_cache["path"] = "/usr/bin:/bin"
    try:
        with pytest.raises(FileNotFoundError) as e:
            srv.start_term("claude", None)
        assert "CLAUDE_BIN" in str(e.value)
    finally:
        srv._env_cache.clear()


def test_terminal_env_carries_login_path(monkeypatch):
    srv._env_cache["claude"] = "/nonexistent/claude"
    srv._env_cache["path"] = "/usr/bin:/bin"
    try:
        captured = {}

        class FakeTerm:
            def __init__(self, argv, cwd, env=None, **kw):
                captured["argv"] = argv
                captured["env"] = env
                self.id, self.alive = "x", True

        monkeypatch.setattr(srv, "PosixTerm", FakeTerm)
        monkeypatch.setattr(srv, "WindowsTerm", FakeTerm)
        srv.TERMS.clear()
        srv.start_term("claude", None)
        assert captured["argv"][0] == "/nonexistent/claude"
        assert captured["env"]["PATH"] == "/usr/bin:/bin"
        assert captured["env"]["CLAUDE_DEVTOOLS_UI"] == "1"
    finally:
        srv.TERMS.clear()
        srv._env_cache.clear()


def test_child_env_scrubs_parent_session_markers():
    """Regression: a dashboard started from inside a Claude Code session leaked
    CLAUDE_CODE_CHILD_SESSION into terminals, which turns transcript saving OFF
    — the sessions this tool exists to display would never be recorded."""
    parent = {
        "HOME": "/Users/x", "PATH": "/usr/bin",
        "CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDE_CODE_SESSION_ID": "abc", "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_AGENT_SDK_VERSION": "1.2", "CLAUDE_PID": "42",
        "CLAUDE_EFFORT": "high", "ANTHROPIC_BASE_URL": "http://internal",
        # genuine user config that must survive
        "CLAUDE_CONFIG_DIR": "/Users/x/.claude",
        "CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_API_KEY": "sk-user",
    }
    env = srv.child_environment(parent, extra={"CLAUDE_DEVTOOLS_UI": "1"})
    for gone in ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION",
                 "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
                 "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_PID", "CLAUDE_EFFORT",
                 "ANTHROPIC_BASE_URL"):
        assert gone not in env, gone
    assert env["CLAUDE_CONFIG_DIR"] == "/Users/x/.claude"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["ANTHROPIC_API_KEY"] == "sk-user"
    assert env["HOME"] == "/Users/x"
    assert env["CLAUDE_DEVTOOLS_UI"] == "1"


def test_child_env_keeps_user_base_url_when_not_nested():
    """Outside a Claude session, ANTHROPIC_BASE_URL is the user's own setting."""
    env = srv.child_environment({"HOME": "/h", "ANTHROPIC_BASE_URL": "https://proxy"})
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy"


def test_inherited_session_markers_detection():
    assert srv.inherited_session_markers({"CLAUDECODE": "1", "HOME": "/h"}) \
        == ["CLAUDECODE"]
    assert srv.inherited_session_markers({"HOME": "/h"}) == []


# ---------------------------------------------------------------- windows backend

def test_conpty_command_line_quoting():
    import winconpty
    assert winconpty.build_command_line(["claude"]) == "claude"
    line = winconpty.build_command_line(["C:\\Program Files\\claude.exe", "/graphify"])
    assert '"C:\\Program Files\\claude.exe"' in line and "/graphify" in line
    # an argument with quotes must stay one argument
    assert winconpty.build_command_line(["x", 'a "b" c']).count('"') >= 2


def test_conpty_environment_block():
    import winconpty
    blk = winconpty.build_environment_block({"A": "1", "B": "two",
                                             "BAD=KEY": "x", "": "y"})
    text = blk.decode("utf-16-le")
    assert text.endswith("\0\0")
    entries = [e for e in text.rstrip("\0").split("\0") if e]
    assert entries == ["A=1", "B=two"]        # malformed keys dropped, sorted


def test_conpty_reports_unavailable_off_windows():
    import winconpty
    if sys.platform == "win32":               # pragma: no cover
        pytest.skip("this assertion is for non-Windows hosts")
    assert winconpty.AVAILABLE is False
    assert winconpty.unsupported_reason() == "not running on Windows"
    with pytest.raises(NotImplementedError):
        winconpty.ConPtyProcess(["cmd.exe"], None)


def test_terminal_backend_selection():
    # POSIX hosts must keep using the pty implementation
    if srv.HAS_PTY:
        assert issubclass(srv.PosixTerm, srv.Term)
        assert srv.HAS_TERMINAL is True
    assert issubclass(srv.WindowsTerm, srv.Term)
    for hook in ("_spawn", "_read", "_write", "_set_size", "_hangup",
                 "_terminate"):
        assert hasattr(srv.PosixTerm, hook) and hasattr(srv.WindowsTerm, hook)


def test_session_endpoint_roundtrip(http_server):
    code, body = fetch(http_server + "/api/session?project=-Users-x-proj&id=s1",
                       headers={"X-Devtools-Token": "a" * 48})
    assert code == 200
    data = json.loads(body)
    assert data["title"] == "hello"
    assert [e["kind"] for e in data["entries"]] == ["user", "assistant"]


# ------------------------------------------------- terminal scrollback offsets
#
# The stream hands clients an ABSOLUTE byte offset while `buf` keeps only a
# bounded tail. Conflating the two froze the terminal at exactly SCROLLBACK_CAP:
# once a caught-up client's position equalled len(buf), trimming the front kept
# len(buf) pinned at the cap, so `buf[pos:]` stayed empty forever and no further
# output ever reached the browser.

CAP = srv.SCROLLBACK_CAP


def _detached_term(buf, discarded=0):
    """A Term with its buffer state set directly — no PTY, no pump thread."""
    t = srv.Term.__new__(srv.Term)
    t.buf = bytearray(buf)
    t.discarded = discarded
    return t


class _ScriptedTerm(srv.Term):
    """Real Term with the PTY transport replaced by a scripted byte stream."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        super().__init__(["/bin/false"], str(HERE), cols=80, rows=24, env={})

    def _spawn(self, argv, cwd, env, cols, rows):
        pass

    def _read(self):
        return self._chunks.pop(0) if self._chunks else b""   # b'' == EOF

    def _write(self, data):
        pass

    def _set_size(self, cols, rows):
        pass

    def _hangup(self):
        pass

    def _terminate(self):
        pass


def test_pump_trims_scrollback_and_accounts_for_it():
    chunks = 8                               # 800 KB, comfortably past the cap
    total = chunks * 100_000
    t = _ScriptedTerm([b"A" * 100_000] * chunks)
    for _ in range(100):
        if not t.alive:
            break
        time.sleep(0.02)
    assert not t.alive, "pump never reached EOF"
    assert len(t.buf) == CAP                 # tail is bounded
    assert t.discarded == total - CAP        # ...and the loss is recorded
    assert t.produced() == total             # absolute count survives trimming


def test_slice_from_delivers_output_produced_after_a_trim():
    """The exact freeze: client caught up at the cap, then the front trims."""
    t = _detached_term(b"B" * CAP, discarded=100)   # 100 bytes aged out
    chunk, pos = t.slice_from(CAP)                  # client had consumed CAP
    assert chunk == b"B" * 100, "post-trim output was not delivered"
    assert pos == CAP + 100
    # and it does not re-deliver on the next poll
    assert t.slice_from(pos) == (b"", CAP + 100)


def test_slice_from_clamps_a_client_that_fell_behind():
    t = _detached_term(b"C" * 1000, discarded=5000)
    chunk, pos = t.slice_from(0)          # asking for bytes that aged out
    assert chunk == b"C" * 1000           # skip the gap, do not stall
    assert pos == 6000


def test_slice_from_tolerates_a_position_past_the_end():
    t = _detached_term(b"D" * 10, discarded=0)
    assert t.slice_from(999) == (b"", 10)          # no negative index, no crash


def test_slice_from_reassembles_the_stream_without_loss():
    t = _detached_term(b"", 0)
    pos, seen = 0, bytearray()
    for i in range(20):
        t.buf.extend(bytes([65 + i]) * 50)
        chunk, pos = t.slice_from(pos)
        seen.extend(chunk)
    assert bytes(seen) == bytes(t.buf)


# ---------------------------------------------------------------- plan pane

@pytest.fixture
def plan_project(tmp_path, monkeypatch):
    """A project directory that looks like $HOME so safe_home_path accepts it."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    return proj


def test_plan_parses_checkboxes_and_skips_comments_and_fences():
    items, done, total = srv.parse_plan_text(
        "# Plan\n"
        "<!-- a note\n     spanning two lines -->\n"
        "Status: DRAFT\n"
        "## Steps\n"
        "- [x] first\n"
        "- [ ] second\n"
        "  - [~] nested\n"
        "1. [ ] numbered\n"
        "```\n- [ ] inside a fence\n```\n")
    kinds = [(i["kind"], i.get("state"), i["text"]) for i in items]
    assert ("task", "done", "first") in kinds
    assert ("task", "doing", "nested") in kinds
    assert ("head", None, "Plan") in kinds
    assert ("text", None, "Status: DRAFT") in kinds
    assert not any("fence" in i["text"] for i in items)     # code blocks ignored
    assert not any("spanning" in i["text"] for i in items)  # comments ignored
    assert (done, total) == (1, 4)


def test_plan_discovery_prefers_the_live_plan_then_dated_plans(plan_project):
    root = plan_project
    (root / "PLAN.md").write_text("- [ ] root\n")
    plans = root / "quality_reports" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-01-01_old.md").write_text("- [ ] old\n")
    # no .claude/plan.md yet: the dated plan wins over PLAN.md
    assert srv.plan_candidates(root)[0].name == "2026-01-01_old.md"
    (root / ".claude" / "plan.md").write_text("- [ ] live\n")
    cands = srv.plan_candidates(root)
    assert cands[0].name == "plan.md"
    assert [c.name for c in cands[1:]] == ["2026-01-01_old.md", "PLAN.md"]


def test_plan_toggle_rewrites_only_the_marker(plan_project):
    f = plan_project / ".claude" / "plan.md"
    f.write_text("# Plan\n- [ ] alpha\n- [ ] beta\n")
    out = srv.plan_toggle(plan_project, str(f), 1, "alpha", "done")
    assert f.read_text() == "# Plan\n- [x] alpha\n- [ ] beta\n"
    assert (out["done"], out["total"]) == (1, 2)
    srv.plan_toggle(plan_project, str(f), 1, "alpha", "open")
    assert f.read_text() == "# Plan\n- [ ] alpha\n- [ ] beta\n"


def test_plan_toggle_refuses_files_outside_the_project(plan_project):
    victim = Path.home() / "secrets.md"
    victim.write_text("- [ ] do not touch\n")
    with pytest.raises(ValueError):
        srv.plan_toggle(plan_project, str(victim), 0, None, "done")
    assert victim.read_text() == "- [ ] do not touch\n"


def test_plan_toggle_refuses_when_the_line_moved(plan_project):
    f = plan_project / ".claude" / "plan.md"
    f.write_text("- [ ] alpha\n- [ ] beta\n")
    with pytest.raises(ValueError):                 # stale text from the client
        srv.plan_toggle(plan_project, str(f), 0, "beta", "done")
    with pytest.raises(ValueError):                 # not a checkbox line
        f.write_text("plain prose\n")
        srv.plan_toggle(plan_project, str(f), 0, None, "done")
    with pytest.raises(ValueError):                 # out of range
        srv.plan_toggle(plan_project, str(f), 99, None, "done")


def test_plan_toggle_preserves_crlf_and_indentation(plan_project):
    f = plan_project / ".claude" / "plan.md"
    f.write_bytes(b"# Plan\r\n  - [ ] indented\r\n")
    srv.plan_toggle(plan_project, str(f), 1, "indented", "done")
    assert f.read_bytes() == b"# Plan\r\n  - [x] indented\r\n"


# ------------------------------------------------------- end-of-session improve

def test_mangle_cwd_matches_claude_code_project_dirs():
    assert srv.mangle_cwd("/Users/x/Docs/Cours MACRO 1/app") == \
        "-Users-x-Docs-Cours-MACRO-1-app"
    assert srv.mangle_cwd("/a/b.c") == "-a-b-c"


def test_improve_never_runs_inside_its_own_retrospective(monkeypatch, tmp_path):
    monkeypatch.setenv("CDL_IMPROVE_RUN", "1")
    monkeypatch.setattr(srv, "find_claude", lambda: "/bin/false")
    assert srv.spawn_improve(str(tmp_path)) is None


def test_improve_respects_the_kill_switch(monkeypatch, tmp_path):
    monkeypatch.delenv("CDL_IMPROVE_RUN", raising=False)
    monkeypatch.setenv("CDL_IMPROVE", "0")
    assert srv.improve_enabled() is False
    assert srv.spawn_improve(str(tmp_path)) is None


def test_improve_skips_short_sessions(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "CLAUDE_ROOT", tmp_path)
    cwd = "/tmp/tiny"
    d = tmp_path / "projects" / srv.mangle_cwd(cwd)
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text("{}\n")                 # far under the threshold
    assert srv.newest_transcript(cwd) is None
    (d / "s.jsonl").write_text("x" * (srv.IMPROVE_MIN_TRANSCRIPT + 1))
    assert srv.newest_transcript(cwd).name == "s.jsonl"


# ---------------------------------------------------------------- config pane

def test_front_matter_reads_name_and_description(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("---\nname: coder\ndescription: Writes code.\n"
                 "tools: Read, Grep\n---\n\n# body\n")
    fm = srv.read_frontmatter(f)
    assert fm["name"] == "coder" and fm["description"] == "Writes code."
    f.write_text("no front matter\n# Heading\n")
    assert srv.read_frontmatter(f) == {}
    assert srv.first_heading(f) == "Heading"


def test_config_inventory_separates_resident_from_on_demand(tmp_path):
    root = tmp_path / ".claude"
    (root / "rules").mkdir(parents=True)
    (root / "agents").mkdir()
    (root / "skills" / "graphify").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("x" * 4000)
    (root / "rules" / "workflow.md").write_text("# Workflow\n" + "y" * 2000)
    (root / "agents" / "coder.md").write_text(
        "---\nname: coder\ndescription: Writes code.\n---\n" + "z" * 8000)
    (root / "skills" / "graphify" / "SKILL.md").write_text(
        "---\nname: graphify\ndescription: Graphs things.\n---\n" + "w" * 8000)

    inv = srv.config_inventory(root)
    by = {g["key"]: g for g in inv["groups"]}
    assert [i["name"] for i in by["skills"]["items"]] == ["graphify"]
    assert by["memory"]["items"][1]["description"] == "Workflow"   # heading fallback

    # instruction files cost their whole body every turn ...
    claude_md = by["memory"]["items"][0]
    assert claude_md["resident"] == claude_md["tokens"] > 900
    # ... an agent costs only its description until it is dispatched
    coder = by["agents"]["items"][0]
    assert coder["resident"] < 10 < coder["tokens"]
    assert inv["ondemand"] > inv["resident"]


def test_config_inventory_flags_hooks_nothing_points_at(tmp_path):
    root = tmp_path / ".claude"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "orphan.sh").write_text("#!/bin/sh\n")
    (root / "hooks" / "live.sh").write_text("#!/bin/sh\n")
    (root / "settings.json").write_text(json.dumps({"hooks": {"SessionEnd": [
        {"hooks": [{"type": "command",
                    "command": '"' + str(root / "hooks" / "live.sh") + '"'}]}]}}))
    # a settings.local.json without hooks must not erase the registration
    (root / "settings.local.json").write_text(json.dumps({"permissions": {}}))

    hooks = {i["name"]: i for g in srv.config_inventory(root)["groups"]
             if g["key"] == "hooks" for i in g["items"]}
    assert hooks["live"]["event"] == "SessionEnd"
    assert hooks["orphan"]["event"] is None


def test_config_view_skips_a_project_claude_dir_with_nothing_in_it(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(srv, "CLAUDE_ROOT", tmp_path / ".claude")
    (tmp_path / ".claude").mkdir()
    proj = tmp_path / "proj" / ".claude"
    proj.mkdir(parents=True)
    (proj / "plan.md").write_text("- [ ] a\n")        # not a config component
    assert srv.config_view(str(tmp_path / "proj"))["project"] is None
    (proj / "agents").mkdir()
    (proj / "agents" / "x.md").write_text("---\nname: x\ndescription: d\n---\n")
    assert srv.config_view(str(tmp_path / "proj"))["project"]["count"] == 1


def test_mcp_servers_come_from_claude_json_not_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".claude.json").write_text(json.dumps({
        "mcpServers": {"garmin": {"type": "stdio", "command": "x",
                                  "env": {"TOKEN": "sekrit"}}},
        "projects": {str(tmp_path / "proj"): {
            "mcpServers": {"datagouv": {"type": "http", "url": "https://x"}}}}}))
    assert srv.mcp_servers(tmp_path / ".claude") == {"garmin": "stdio"}
    assert srv.mcp_servers(None, scope=tmp_path / "proj") == {"datagouv": "http"}


def test_config_reports_project_mcp_even_without_a_project_claude_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(srv, "CLAUDE_ROOT", tmp_path / ".claude")
    (tmp_path / ".claude").mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / ".claude.json").write_text(json.dumps({"projects": {
        str(proj): {"mcpServers": {"datagouv": {"type": "http"}}}}}))
    inv = srv.config_view(str(proj))["project"]
    assert [i["name"] for g in inv["groups"] for i in g["items"]] == ["datagouv"]


def test_mcp_inventory_never_leaks_args_or_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(srv, "CLAUDE_ROOT", tmp_path / ".claude")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {
        "s": {"type": "stdio", "command": "/bin/x",
              "args": ["--key", "AKIA-DO-NOT-LEAK"],
              "env": {"API_KEY": "sk-do-not-leak"}}}}))
    blob = json.dumps(srv.config_view())
    assert "DO-NOT-LEAK" not in blob and "sk-do-not-leak" not in blob
    assert "/bin/x" not in blob


def test_empty_retrospectives_are_not_offered_as_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "IMPROVE_DIR", tmp_path)
    d = tmp_path / "proj"
    d.mkdir()
    stale = d / "2026-01-01_0900.md"
    stale.write_text("# Retrospective\n")                 # header only
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    real = d / "2026-01-02_0900.md"
    real.write_text("# Retrospective\n\n" + "finding. " * 60)
    names = [r["name"] for r in srv.improve_reports("proj")["reports"]]
    assert names == [real.name]

    fresh = d / "2026-01-03_0900.md"                      # still being written
    fresh.write_text("# Retrospective\n")
    assert fresh.name in [r["name"] for r in srv.improve_reports("proj")["reports"]]


def test_hook_name_resolves_through_an_interpreter(tmp_path):
    assert srv.hook_script_name(["python3", "/a/pre-compact.py"]) == "pre-compact.py"
    assert srv.hook_script_name(["/a/protect-files.sh"]) == "protect-files.sh"
    assert srv.hook_script_name(["node", "--enable-source-maps", "/a/x.mjs"]) == "x.mjs"
    assert srv.hook_script_name(["some-binary"]) == "some-binary"


def test_helper_scripts_in_hooks_are_not_flagged_as_orphans(tmp_path):
    root = tmp_path / ".claude"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "lib.sh").write_text("#!/bin/sh\n# a linter other hooks call\n")
    (root / "hooks" / "guard.sh").write_text(
        "#!/bin/sh\n# reads the payload\ncat | grep tool_input\n")
    (root / "hooks" / "declared.py").write_text('"""Hook Event: PreCompact"""\n')
    (root / "settings.json").write_text("{}")

    got = {i["name"]: i for g in srv.config_inventory(root)["groups"]
           if g["key"] == "hooks" for i in g["items"]}
    assert got["lib"]["orphan"] is False          # helper, not a hook
    assert got["guard"]["orphan"] is True         # consumes a hook payload
    assert got["declared"]["orphan"] is True
    assert "PreCompact" in got["declared"]["description"]


def test_config_inventory_counts_nested_rules(tmp_path):
    """Claude Code loads rules/ recursively, so a subfolder is resident too."""
    root = tmp_path / ".claude"
    (root / "rules" / "pipeline").mkdir(parents=True)
    (root / "rules" / "top.md").write_text("# Top\n" + "x" * 400)
    (root / "rules" / "pipeline" / "workflow.md").write_text("# Flow\n" + "y" * 4000)
    mem = [g for g in srv.config_inventory(root)["groups"] if g["key"] == "memory"][0]
    names = {i["name"]: i for i in mem["items"]}
    assert set(names) == {"top", "pipeline/workflow"}
    assert names["pipeline/workflow"]["always"] is True
    assert names["pipeline/workflow"]["resident"] > names["top"]["resident"]


# ---------------------------------------------------------------- figure review

@pytest.fixture
def figure(plan_project):
    img = plan_project / "fig.png"
    img.write_bytes(b"\x89PNG fake v1")
    return img


def test_review_roundtrip_clamps_and_numbers(figure):
    d = srv.review_write(str(figure), [
        {"type": "point", "x": 0.25, "y": 1.7, "text": "bigger label"},
        {"type": "region", "x": -1, "y": 0.1, "w": 0.5, "h": 0.2,
         "status": "bogus", "text": "drop this band"}], 0)
    assert d["revision"] == 1 and not d["stale"]
    a, b = d["comments"]
    assert (a["n"], a["y"], a["status"]) == (1, 1.0, "open")
    assert (b["n"], b["x"], b["w"], b["status"]) == (2, 0.0, 0.5, "open")
    saved = json.loads((figure.parent / ".review" / "fig.png.json").read_text())
    assert saved["figure"]["content_hash"] == d["content_hash"]
    assert figure.read_bytes() == b"\x89PNG fake v1"          # never rewritten


def test_review_flags_a_regenerated_figure(figure):
    srv.review_write(str(figure), [{"type": "point", "x": .5, "y": .5}], 0)
    figure.write_bytes(b"\x89PNG fake v2")
    assert srv.review_read(str(figure))["stale"] is True


def test_review_refuses_a_stale_revision(figure):
    srv.review_write(str(figure), [], 0)
    with pytest.raises(ValueError, match="changed on disk"):
        srv.review_write(str(figure), [], 0)


def test_review_rejects_bad_input(figure, plan_project):
    with pytest.raises(ValueError):
        srv.review_write(str(figure), [{"type": "arrow", "x": 0, "y": 0}], 0)
    with pytest.raises(ValueError):
        srv.review_write(str(figure), [{"type": "point", "x": "a", "y": 0}], 0)
    notes = plan_project / "notes.md"
    notes.write_text("x")
    with pytest.raises(ValueError):
        srv.review_read(str(notes))                           # not an image
    with pytest.raises(ValueError):
        srv.review_read("/etc/hosts.png")                     # outside $HOME
