"""Allow one KiPI at a time per user, as a plugin and standalone each.

The first KiPI holds a lock file and listens on a local socket (a named pipe on Windows).
A second one finds the lock taken, asks the first to bring its window to the front, and
exits; if the first does not answer, it says that KiPI is already open instead.

The lock is a :class:`QLockFile`, which records the owner's PID, so a KiPI that crashed
or was killed does not lock the next one out. Like the app identity, this is best effort:
if the lock file cannot be written at all, KiPI starts anyway.

The plugin and the standalone app have a lock each: a standalone KiPI that is open must
not answer KiCad's plugin button for the board in the PCB editor, nor the other way round.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

os.environ.setdefault("QT_API", "pyside6")

from qtpy.QtCore import QLockFile  # noqa: E402

from kipi.host import Host, current as current_host  # noqa: E402

log = logging.getLogger(__name__)

ACTIVATE = b"activate\n"
ACK = b"ok\n"
TIMEOUT_MS = 2000


def _user() -> str:
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name (e.g. a container)
        name = ""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "user"


def _name() -> str:
    """The plugin keeps the name it always had; the standalone app has its own."""
    mode = "" if current_host() is Host.PLUGIN else "standalone-"
    return f"kipi-{mode}{_user()}"


def lock_path() -> Path:
    return Path(tempfile.gettempdir()) / f"{_name()}.lock"


def server_name() -> str:
    return _name()


class InstanceGuard:
    """Held by the KiPI that is running; released when it closes."""

    def __init__(self, lock: Optional[QLockFile]):
        self._lock = lock
        self._server = None

    @property
    def locked(self) -> bool:
        return self._lock is not None

    def listen(self, on_activate: Callable[[], None]) -> bool:
        """Answer later KiPIs by calling ``on_activate`` (needs the QApplication)."""
        if self._lock is None:
            return False
        try:
            from qtpy.QtNetwork import QLocalServer
        except ImportError:
            log.info("QtNetwork is not available; a second KiPI will only be told to close")
            return False
        server = QLocalServer()
        server.setSocketOptions(QLocalServer.UserAccessOption)
        # We hold the lock, so a socket left by a KiPI that crashed is stale (Unix only).
        QLocalServer.removeServer(server_name())
        if not server.listen(server_name()):
            log.warning("Could not listen for other KiPI instances: %s", server.errorString())
            return False

        def accept():
            while server.hasPendingConnections():
                sock = server.nextPendingConnection()
                sock.disconnected.connect(sock.deleteLater)
                sock.readyRead.connect(lambda s=sock: answer(s))

        def answer(sock):
            if bytes(sock.readAll()).startswith(ACTIVATE.strip()):
                log.info("Another KiPI was started; bringing this one to the front")
                try:
                    on_activate()
                finally:
                    sock.write(ACK)
                    sock.flush()
                    sock.disconnectFromServer()

        server.newConnection.connect(accept)
        self._server = server
        return True

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None


def acquire(path: Optional[Path] = None) -> Optional[InstanceGuard]:
    """The guard if this is the only KiPI, or None if another one holds the lock."""
    lock = QLockFile(str(path or lock_path()))
    lock.setStaleLockTime(0)        # only a dead owner makes the lock stale, never its age
    if lock.tryLock(0):
        return InstanceGuard(lock)
    if lock.error() == QLockFile.LockFailedError:
        return None
    # Not a running KiPI: the temp folder is read-only, full, or similar. Run unguarded.
    log.warning("Could not create the single-instance lock %s (error %s); "
                "not checking for another KiPI", path or lock_path(), lock.error())
    return InstanceGuard(None)


def running_pid(path: Optional[Path] = None) -> Optional[int]:
    try:
        pid, _host, _app = QLockFile(str(path or lock_path())).getLockInfo()
        return int(pid) or None
    except Exception:  # noqa: BLE001 - older bindings return a different tuple
        return None


def activate_running(timeout_ms: int = TIMEOUT_MS) -> bool:
    """Ask the running KiPI to come to the front; True if it said it did."""
    try:
        from qtpy.QtNetwork import QLocalSocket
    except ImportError:
        return False
    if sys.platform == "win32":
        # Windows only lets the foreground process hand focus on. KiCad started us from a
        # click, so we may; pass that right to the running KiPI.
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)    # ASFW_ANY
        except Exception:  # noqa: BLE001
            pass
    sock = QLocalSocket()
    sock.connectToServer(server_name())
    if not sock.waitForConnected(timeout_ms):
        log.info("The running KiPI did not answer: %s", sock.errorString())
        return False
    sock.write(ACTIVATE)
    # On Unix flush() sends everything at once, and waitForBytesWritten() then returns
    # False for want of anything left to write; its result says nothing about delivery.
    sock.flush()
    sock.waitForBytesWritten(timeout_ms)
    reply = b""
    deadline = time.monotonic() + timeout_ms / 1000
    while not reply.startswith(ACK.strip()):
        left = int((deadline - time.monotonic()) * 1000)
        # The answer can arrive together with the server hanging up; read what is buffered
        # before waiting for more.
        if not sock.bytesAvailable() and (left <= 0 or not sock.waitForReadyRead(left)):
            break
        reply += bytes(sock.readAll())
    sock.abort()
    if not reply.startswith(ACK.strip()):
        log.info("The running KiPI did not confirm (reply %r): %s", reply, sock.errorString())
        return False
    return True


def bring_to_front(win) -> None:
    """Show ``win`` above other windows, restoring it if minimized."""
    from qtpy.QtCore import Qt
    if win.isMinimized():
        win.showNormal()
    win.setWindowState((win.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
    win.show()
    win.raise_()
    win.activateWindow()
    if sys.platform == "win32":
        try:
            _win32_foreground(int(win.winId()))
        except Exception:  # noqa: BLE001 - raise_() above is the fallback
            log.debug("Could not force KiPI to the foreground", exc_info=True)


def _win32_foreground(hwnd: int) -> None:
    """Windows ignores activateWindow() from a process the user did not just click, which
    is what KiPI is once it has started up behind KiCad (#112). Borrow the foreground
    window's input queue for the hand-over; if Windows still refuses, at least put the
    window on top of the others."""
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.c_void_p)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.BringWindowToTop.argtypes = (wintypes.HWND,)
    user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint)
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return
    mine = kernel32.GetCurrentThreadId()
    theirs = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(theirs and theirs != mine and user32.AttachThreadInput(mine, theirs, True))
    try:
        user32.BringWindowToTop(hwnd)
        ok = user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(mine, theirs, False)
    if not ok:
        flags = 0x0001 | 0x0002 | 0x0040          # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)     # HWND_TOPMOST
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)     # HWND_NOTOPMOST


def claim(path: Optional[Path] = None) -> Optional[InstanceGuard]:
    """The guard for this KiPI, or None after handing over to one already running."""
    guard = acquire(path)
    if guard is not None:
        return guard
    pid = running_pid(path)
    log.info("KiPI is already running (PID %s)", pid)
    if activate_running():
        return None
    from qtpy.QtWidgets import QMessageBox
    from kipi.gui.identity import make_app
    _app = make_app()  # noqa: F841 - must outlive the box
    mode = "the KiPI plugin" if current_host() is Host.PLUGIN else "standalone KiPI"
    QMessageBox.information(
        None, "KiPI",
        "KiPI is already open" + (f" (PID {pid})" if pid else "") + ".\n\n"
        f"Only one {mode} can run at a time. Switch to its window, or close it and "
        "start KiPI again.")
    return None
