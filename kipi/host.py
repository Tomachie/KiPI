"""How KiPI was started: as a KiCad plugin, or on its own.

As a plugin, KiCad starts KiPI for the board open in its PCB editor and KiPI reads that
board live. Standalone, KiPI opens board files and nothing in it needs KiCad. The entry
point decides (:func:`set_current`); everything else asks :func:`current`.
"""

from __future__ import annotations

import enum
import os
from typing import Mapping, Optional

#: Set to "plugin" or "standalone" to override the detection, e.g. to run the plugin
#: workflow from a terminal while developing.
HOST_ENV = "KIPI_HOST"
#: KiCad sets this for the plugin actions it starts (the IPC API socket to talk back on).
KICAD_SOCKET_ENV = "KICAD_API_SOCKET"


class Host(enum.Enum):
    PLUGIN = "plugin"
    STANDALONE = "standalone"

    @property
    def label(self) -> str:
        return "KiCad plugin" if self is Host.PLUGIN else "Standalone"


def detect(environ: Optional[Mapping[str, str]] = None) -> Host:
    """:data:`HOST_ENV` if set, else PLUGIN when KiCad started us, else STANDALONE."""
    env = os.environ if environ is None else environ
    forced = env.get(HOST_ENV, "").strip().lower()
    for h in Host:
        if forced == h.value:
            return h
    return Host.PLUGIN if env.get(KICAD_SOCKET_ENV) else Host.STANDALONE


_current: Optional[Host] = None


def set_current(h: Host) -> None:
    global _current
    _current = h


def current() -> Host:
    """The host the entry point set, else the detected one."""
    return _current if _current is not None else detect()
