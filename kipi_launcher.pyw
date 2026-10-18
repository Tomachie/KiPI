"""Start KiPI on its own with no console window: double-click this file. See
kipi_launcher.py."""

import os
import sys

if sys.version_info < (3, 8):
    sys.exit(2)          # no console to say why; kipi_launcher.py in a terminal says

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kipi.launcher.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(windowed=True))
