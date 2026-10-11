"""Qt application bootstrap shared by the KiCad plugin entry and the standalone CLI."""

from __future__ import annotations

import faulthandler
import logging
import os
import shutil
import sys
import tempfile
import threading
import traceback
from pathlib import Path

LOG_DIR = Path(tempfile.gettempdir())
LOG_PATH = LOG_DIR / "kipi.log"
PREV_LOG_PATH = LOG_DIR / "kipi.prev.log"
CRASH_PATH = LOG_DIR / "kipi_crash.log"

log = logging.getLogger("kipi")
_crash_file = None   # kept open for faulthandler for the life of the process


def setup_logging(level=logging.INFO) -> Path:
    """Log to %TEMP%/kipi.log (the previous run is kept as kipi.prev.log).

    Inside KiCad the plugin has no console, so this file, and kipi_crash.log for hard
    crashes, are the only record of what went wrong.
    """
    global _crash_file
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size:
            shutil.copyfile(LOG_PATH, PREV_LOG_PATH)
    except OSError:
        pass
    handlers = [logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers, force=True)
    try:
        _crash_file = open(CRASH_PATH, "w", encoding="utf-8")
        faulthandler.enable(file=_crash_file, all_threads=True)
    except OSError:
        pass
    _install_excepthooks()
    return LOG_PATH


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
