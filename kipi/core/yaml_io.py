"""YAML for KiPI's saved setups (#76): easier to read, diff and edit by hand than JSON.

PyYAML is a dependency, but a KiPI whose environment predates it may not have it yet; then
:func:`available` is False and callers keep writing JSON instead.

The loader reads numbers as YAML 1.2 does (``1e-9`` is a float, not a string as YAML 1.1
has it), so hand-edited values come back as numbers.
"""

from __future__ import annotations

import json
import re
from typing import Any

try:
    import yaml
except ImportError:         # an environment set up before KiPI needed it
    yaml = None


def available() -> bool:
    return yaml is not None


if yaml is not None:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_implicit_resolver(
        "tag:yaml.org,2002:float",
        re.compile(r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+]?[0-9]+)?
                       |[-+]?(?:[0-9][0-9_]*)(?:[eE][-+]?[0-9]+)
                       |\.[0-9_]+(?:[eE][-+]?[0-9]+)?
                       |[-+]?\.(?:inf|Inf|INF)
                       |\.(?:nan|NaN|NAN))$""", re.X),
        list("-+0123456789."))


def dumps(data: Any) -> str:
    """``data`` (what JSON could hold) as block-style YAML, keys in their own order."""
    # Through JSON first: tuples become lists and anything JSON could not save fails the
    # same way here, so the YAML holds exactly what the JSON did.
    plain = json.loads(json.dumps(data))
    return yaml.safe_dump(plain, sort_keys=False, allow_unicode=True, default_flow_style=False,
                          width=100)


def loads(text: str) -> Any:
    return yaml.load(text, Loader=_Loader)
