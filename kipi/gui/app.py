"""Qt application bootstrap shared by the KiCad plugin entry and the standalone CLI."""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

LOG_PATH = Path(tempfile.gettempdir()) / "kipi.log"


def setup_logging(level=logging.INFO) -> Path:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"),
                  logging.StreamHandler(sys.stderr)] if sys.stderr else
                 [logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")],
        force=True)
    return LOG_PATH


def run(kicad=None, board=None, snapshot=None) -> int:
    os.environ.setdefault("QT_API", "pyside6")
    from qtpy.QtWidgets import QApplication

    from kipi.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("KiPI")
    app.setStyle("Fusion")
    win = MainWindow(kicad=kicad, board=board, snapshot=snapshot)
    win.show()
    return app.exec()
