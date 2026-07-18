__version__ = "0.2.1"
__release_date__ = "2026-07-18"

# Single source of truth for the app version and release date.
#
# tranqli.iss parses __version__ from the FIRST line of this file (see the
# GetVersion macro there), so keep the __version__ assignment as line 1 —
# no leading docstring or comment above it. __release_date__ is line 2.
#
# Both the About dialog (via green_tracker.__version__) and the installer
# read from here, so the version lives in exactly one place. A Python
# literal rather than a file read, so it still works inside the frozen
# PyInstaller build (which bundles this module, not loose data files).
