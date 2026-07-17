# Green Tracker — Build Brief

A minimalist, low-CPU Windows time tracker. A small frameless rounded widget that
tracks time against tagged daily tasks, with a system-tray icon and a tiny local
web page for editing the data.

This document is the source of truth for implementation. Build it with Claude Code
on the target Windows machine (it can install dependencies, build, and test locally).

---

## 1. Tech stack

- **Python** 3.11+
- **PySide6** — the widget (frameless, rounded, translucent, always-on-top, draggable)
  and the system-tray icon. Chosen because custom-shaped translucent windows, gradients,
  hover repainting, custom fonts, and a real tray icon are all first-class in Qt.
- **Flask** — serves the local CSV editor page in the default browser.
- **ctypes** — Win32 calls: `GetLastInputInfo` (idle) and power-broadcast handling (sleep).
- **PyInstaller** — package to a single `.exe` (`--onefile --windowed`).
- **DSEG** font (SIL OFL, free 7-segment digital-watch face) — bundle the `.ttf`,
  load via `QFontDatabase.addApplicationFont`.

Expect a ~40–70 MB `.exe` (Qt). Acceptable for a desktop utility.

---

## 2. Core performance principle — NO continuous tick

The app must NOT run as a live stopwatch. Current elapsed time is **computed on demand**
from stored timestamps, only at these moments:

- when tracking starts (record start timestamp),
- when the mouse hovers the widget (compute `now - start + accumulated` to render),
- when pausing / resuming,
- when saving a session.

The **only** recurring background task is the **30-second idle check**, and it runs
**only while tracking is active**. While paused/idle there is nothing to poll. Goal:
near-zero idle CPU.

---

## 3. Time model

State machine: `RUNNING` ↔ `PAUSED`. (Idle auto-pause is just a `PAUSED` reached
automatically.)

- An in-memory **active session** holds: `tag`, `start_timestamp`, `accumulated_seconds`.
- Elapsed now = `accumulated_seconds + (now - start_timestamp)` when RUNNING,
  else just `accumulated_seconds`.
- **Left-click** toggles RUNNING ↔ PAUSED. Pausing folds the running stretch into
  `accumulated_seconds` and clears `start_timestamp`. Resuming sets a fresh `start_timestamp`.
- **Idle auto-pause:** every 30s while RUNNING, call `GetLastInputInfo`. If idle ≥ 5 min,
  pause AND **backdate** the end of the running stretch to `last_input_time + 1 minute`
  — i.e. keep a flat 1-minute grace and discard the rest of the idle gap (cuts off 4 min
  at the nominal 5-min trip). Then restore the widget to visibility (see §6).
- **Sleep:** discard gaps. Two layers — (a) listen for Windows suspend/resume power
  broadcasts; (b) backstop: on each 30s poll, if wall-clock advanced far more than the
  poll interval, treat the excess as a non-worked gap and exclude it. Either way the
  sleep period is not counted.
- **Midnight split:** when a running stretch (or the accumulated session) spans one or
  more midnights, split it at each `00:00` boundary so each calendar day receives only
  its own minutes. Splitting is resolved at pause/save time by walking day boundaries
  between the stretch's start and end.

---

## 4. Sessions & tags

- A **session** = one **tagged task for a given day**. Natural key: `(tag, date)`.
- Flow: left-click to start → assign a tag from the right-click menu (if not already set)
  → work, pausing/resuming freely → right-click **Save session** to commit.
- Auto-generated session name on save: **`tag+date`**, e.g. `email-2026-05-28`
  (user can rename later).
- One measurement per `(tag, date)`: saving a session whose `(tag, date)` already exists
  **adds** its minutes to the existing row rather than creating a duplicate.
- **Tags section** in the right-click menu lists every tag with its **lifetime total**
  formatted as **`Dd Hh`** (e.g. 50h → `02d 02h`; days and hours zero-padded to 2, days
  grow beyond 2 digits as needed). Minutes are dropped at this aggregate level.

Display formats:
- Widget face: **HH:MM**.
- Tag totals: **`02d 02h`** (days + hours).

---

## 5. Widget UI

