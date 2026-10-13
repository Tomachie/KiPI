"""Results tables that sort when a column header is clicked.

Each click on a header sorts by that column, alternating ascending and descending. Cells
sort by number where they hold one (or by an explicit key), otherwise by text, and blank
cells go last either way. Rows can be kept together: a child row (e.g. a VRM phase) stays
directly under its parent row, and pinned rows (e.g. a total) stay at the bottom.
"""

from __future__ import annotations

import re
from typing import Optional

from qtpy.QtCore import Qt
from qtpy.QtWidgets import QTableWidget, QTableWidgetItem

_NUMBER = re.compile(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")


class SortItem(QTableWidgetItem):
    """A table cell that sorts by ``key`` if given, else by the number its text starts with."""

    def __init__(self, text: str = "", key: Optional[float] = None):
        super().__init__(text)
        self.key = key
        self.parent: Optional[SortItem] = None   # the cell this one's row stays under
        self.sub = 0                             # order under the parent, or among pins
        self.pin = 0                             # > 0: stays at the bottom
        self.row0 = 0                            # row when filled, to tell groups apart

    def value(self):
        """(0, number), (1, text) or (2, "") for a blank cell."""
        if self.key is not None:
            return (0, float(self.key))
        text = self.text().replace("±", "").strip()
        if not text:
            return (2, "")
        m = _NUMBER.match(text)
        return (0, float(m.group(0))) if m else (1, text.casefold())

    def __lt__(self, other):
        if not isinstance(other, SortItem):
            return super().__lt__(other)
        t = self.tableWidget()
        desc = t is not None and t.horizontalHeader().sortIndicatorOrder() == Qt.DescendingOrder
        # Qt sorts descending by asking "other < self", so a comparison that must keep
        # its direction whichever way the column sorts is flipped when descending.
        fixed = lambda a, b: (a < b) != desc       # noqa: E731
        if self.pin != other.pin:
            return fixed(self.pin, other.pin)
        if self.pin:
            return fixed(self.sub, other.sub)
        a, b = self.parent or self, other.parent or other
        va, vb = a.value(), b.value()
        if (va[0] == 2) != (vb[0] == 2):           # blanks last
            return fixed(va[0] == 2, vb[0] == 2)
        if (va, a.row0) != (vb, b.row0):
            return (va, a.row0) < (vb, b.row0)
        return fixed(self.sub, other.sub)        # same group: parent first, then children


def make_sortable(t: QTableWidget):
    """Sort ``t`` by a column when its header is clicked, alternating the direction."""
    header = t.horizontalHeader()
    header.setSectionsClickable(True)
    header.setSortIndicatorShown(True)
    reset_sort(t)

    def clicked(col):
        same = header.property("kipi_sorted") == col
        order = (Qt.DescendingOrder if same and header.sortIndicatorOrder() == Qt.AscendingOrder
                 else Qt.AscendingOrder)
        header.setSortIndicator(col, order)
        header.setProperty("kipi_sorted", col)
        t.sortItems(col, order)
    header.sectionClicked.connect(clicked)


def reset_sort(t: QTableWidget):
    """Forget the sort column, e.g. when the table is refilled in its natural order."""
    header = t.horizontalHeader()
    header.setSortIndicator(-1, Qt.AscendingOrder)
    header.setProperty("kipi_sorted", -1)


def set_row(t: QTableWidget, r: int, cells, parent_row: Optional[int] = None, sub: int = 0,
            pin: int = 0):
    """Fill row ``r`` with ``cells`` (text, or (text, sort key)). Rows that sort with a
    parent pass its ``parent_row`` (already filled) and their order ``sub`` under it;
    pinned rows pass ``pin`` > 0 and their order ``sub`` among pinned rows."""
    for c in range(t.columnCount()):
        cell = cells[c] if c < len(cells) else ""
        text, key = cell if isinstance(cell, tuple) else (cell, None)
        it = SortItem(text, key)
        it.row0, it.sub, it.pin = r, sub, pin
        if parent_row is not None:
            it.parent = t.item(parent_row, c)
        t.setItem(r, c, it)
