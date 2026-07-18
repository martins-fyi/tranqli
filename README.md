# Tranqli

A quiet desktop time tracker for Windows. No account, no cloud, no analytics.

A small frameless always-on-top widget that tracks time against tagged daily tasks. One click starts it. Another pauses. Another resumes. Right-click for the menu. Hover the widget to see the time. Color tells you whether it's running. Data stays on your machine as a plain CSV file.

## What it does

- Always-on-top draggable widget — circle when paused under an hour, rectangle when showing HH:MM
- Tracking the same tag on the same day just adds to that day's entry — no duplicates
- Switch between tags on the fly, retag a session, or rename / delete / merge tags from the menu
- Undo the last change, up to 8 steps back — from the menu, the archive, or the web page
- Archive shows all records together or one tab per tag
- Idle auto-pauses after 3 minutes; sleep gaps discarded
- Six color schemes; three sizes (small, medium, large); each tag can carry its own scheme
- Plain CSV at `%APPDATA%\Tranqli\sessions.csv` — edit in Excel, a text editor, or the bundled local web page (`Edit data (web)` in the right-click menu), which can filter by tag
- Crash-safe writes (write-temp + atomic rename)

## What it doesn't do

- No popup notifications
- No tracking, no servers, no accounts — your data never leaves your computer
- No "AI-powered insights" — it just counts minutes

## Status

- **Windows** — primary platform, working today
- **macOS** — planned, via GitHub Actions cross-build

## Run from source

Requires Python 3.11 or newer.

```powershell
git clone https://github.com/martins-fyi/tranqli.git
cd tranqli
pip install PySide6 flask
py -m green_tracker.main
```

## Build the Windows EXE

```powershell
py -m PyInstaller --onedir --windowed --name Tranqli `
    --icon green_tracker\assets\Tranqli.ico `
    --add-data "green_tracker\assets;green_tracker\assets" `
    tranqli.py --noconfirm
```

Output is the `dist\Tranqli\` folder. Move or copy the **whole folder** as a unit, never just the EXE.

## Design docs

- `green-tracker-build-brief.md` — original detailed spec
- `brief-addendum.md` — the more current of the two: finalized behavior lives here, and where the two disagree, the addendum wins

The internal Python package is still named `green_tracker` — the rename to Tranqli was at the product level, not in source paths.

## Credits

- Font: [Uncut Sans](https://www.fontshare.com/fonts/uncut-sans) by [Indian Type Foundry](https://www.indiantypefoundry.com/) — free for commercial and personal use

## License

MIT — see [LICENSE](./LICENSE).

## Author

Made by [martins](https://martins.fyi).