Frameless, always-on-top (`Qt.WindowStaysOnTopHint`), no taskbar entry (`Qt.Tool`),
translucent background so only the painted shape shows. **Draggable anywhere** on
screen: a left-press enters "drag mode" only past a ~4 px movement threshold, so a
stationary click never accidentally repositions. Last position persists in
`config.json` across restarts.

The widget has two shapes — **rectangle** (default) and **circle** — switchable from
the right-click menu. **The app always boots in rectangle mode**; the chosen shape is
deliberately **not** persisted across launches.

### Rectangle

- **Three sizes** in the right-click menu — Small / **Medium (default)** / Large.
  Sizes are stored as the digit font size in logical pixels (Qt handles DPI scaling
  to physical pixels):

  | Size   | Font | Height (font + 16) | Bevel |
  |--------|------|--------------------|-------|
  | Small  | 22   | 38                 | 8     |
  | Medium | 48   | 64                 | 14    |
  | Large  | 64   | 80                 | 18    |

  Medium's height matches the original `2 × SM_CYICON` reference (~64 px at 100% DPI).
  Changing size **keeps the widget's screen-center pinned** so it doesn't visibly jump
  when resizing.

- **Geometry rules** (uniform across sizes):
  - Padding: **8 px on all four sides** (`box-sizing` border-box).
  - Height = font size + 16 (line-height 1).
  - Width = `QFontMetrics.horizontalAdvance("00:00")` + 16.
  - **Bevel radius = height × 0.67 ÷ 3** (two-thirds of a third of the height) — a
    softer round than a pure third.

- **Colors** (intrinsic to the widget; not theme-adaptive):
  - Running background: solid `#0F432D` (muted dark green).
  - Paused background: subtle vertical gradient from `#5B5E62` (top) to `#3F4246`
    (bottom).
  - Running text: `#BFC6C0` (soft muted gray — the dim, calm reveal).
  - Paused text: `#D6D6D6` (lighter gray, more present).

- **Digital font:** DSEG 7-Classic Regular. Tight letter-spacing
  (`QFont.PercentageSpacing` at 97 %, ≈ 3 % tighter than normal). A small downward
  vertical bias (~0.05 em via a translated draw rect) compensates for the digits'
  missing descender so the cap-height reads visually centered in the pill.

- **State rules:**
  - **RUNNING:** green pill, text hidden. Hovering reveals the time; leaving fades it
    back out.
  - **PAUSED:** gray gradient pill, text always shown.

- **Crossfade:** state changes (running ↔ paused) and the hover reveal/hide both
  animate over **2000 ms** with `QEasingCurve.InOutCubic`. Background opacity, text
  opacity, and text colour all crossfade together. Implemented as two
  `QPropertyAnimation`s on custom properties (`bg_phase`, `text_opacity`).

- **Minute-refresh while visible:** when (RUNNING **and** hovered), a hover-scoped
  `QTimer` (1 s interval) calls `update()` so the displayed minute rolls over live.
  The timer stops on `leaveEvent` and whenever state leaves RUNNING — it only ticks
  during sustained hovers in the running state, never as a background task.

### Circle (compact button)

A **35 × 35 px** circle that replaces the rectangle when selected. Same colours and
state rules for the background, same click semantics (left = toggle, right = menu),
same always-on-top behaviour.

- **HH digits revealed only on hover, in both states.** No text when the mouse is
  elsewhere — state is communicated by the colour alone (green or gradient gray), and
  the digits are an on-demand reveal in both RUNNING and PAUSED. (Different from the
  rectangle's "blank running, shown paused" rule; the circle is too small to read
  unhovered, and colour suffices to convey state.)
- HH font size: **16 px**, same tight letter-spacing and vertical-bias compensation.
- The crossfade uses the same 2 s curve.
- Switching back to the rectangle restores the last selected rectangle size.

### Interaction (both shapes)

- **Left-click** (stationary): toggle start → pause → resume.
- **Left-press + drag** (past ~4 px threshold): reposition; emits `position_changed`
  to be persisted to `config.json`.
- **Right-click anywhere:** context menu (§7), opens at cursor.

---

## 6. Restore-on-auto-pause

