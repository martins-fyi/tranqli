# Building and packaging Tranqli

Two artefacts, in this order:

1. **`dist\Tranqli\`** — a PyInstaller `--onedir` bundle (`Tranqli.exe`
   plus an `_internal\` folder).
2. **`installer\Tranqli-Setup-<version>.exe`** — an Inno Setup installer
   built from that folder. This is what ships on GitHub Releases.

Both steps must run on Windows: PyInstaller cannot cross-compile, so a
Linux-side build of a Windows `.exe` is not possible. From WSL, call the
Windows toolchain through interop (`py.exe`, `ISCC.exe`) exactly the way
`git.exe` is already used — see [From WSL](#from-wsl).

## One-time setup

PyInstaller must be installed **into the interpreter you build with**,
and that interpreter needs the app's runtime dependencies importable —
PyInstaller analyses real imports, it doesn't read `requirements.txt`.

    py -m pip install pyinstaller PySide6 Flask

The project's `venv\` is the *test* environment (pytest + PySide6) and
has no PyInstaller in it. The builds are done with the system `py`
launcher instead. If you'd rather build from the venv, install
PyInstaller there and substitute `venv\Scripts\python.exe` for `py`
below.

Inno Setup 6 must also be installed. `ISCC.exe` is its command-line
compiler; the default per-user install puts it at
`%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`.

## 1. PyInstaller bundle

From the project root (`C:\Users\yes\green-tracker`) in PowerShell:

    py -m PyInstaller --noconfirm --onedir --windowed --name Tranqli --icon "green_tracker\assets\Tranqli.ico" --add-data "green_tracker\assets;green_tracker\assets" tranqli.py

- `tranqli.py` is the entry point — a thin wrapper whose
  `from green_tracker.main import main` is what PyInstaller follows to
  discover the whole package.
- `--add-data` bundles the font and icon. On Windows the source and
  destination are separated by `;` (Linux/macOS use `:`); quote the whole
  argument so PowerShell doesn't read the `;` as a statement separator.
- `--windowed` suppresses the console window.
- `--name Tranqli` is what makes the output `dist\Tranqli\` — the path
  `tranqli.iss` reads from. Change it and the installer step breaks.

Output: `dist\Tranqli\` — about 116 MB on disk, of which `Tranqli.exe`
is ~6 MB and the rest is `_internal\` (Qt, mostly). Takes a bit under
two minutes.

### Dev build — keeps a console window for diagnostics

If the app dies at startup, the release build shows nothing. Rebuild
with a console attached and run it from a terminal to see the traceback:

    py -m PyInstaller --noconfirm --onedir --console --name Tranqli-dev --icon "green_tracker\assets\Tranqli.ico" --add-data "green_tracker\assets;green_tracker\assets" tranqli.py

Output: `dist\Tranqli-dev\Tranqli-dev.exe`. Delete `dist\Tranqli-dev\`
when you're done — it is not part of a release, and leaving it behind
just makes `dist\` ambiguous.

## 2. Inno Setup installer

    "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" tranqli.iss

Output: `installer\Tranqli-Setup-<version>.exe` — ~36 MB, about a minute
to compress.

`tranqli.iss` single-sources the version: its `GetVersion` ISPP macro
parses `__version__` from the first line of `green_tracker\_version.py`
at compile time and uses it for `AppVersion`, `AppVerName` and
`OutputBaseFilename`. Nothing else needs editing to cut a new version —
bump `_version.py` and the installer filename, the Add/Remove Programs
entry and the app's About dialog all follow. That macro is also why
`__version__` must stay on line 1 of that file with no docstring above
it.

The installer is per-user (`PrivilegesRequired=lowest`, installs to
`{userpf}\Tranqli`), so it raises no UAC prompt and no
"current user / all users" page. It offers optional desktop and startup
shortcuts.

## From WSL

Interop works for both tools, so a release can be built without leaving
this shell. Run from `/mnt/c/Users/yes/green-tracker`:

    rm -rf build dist
    py.exe -m PyInstaller --noconfirm --onedir --windowed --name Tranqli --icon "green_tracker\assets\Tranqli.ico" --add-data "green_tracker\assets;green_tracker\assets" tranqli.py
    "/mnt/c/Users/yes/AppData/Local/Programs/Inno Setup 6/ISCC.exe" tranqli.iss

The Windows working directory is inherited from the WSL one as long as
you are under `/mnt/c/`, so the relative paths above resolve correctly.
Backslashes inside double quotes are literal to bash, so the `--add-data`
and `--icon` arguments need no escaping.

## Running the result

Double-click `dist\Tranqli\Tranqli.exe`, or launch the installed copy.
The widget appears, the tray icon initialises, idle/sleep detection runs,
and storage lives at `%APPDATA%\Tranqli\` — `sessions.csv`,
`config.json`, and `active_session.json` (the crash-recovery snapshot).

The onedir folder is location-independent, but it must stay intact:
`Tranqli.exe` will not run without its `_internal\` sibling.

## Shipping a release

1. Bump `__version__` (and `__release_date__`) in
   `green_tracker/_version.py`.
2. Clean: `rm -rf build dist` — PyInstaller reuses stale output
   otherwise.
3. Build the bundle, then compile the installer (above).
4. Upload `installer\Tranqli-Setup-<version>.exe` to a GitHub Release.
5. Point the download link in `docs/index.html` at the new asset.

## Notes

- **Why onedir, not onefile.** A onefile build unpacks the whole bundle
  — Qt included — into `%TEMP%\_MEIxxxxxx\` on *every* launch, which
  costs seconds each time. Onedir ships the files already unpacked, so
  the app starts immediately and the bundled font is loaded straight
  from `_internal\green_tracker\assets\uncut-sans-medium.otf` rather
  than being re-extracted per launch. The installer makes the folder
  shape invisible to users anyway.
- `build\`, `dist\` and the generated `*.spec` are PyInstaller
  artefacts and are gitignored. The flag-based command above regenerates
  `Tranqli.spec` each run, so there is no spec file to keep in sync —
  the command line in this document is the source of truth. (If you do
  want to build from a spec, `py -m PyInstaller Tranqli.spec` works, but
  the flags then live only in that untracked file.)
- `installer\` is gitignored too — the `.exe`s there are release
  artefacts, uploaded to GitHub rather than committed.
- PySide6 and Flask both ship PyInstaller hooks, so no
  `--hidden-import` flags are needed.
- Uninstalling deliberately leaves `%APPDATA%\Tranqli\` in place; the
  user's tracked time is not build output.
