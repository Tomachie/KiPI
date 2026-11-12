# Copyright (c) 2026 Tomachie LLC.
"""This fork of KiPI: the tag it adds to the release it is built on, and who changed it.

``kipi.__version__`` stays the upstream release (the update check compares it with
upstream's releases, and results files record it); what KiPI shows - the window title,
the welcome screen, About, report headers - is :data:`VERSION`, the release with this
fork's tag as a PEP 440 local version ("0.9.1+tomachie.1"). Raise the tag's number with
every change this fork ships.
"""

from kipi import __version__

TAG = "tomachie.8"
BY = "Tomachie LLC"
REPO = "https://github.com/Tomachie/KiPI"
VERSION = f"{__version__}+{TAG}"
# How this fork's DC model differs from the release it is built on, for About and the
# reports. Every item reads from a Tomachie power-tree file (pdn.json) and is off without one.
DIFFERS = (
    "Die and package model: each ball of a load joins its die through its package resistance "
    "(the maker's IBIS package model), so a ball with poor copper shows its own drop; die "
    "supplies are separate pin groups with their own currents, I/O bank currents on top",
    "Worst-case corner beside the typical: maximum currents and resistances, board inputs at "
    "their minimum through the cable allowance",
    "Pre-layout copper margin beside the solved margin, headroom for loads of unknown current, "
    "each load's return drop",
    "Capacitor and series-part AC values from the makers' own model files (ESR, ESL, SRF, "
    "impedance and inductance against DC current), via plating and ambient from the file",
    "A load's pads are one node, as its package joins them, when the file describes the part",
)
