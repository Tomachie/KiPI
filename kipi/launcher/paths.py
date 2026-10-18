"""Where the launcher keeps KiPI's environment, and where KiPI itself is.

The environment is per user, outside the KiPI folder, so the folder can be read-only,
shared or replaced by a newer KiPI without losing it:

* Windows: ``%LOCALAPPDATA%\\KiPI``
* macOS: ``~/Library/Application Support/KiPI``
* Linux: ``$XDG_DATA_HOME/kipi`` (``~/.local/share/kipi``)

``KIPI_HOME`` puts it somewhere else.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Optional

HOME_ENV = "KIPI_HOME"

#: The KiPI folder: the one holding the ``kipi`` package, ``requirements*.txt`` and docs.
KIPI_ROOT = Path(__file__).resolve().parents[2]


def data_dir(environ: Optional[Mapping[str, str]] = None, platform: Optional[str] = None,
             home: Optional[Path] = None) -> Path:
    """The folder the launcher keeps everything in."""
    env = os.environ if environ is None else environ
    platform = platform or sys.platform
    home = home or Path.home()
    if env.get(HOME_ENV):
        return Path(env[HOME_ENV])
    if platform == "win32":
        base = env.get("LOCALAPPDATA")
        return Path(base) / "KiPI" if base else home / "AppData" / "Local" / "KiPI"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "KiPI"
    xdg = env.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else home / ".local" / "share") / "kipi"


def env_dir(data: Optional[Path] = None) -> Path:
    """KiPI's virtual environment."""
    return (data or data_dir()) / "venv"


def env_python(env: Path, windowed: bool = False, platform: Optional[str] = None) -> Path:
    """The environment's Python; ``windowed`` is the one with no console (Windows)."""
    if (platform or sys.platform) == "win32":
        return env / "Scripts" / ("pythonw.exe" if windowed else "python.exe")
    return env / "bin" / "python"


def state_path(env: Path) -> Path:
    """What the launcher installed into ``env`` (see :mod:`kipi.launcher.state`)."""
    return env / "kipi-setup.json"


def log_dir(data: Optional[Path] = None) -> Path:
    return (data or data_dir()) / "logs"


def requirements_file(root: Path = KIPI_ROOT) -> Path:
    """What KiPI needs standalone (pyproject.toml's dependencies)."""
    return root / "requirements-standalone.txt"
