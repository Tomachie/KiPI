"""The plugin must never block writing to a pipe its parent (KiCad) does not drain."""

import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_with_unread_pipes(code: str, tmp_path: Path, timeout: float = 60.0) -> str:
    script = tmp_path / "child.py"
    status = tmp_path / "status.txt"
    script.write_text(textwrap.dedent(code), encoding="utf-8")
    # stdout/stderr are pipes that nobody reads, like KiCad's when it is busy.
    p = subprocess.Popen([sys.executable, str(script), str(status)], cwd=ROOT,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    t0 = time.time()
    while p.poll() is None and time.time() - t0 < timeout:
        time.sleep(0.2)
    hung = p.poll() is None
    if hung:
        p.kill()
    assert not hung, "child blocked writing to an undrained pipe"
    if not status.exists():
        err = p.stderr.read().decode(errors="replace")[-2000:]
        raise AssertionError(f"child failed (exit {p.returncode}): {err}")
    return status.read_text()


def test_logging_does_not_echo_into_a_pipe(tmp_path):
    out = _run_with_unread_pipes("""
        import sys, logging
        sys.path.insert(0, ".")
        from kipi.gui import app
        app.setup_logging()                      # stderr is a pipe, not a terminal
        for i in range(20000):
            logging.getLogger("kipi").info("progress line %d %s", i, "x" * 80)
        echo = [h for h in logging.getLogger().handlers if type(h) is logging.StreamHandler]
        open(sys.argv[1], "w").write(f"done {len(echo)}")
    """, tmp_path)
    assert out == "done 0"


def test_detached_streams_absorb_native_and_python_writes(tmp_path):
    out = _run_with_unread_pipes("""
        import os, sys, logging
        sys.path.insert(0, ".")
        from kipi.gui import app
        app.setup_logging(console=True)          # even with console echo forced on
        path = app.detach_std_streams()
        for i in range(5000):
            logging.getLogger("kipi").info("line %d %s", i, "y" * 80)
            print("print", i)
            os.write(2, b"native stderr" + bytes([10]))
            os.write(1, b"native stdout" + bytes([10]))
        open(sys.argv[1], "w").write("done " + str(os.path.getsize(path) > 100_000))
    """, tmp_path)
    assert out == "done True"
