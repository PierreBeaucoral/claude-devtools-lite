#!/usr/bin/env python3
"""Windows self-test for the ConPTY terminal backend.

Run this ON WINDOWS to prove the embedded terminal works on your machine:

    python tools\\selftest_windows.py

It spawns a real pseudo-console, types a command, reads the output back,
resizes, and closes the session — printing PASS/FAIL for each step. Exit
code 0 means the terminal pane will work in the dashboard.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import winconpty  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main():
    print(f"platform={sys.platform}  python={sys.version.split()[0]}")
    reason = winconpty.unsupported_reason()
    check("ConPTY available", reason is None, reason or "kernel32.CreatePseudoConsole present")
    if reason:
        print("\nThe dashboard will run without the terminal pane.")
        return 1

    shell = os.environ.get("COMSPEC", "cmd.exe")
    try:
        p = winconpty.ConPtyProcess([shell], os.getcwd(),
                                    {"CLAUDE_DEVTOOLS_UI": "1"}, 100, 30)
        check("spawn pseudo-console", True, f"pid={p.pid} ({shell})")
    except Exception as e:
        check("spawn pseudo-console", False, repr(e))
        return 1

    try:
        marker = "CONPTY-SELFTEST-OK"
        p.write(f"echo {marker}\r\n".encode())
        deadline, out = time.time() + 10, b""
        while time.time() < deadline and marker.encode() not in out.replace(b"\r\n", b""):
            chunk = p.read()
            if not chunk:
                break
            out += chunk
        text = out.decode("utf-8", "replace")
        check("write command + read output", marker in text.replace("\r\n", ""),
              f"{len(out)} bytes read")

        p.set_size(120, 40)
        check("resize pseudo-console", True, "120x40")
        check("process alive", p.alive())

        p.write(b"exit\r\n")
        deadline = time.time() + 10
        while time.time() < deadline and p.alive():
            p.read()
            time.sleep(0.1)
        check("clean exit on 'exit'", not p.alive())
    finally:
        p.request_close()
        p.close_handles()

    ok = all(results)
    print("\n" + ("ALL CHECKS PASSED — the terminal pane will work."
                  if ok else "SOME CHECKS FAILED — see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
