"""Run pipeline stages off the UI thread and stream their progress to Qt."""

from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from qtpy.QtCore import QObject, QThread, Signal

from kipi.progress import Cancelled, ProgressReporter


class QtProgress(QObject, ProgressReporter):
    """ProgressReporter whose sinks emit Qt signals (safe to call from any thread)."""

    sig_message = Signal(str, int)
    sig_stage = Signal(str)
    sig_fraction = Signal(float)   # < 0 means indeterminate

    def __init__(self):
        QObject.__init__(self)
        ProgressReporter.__init__(self)

    def message(self, text: str, level: int = logging.INFO):
        logging.getLogger("kipi").log(level, text)
        self.sig_message.emit(text, level)

    def stage(self, name: str):
        self.sig_stage.emit(name)
        self.message(f"== {name}")

    def fraction(self, value):
        self.sig_fraction.emit(-1.0 if value is None else float(value))


class Task(QThread):
    """Runs ``fn(progress)`` in a thread. Emits ``done(result)`` or ``failed(text)``."""

    done = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, fn: Callable[[ProgressReporter], Any], progress: QtProgress, parent=None):
        super().__init__(parent)
        self._fn = fn
        self.progress = progress

    def run(self):
        try:
            result = self._fn(self.progress)
        except Cancelled:
            self.progress.warn("Cancelled")
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001 - surface every failure in the log
            self.progress.error(f"{type(e).__name__}: {e}")
            self.failed.emit(traceback.format_exc())
        else:
            self.done.emit(result)
