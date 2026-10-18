"""What the launcher installed into KiPI's environment, kept in the environment itself.

It is written only once an install has finished, so an environment without one (an
install that was canceled, failed or never ran) is set up again on the next start.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

from kipi.launcher.paths import env_python, state_path


@dataclass
class State:
    requirements_hash: str = ""    # of requirements-standalone.txt when installed
    pardiso: bool = False          # PARDISO was chosen and installed
    python: str = ""               # the environment's Python version, "3.13.1"
    kipi_version: str = ""         # the KiPI that set it up
    installed: str = ""            # when, ISO 8601
    tool: str = "pip"              # "pip", "uv", or uv run by "uv (uvx)" / "uv (pipx)"


def load(env: Path) -> Optional[State]:
    try:
        data = json.loads(state_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    known = {f.name for f in fields(State)}
    return State(**{k: v for k, v in data.items() if k in known})


def save(env: Path, state: State) -> None:
    path = state_path(env)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    os.replace(tmp, path)           # never a half-written record


def needs_setup(env: Path, requirements_hash: str) -> Optional[str]:
    """Why the environment has to be (re)installed, or None if it is ready."""
    if not env_python(env).is_file():
        return "KiPI's environment is not set up yet" if not env.exists() else \
            "KiPI's environment is incomplete or its Python is gone"
    state = load(env)
    if state is None:
        return "the last setup did not finish"
    if state.requirements_hash != requirements_hash:
        return "this KiPI needs different packages than the ones installed"
    return None
