"""Qt application bootstrap shared by the KiCad plugin entry and the standalone CLI."""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

LOG_DIR = Path(tempfile.gettempdir()) / "kipi"
LOG_PATH = LOG_DIR / "kipi.log"          # replaced per session by setup_logging()
KEEP_SESSIONS = 15

log = logging.getLogger("kipi")
_crash_file = None   # kept open for faulthandler for the life of the process


def setup_logging(level=logging.INFO) -> Path:
    """Write this session's log to %TEMP%/kipi/kipi-<time>-<pid>.log.

    Inside KiCad the plugin has no console, so these files are the only record of
    what went wrong. Each session gets its own log and its own crash/hang file
    (``*.crash.log``), so one run never overwrites another; the newest
    KEEP_SESSIONS sessions are kept.
    """
    global _crash_file, LOG_PATH
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    LOG_PATH = LOG_DIR / f"kipi-{stamp}-{os.getpid()}.log"
    crash_path = LOG_PATH.with_suffix(".crash.log")
    _prune_old_logs()
    handlers = [logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(threadName)s: "
                        "%(message)s", handlers=handlers, force=True)
    try:
        _crash_file = open(crash_path, "w", encoding="utf-8")
        _crash_file.write("# KiPI native crash / hang stack dumps for this session.\n"
                          "# 'Windows fatal exception: code 0x8001010d' entries are harmless COM\n"
                          "# notices that Windows raises and Python reports; they are not crashes.\n")
        _crash_file.flush()
        faulthandler.enable(file=_crash_file, all_threads=True)
    except OSError:
        pass
    _install_excepthooks()
    log.info("KiPI session log %s (Python %s)", LOG_PATH, sys.version.split()[0])
    return LOG_PATH


def _prune_old_logs():
    try:
        logs = sorted(LOG_DIR.glob("kipi-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        sessions = [p for p in logs if not p.name.endswith(".crash.log")]
        for old in sessions[KEEP_SESSIONS:]:
            old.unlink(missing_ok=True)
            old.with_suffix(".crash.log").unlink(missing_ok=True)
    except OSError:
        pass


def watchdog_start(seconds: float = 60.0):
    """Dump every thread's stack to the crash log every ``seconds`` until stopped.

    Called when a background task starts, so a task that never finishes leaves a
    record of exactly where it is stuck.
    """
    if _crash_file is not None:
        faulthandler.dump_traceback_later(seconds, repeat=True, file=_crash_file)


def watchdog_stop():
    faulthandler.cancel_dump_traceback_later()


def _install_excepthooks():
    def report(exc_type, exc, tb, where="UI thread"):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.critical("Uncaught exception in %s:\n%s", where, text)
        if where != "UI thread":
            return
        try:
            from qtpy.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is not None:
                QMessageBox.critical(None, "KiPI error",
                                     f"{exc_type.__name__}: {exc}\n\n"
                                     f"Details were written to {LOG_PATH}")
        except Exception:  # noqa: BLE001 - never let error reporting raise
            pass

    sys.excepthook = report
    threading.excepthook = lambda a: report(a.exc_type, a.exc_value, a.exc_traceback,
                                            f"thread {a.thread.name if a.thread else '?'}")


def run(kicad=None, board=None, snapshot=None) -> int:
    os.environ.setdefault("QT_API", "pyside6")
    from qtpy.QtWidgets import QApplication

    from kipi.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("KiPI")
    app.setStyle("Fusion")
    win = MainWindow(kicad=kicad, board=board, snapshot=snapshot)
    win.show()
    code = app.exec()
    log.info("KiPI closed normally")
    return code
