"""Regression tests for claude-devtools-lite's server.

Run:  python3 -m pytest tests/ -q
Covers the JSONL parsing invariants (usage dedup, tool pairing, sidechains,
compaction), the 5h-block reconstruction, path-safety guards, and the HTTP
auth/CSRF layer against a live server on an ephemeral port.
"""
import importlib.util
import json
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


def test_session_endpoint_roundtrip(http_server):
    code, body = fetch(http_server + "/api/session?project=-Users-x-proj&id=s1",
                       headers={"X-Devtools-Token": "a" * 48})
    assert code == 200
    data = json.loads(body)
    assert data["title"] == "hello"
    assert [e["kind"] for e in data["entries"]] == ["user", "assistant"]
