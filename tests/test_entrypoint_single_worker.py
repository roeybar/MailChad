"""The terminal entrypoint pins one worker for its duplicate-send guard."""
from __future__ import annotations
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "deploy" / "entrypoint.sh"

def test_entrypoint_pins_one_worker():
    # This fails if --workers 1 is removed or changed to --workers 2.
    lines = ENTRYPOINT.read_text().splitlines()
    exec_line = [line for line in lines if line.strip()][-1]
    assert exec_line.startswith("exec uvicorn mailman.terminal.main:app")
    assert "--workers 1" in exec_line
    commands = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    assert re.findall(r"--workers(?:\s+|=)(\S+)", commands) == ["1"]

def test_entrypoint_explains_worker_pin():
    # This fails if the comment directly above the exec line is deleted.
    lines = ENTRYPOINT.read_text().splitlines()
    exec_index = max(i for i, line in enumerate(lines) if line.strip())
    comment = lines[exec_index - 1]
    assert comment.startswith("#")
    assert "One worker only" in comment and "duplicate-send guard" in comment

def test_entrypoint_is_lf():
    # This fails if the entrypoint uses CRLF line endings.
    assert b"\r" not in ENTRYPOINT.read_bytes()
