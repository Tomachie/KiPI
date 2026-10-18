"""Start KiPI on its own (standalone), setting it up the first time.

    python kipi_launcher.py [board file]
    python kipi_launcher.py --help        # setup options: --setup, --repair, --reset, ...

The first start installs KiPI's packages into an environment of its own (it asks first,
and says how big they are). Later starts open KiPI straight away. Double-click
kipi_launcher.pyw to do the same with no console window.

Kept to Python 2-compatible syntax, so an old Python can at least say what is needed.
"""

import os
import sys

if sys.version_info < (3, 8):
    sys.stderr.write("KiPI's launcher needs Python 3.8 or later, and KiPI itself Python 3.10 "
                     "to 3.13. This is Python %d.%d.\n" % sys.version_info[:2])
    sys.exit(2)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kipi.launcher.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(windowed=False))
