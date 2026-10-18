"""The S-expressions KiCad saves its files in.

A file parses to nested lists: ``(segment (start 1 2) (layer "F.Cu"))`` becomes
``["segment", ["start", "1", "2"], ["layer", "F.Cu"]]``. Every atom stays a string,
quoted or not; the reader converts the numbers it needs. Quoting carries no meaning the
reader needs (KiCad 10 quotes net names where older files give net numbers, and the
reader tells those apart by the file's net table).
"""

from __future__ import annotations

import re
from typing import Iterator, List, Optional, Union

Node = List[Union[str, "Node"]]

# One token: a bracket, a quoted string (with backslash escapes), or a bare atom.
_TOKEN = re.compile(r'[()]|"(?:[^"\\]|\\.)*"|[^\s()"]+', re.S)
_ESCAPE = re.compile(r"\\(.)", re.S)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


class SexprError(ValueError):
    pass


def _unescape(m) -> str:
    c = m.group(1)
    return _ESCAPES.get(c, c)


def parse(text: str) -> Node:
    """The first expression in ``text``."""
    stack: List[Node] = []
    current: Optional[Node] = None
    for tok in _TOKEN.findall(text):
        if tok == "(":
            node: Node = []
            if current is not None:
                current.append(node)
                stack.append(current)
            current = node
        elif tok == ")":
            if current is None:
                raise SexprError("unbalanced ')'")
            if not stack:
                return current
            current = stack.pop()
        elif current is None:
            raise SexprError(f"text outside an expression: {tok[:40]!r}")
        elif tok[0] == '"':
            body = tok[1:-1]
            current.append(_ESCAPE.sub(_unescape, body) if "\\" in body else body)
        else:
            current.append(tok)
    if current is None:
        raise SexprError("no expression found")
    raise SexprError("the file ends inside an expression (is it complete?)")


def children(node: Node, head: str) -> Iterator[Node]:
    """The child expressions of ``node`` named ``head``."""
    for c in node:
        if type(c) is list and c and c[0] == head:
            yield c


def child(node: Node, head: str) -> Optional[Node]:
    """The first child expression of ``node`` named ``head``, or None."""
    for c in node:
        if type(c) is list and c and c[0] == head:
            return c
    return None


def value(node: Node, head: str, default: Optional[str] = None) -> Optional[str]:
    """The first atom of the child ``head``: ``(width 0.2)`` -> "0.2"."""
    c = child(node, head)
    if c is None or len(c) < 2 or type(c[1]) is list:
        return default
    return c[1]


def number(node: Node, head: str, default: float = 0.0) -> float:
    v = value(node, head)
    try:
        return float(v) if v is not None else default
    except ValueError:
        return default


def xy(node: Optional[Node], default=(0.0, 0.0)) -> tuple:
    """``(at 1 2 90)`` -> (1.0, 2.0)."""
    if node is None or len(node) < 3:
        return default
    return (float(node[1]), float(node[2]))


def atoms(node: Node) -> List[str]:
    """The atoms directly in ``node``, after its name: ``(layers "F.Cu" "B.Cu")``."""
    return [c for c in node[1:] if type(c) is not list]


def flag(node: Node, name: str) -> bool:
    """Whether ``node`` holds the bare atom ``name`` or a child ``(name yes)``/``(name)``.
    KiCad writes yes/no options both ways across versions."""
    for c in node[1:]:
        if c == name:
            return True
        if type(c) is list and c and c[0] == name:
            return len(c) < 2 or c[1] not in ("no", "false")
    return False
