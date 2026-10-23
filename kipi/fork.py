# Copyright (c) 2026 Tomachie LLC.
"""This fork of KiPI: the tag it adds to the release it is built on, and who changed it.

``kipi.__version__`` stays the upstream release (the update check compares it with
upstream's releases, and results files record it); what KiPI shows - the window title,
the welcome screen, About, report headers - is :data:`VERSION`, the release with this
fork's tag as a PEP 440 local version ("0.9.1+tomachie.1"). Raise the tag's number with
every change this fork ships.
"""

from kipi import __version__

TAG = "tomachie.3"
BY = "Tomachie LLC"
REPO = "https://github.com/Tomachie/KiPI"
VERSION = f"{__version__}+{TAG}"
