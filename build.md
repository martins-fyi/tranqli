# Building Traenky.exe

Traenky packages into a single Windows `.exe` via PyInstaller. The build
runs on Windows (the target platform); cross-compilation from WSL/Linux
isn't supported by PyInstaller.

## One-time setup

From the project root (`C:\Users\yes\green-tracker`) in PowerShell:

    py -m pip install pyinstaller

(`py -m PyInstaller …` rather than `pyinstaller …` avoids any PATH issue
when the `Scripts\` directory isn't on PATH.)

## Building

The `--add-data` flag bundles the font into the executable. On Windows
the source and destination are separated by `;` (Linux/macOS use `:`).
Quote the whole argument so PowerShell doesn't interpret the `;` as a
statement separator.

### Dev build — keeps a console window for diagnostics

Build this one first. If the app errors at startup, the console shows
the traceback. Without it, errors vanish silently.

    py -m PyInstaller --onefile --name Traenky-dev --add-data "green_tracker\assets;green_tracker\assets" traenky.py

Output: `dist\Traenky-dev.exe` (~40-70 MB).

### Release build — no console window

Once the dev build runs cleanly:

    py -m PyInstaller --onefile --windowed --name Traenky --add-data "green_tracker\assets;green_tracker\assets" traenky.py

Output: `dist\Traenky.exe`.

## Running

Double-click `dist\Traenky.exe`, or from PowerShell:

    .\dist\Traenky.exe

The widget appears, the tray icon initialises, idle/sleep detection runs,
and storage lives at `%APPDATA%\Traenky\` (sessions.csv + config.json).
The .exe is location-independent — you can move it anywhere.

## Notes

- The bundled font is extracted to PyInstaller's temp directory on each
  launch (`%TEMP%\_MEIxxxxxx\green_tracker\assets\uncut-sans-medium.otf`).
  This adds ~2-3 s to first-launch time on onefile builds. To start
  instantly, drop `--onefile` for an onedir build that ships as a folder
  rather than a single file.
- To rebuild from scratch: delete `build\`, `dist\`, and `*.spec` first.
  PyInstaller reuses the .spec from a previous run when present.
- To add an icon: pass `--icon Traenky.ico` to PyInstaller.
- PySide6 and Flask both have PyInstaller hooks built in, so no extra
  `--hidden-import` flags are typically needed.
- `build\`, `dist\`, and `*.spec` files are PyInstaller artefacts — safe
  to delete, and good candidates for `.gitignore`.