If the widget is minimized/hidden to the tray when an **idle auto-pause** fires, bring it
back: un-minimize, raise to front, and ensure it sits within the visible screen area
(if its stored position is off-screen / on a disconnected monitor, snap it to the primary
monitor). Purpose: the user notices tracking stopped and remembers to resume.

---

## 7. Right-click menu (same on widget and tray icon)

- **Save session** — commit active session (name = `tag+date`, merge into existing `(tag, date)`).
- **Set tag** — choose/enter the tag for the active session.
- **Tags ▸** — submenu: each tag with its lifetime total formatted as `02d 02h`.
- **Rename session** — rename a saved session.
- **Delete session** — delete a saved session.
- **Size ▸** — submenu: **Small**, **Medium** (default), **Large**. Current size
  checked. Selecting a size while in circle mode switches back to rectangle at that
  size.
- **Switch to circle button** / **Switch to rectangle** — toggle widget shape. Label
  swaps with current shape. Shape is not persisted (§5).
- **Archive…** — open the archive view (§8).
- **Edit data (web)…** — launch the CSV editor in the browser (§9).
- **Minimize to tray.**
- **Quit.**

---

## 8. Archive

Browseable list of saved sessions:
- The **5 most recent** sessions listed individually at the top.
- Everything else collapsed into **year → month** groups.
- Rename / retag / delete available here too.

---

## 9. Local web CSV editor

- Tiny Flask server started on demand, bound to **`127.0.0.1:49377`** (configurable
  constant; chosen to avoid the usual 8080/3000/5000/8000). Opens in the default browser.
- Shows all saved rows as an **editable table**: add row, edit cells, delete row, save.
- Writes straight back to the same CSV that the app uses, so changes are immediately
  reflected. Server can shut down when the page/app closes.

---

## 10. Storage

- Plain **CSV**, human-readable, Excel-friendly.
- Location: `%APPDATA%\Tranqli\sessions.csv` (create dir on first run).
- Columns:

  | date (YYYY-MM-DD) | tag | session_name | minutes |
  |-------------------|-----|--------------|---------|

  Minutes stored as integer. Web editor may present/edit it as HH:MM and convert.

- **Crash-safe write:** the CSV is updated via write-temp-then-rename. Write to
  `sessions.csv.tmp` in the same directory, then `os.replace()` it onto
  `sessions.csv`. Same-directory keeps the rename on a single filesystem and atomic;
  guarantees the original is never left half-written even if the process is killed
  mid-write.

- **Minute rounding at commit:** when committing a session, each `(tag, date)`'s
  accumulated seconds is rounded to the nearest whole minute (half rounding up). A
  day-portion under 30 s rounds to 0 and writes no row. Per-day rounding means the
  tag total is just the sum of stored rows — internally consistent, no separate
  total to reconcile.

- **`config.json`** (alongside `sessions.csv`) persists:
  - `widget_pos` — last on-screen position of the widget.
  - `widget_size` — `"small" | "medium" | "large"` for the rectangle.
  - `last_tag` — most recently used tag, for quick start.
  - **Shape (rect/circle) is intentionally NOT persisted** — the app always boots in
    rectangle mode (§5).

---

## 11. Tray icon

- Small **circular** indicator in the Windows notification area.
- Color mirrors state: **green** when RUNNING, **gray** when PAUSED.
- **Left-click:** bring the widget back to view if minimized.
- **Right-click:** the same menu as §7.

---

## 12. Suggested file layout

```
green_tracker/
  main.py            # app entry, QApplication, wiring
  widget.py          # the rounded translucent widget (paint, hover, click, drag)
  tray.py            # QSystemTrayIcon + shared context menu
  tracker.py         # state machine, on-demand time math, midnight split
  idle.py            # GetLastInputInfo polling + sleep/gap handling (ctypes)
  storage.py         # CSV + config.json read/write, (tag,date) merge, tag totals
  webserver.py       # Flask CSV editor (127.0.0.1:49377)
  assets/
    dseg.ttf
  build.md           # PyInstaller command + notes
```

PyInstaller: `pyinstaller --onefile --windowed --add-data "assets;assets" main.py`
