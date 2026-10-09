"""Pasting a multi-line request into the real interactive prompt, via a pty.

A pty is the only honest way to test this: the point is how input() and the
terminal deliver a paste (all lines at once) versus typing. The child runs the
real `input` with the real paste-draining; the parent plays the terminal."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

pty = pytest.importorskip("pty")

ROOT = Path(__file__).resolve().parent.parent
CHILD = (
    "import json; from src import cli;"
    "t = cli._read_request(input, cli._drain_pasted_lines);"
    "print('\\nRESULT:' + json.dumps(t), flush=True)"
)

SPEC = """Reviewers see a LOW / MEDIUM / HIGH complexity badge on documents, but can't tell why.
Analysts also want one more scoring factor: documents whose guidelines mix several code systems (CPT, HCPCS, ICD, ICD_PROC).
Requirements
1. New factor: code type diversity (backend)
Count the distinct code systems used across a document's guidelines.

2. Show score and breakdown (web)
Hovering the complexity badge shows a tooltip with the score (e.g. Score 7 / 9).
Acceptance criteria
not doneNewly processed documents store 6 entries in their complexity breakdown."""


def run_in_pty(keystrokes: str, *, chunks: list[str] | None = None, timeout=15):
    """Run the child under a pty; send `keystrokes` as ONE write (a paste), or
    each of `chunks` with a pause between (typing). Returns the parsed result."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD], stdin=slave, stdout=slave, stderr=slave,
        cwd=ROOT, close_fds=True, env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    os.close(slave)
    out = b""

    def read_until(marker: bytes, deadline: float) -> None:
        nonlocal out
        while marker not in out and time.time() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk

    try:
        read_until(b"empty line to quit", time.time() + timeout)        # wait for the prompt
        if chunks is None:
            os.write(master, keystrokes.encode())
        else:
            for c in chunks:
                os.write(master, c.encode())
                time.sleep(0.8)                                         # slower than the paste grace
        read_until(b"RESULT:", time.time() + timeout)
        # Read the whole result line BEFORE the child exits: on macOS the pty
        # drops unread output when the slave side closes, truncating long lines.
        deadline = time.time() + timeout
        while b"\n" not in out[out.rfind(b"RESULT:"):] and time.time() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                out += os.read(master, 65536)
        proc.wait(timeout=timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
    line = next(ln for ln in out.decode(errors="replace").splitlines() if ln.startswith("RESULT:"))
    return json.loads(line[len("RESULT:"):])


def test_pasting_a_multiline_spec_is_one_request():
    got = run_in_pty(SPEC + "\n")           # a paste ends with the trailing newline of its last line
    assert got == SPEC                      # every line, blank line kept, nothing dropped or split


def test_typing_one_line_just_works():
    assert run_in_pty("add a coupon field\n") == "add a coupon field"


def test_empty_line_quits():
    assert run_in_pty("\n") is None


def test_paste_followed_by_a_dot_is_still_accepted():
    assert run_in_pty("line one\nline two\n.\n") == "line one\nline two"


def test_a_long_real_world_spec_survives_a_paste():
    spec = "\n".join(f"{i}. line {i} of a long requirement with some detail, CPT/HCPCS/ICD" for i in range(1, 40))
    assert len(spec) > 2000                      # well past a 1024-byte terminal line
    assert run_in_pty(spec + "\n") == spec
