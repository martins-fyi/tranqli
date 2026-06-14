"""
tranqli.py — PyInstaller entry point (also a convenience launcher).

Wraps `python -m green_tracker.main` so PyInstaller has a top-level script
to analyze. PyInstaller follows the `from green_tracker.main import main`
import to discover the entire `green_tracker` package and bundles
everything together (including the assets/ directory via --add-data).

Daily-use launching is via PyInstaller's `Tranqli.exe`. This file is
mainly the build target — but it also runs fine in dev:

    py tranqli.py
    # equivalent to:
    py -m green_tracker.main

For build instructions see build.md.

Note: this file replaces the earlier `traenky.py`, kept around for the
one-release transition where both names work. Delete `traenky.py` once
you've moved fully to the Tranqli name. The package itself
(`green_tracker/`) is intentionally NOT renamed — internal package
naming has no user-facing impact and renaming the directory would
mean updating every relative import in the codebase.
"""

import sys

from green_tracker.main import main


if __name__ == "__main__":
    sys.exit(main())
