"""KiPI's window comes to the front when it opens, also once the splash closes (#112)."""

import os
import sys

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import Qt  # noqa: E402
from qtpy.QtWidgets import QApplication, QMainWindow  # noqa: E402

from kipi.gui import app as gui_app, single_instance as si, splash  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_bring_to_front_shows_and_restores(app):
    win = QMainWindow()
    win.showMinimized()
    si.bring_to_front(win)
    assert win.isVisible() and not win.isMinimized()
    win.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows foreground rules")
def test_win32_foreground_does_not_raise(app):
    win = QMainWindow()
    win.show()
    si._win32_foreground(int(win.winId()))
    win.close()


def test_closing_the_splash_refocuses_the_window(app, monkeypatch):
    s = splash.Splash()
    calls = []
    s.closed.connect(lambda: calls.append(1))
    s.close()
    assert calls == [1]
    raised = []
    monkeypatch.setattr(si, "bring_to_front", lambda w: raised.append(w))
    win = QMainWindow()
    monkeypatch.setattr(app, "applicationState", lambda: Qt.ApplicationActive)
    gui_app._refocus(app, win)
    assert raised == [win]
    monkeypatch.setattr(app, "applicationState", lambda: Qt.ApplicationInactive)
    gui_app._refocus(app, win)              # the user went elsewhere: leave them be
    assert raised == [win]
