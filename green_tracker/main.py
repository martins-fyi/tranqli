"""
main.py — Green Tracker app entry point and wiring layer.

Brings together every other module:

    - Tracker          (state machine + on-demand elapsed-time math)
    - storage          (CSV + config.json persistence)
    - TrackerWidget    (rounded translucent widget — visuals + interaction)
    - IdleMonitor      (Win32 idle / sleep-gap detection)
    - TrayIcon         (system tray indicator) + shared §7 context menu
    - CsvEditorServer  (local Flask CSV editor at 127.0.0.1:49377)

Owns the application's runtime state and all cross-module signal routing.
Individual modules know nothing of each other; main.py is the only place
that imports more than one of them.

Run with:

    python -m green_tracker.main

For PyInstaller packaging, this file is the entry point — see build.md.
"""

from __future__ import annotations

import calendar
import signal
import sys
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QDate, QPoint, QSize, Qt, QTimer
# QAnimationDriver isn't exposed in PySide6's Python bindings even
# though it exists in Qt's C++ API. Guard the import so the app
# launches; HighRefreshAnimationDriver and its install are then
# conditioned on this being available. When it's None (current
# state of PySide6), Qt's default ~60 Hz animation pulse is used
# instead — motion-blur halo and parity-snapped width animation
# still mitigate stepping at that rate.
try:
    from PySide6.QtCore import QAnimationDriver  # type: ignore
except ImportError:
    QAnimationDriver = None  # type: ignore
from PySide6.QtGui import (
    QBrush, QColor, QFontDatabase, QGuiApplication, QIcon, QPainter,
    QPalette, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QMenu, QMessageBox, QPushButton, QSpinBox,
    QStyle, QStyledItemDelegate, QSystemTrayIcon, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from . import storage
from . import updater
from ._version import __version__, __release_date__
from .tracker import Tracker, State
from .widget import (
    TrackerWidget, FONT_FILE,
    COLOR_SCHEMES, DEFAULT_SCHEME_NAME,
)
from .idle import IdleMonitor
from .tray import TrayIcon, MenuContext, show_context_menu
from .webserver import CsvEditorServer


# ---- Display formatting (brief §5) ----------------------------------------

def _format_full(seconds: float) -> str:
    """MM-only when elapsed < 1 h; HH:MM otherwise.

    The widget morphs into a circle when len(text) <= 2, so returning a
    2-char string for sub-hour times triggers the circle / pill transition
    automatically at the 1 h mark.
    """
    secs = max(0, int(seconds))
    if secs < 3600:
        return f"{secs // 60:02d}"
    h, rem = divmod(secs, 3600)
    return f"{h:02d}:{rem // 60:02d}"


def _format_short(seconds: float) -> str:
    """HH-only — used by the 35 px circle-button shape (brief §5)."""
    secs = max(0, int(seconds))
    return f"{secs // 3600:02d}"


# The "name a new tag" row in the launch gate's picker (§2a). Matched by
# value, since QInputDialog.getItem hands back the chosen string rather
# than its index — so a tag named exactly this would shadow the row. The
# ellipsis makes that about as unlikely as it gets without hand-rolling
# the dialog, and the cost if it ever happened is one extra prompt.
_NEW_TAG_ITEM = "New tag…"


def _seconds_to_minutes(seconds: float) -> int:
    """Round seconds to whole minutes, half rounds up (brief §10).

    Anything under 30 s rounds to 0 — no row should be written.
    """
    return (int(seconds) + 30) // 60


def _format_dhm(minutes: int, hours_per_day: int = 24) -> str:
    """Format a minutes count as a duration string with unit suffixes,
    omitting leading zero fields for readability. Used by the archive
    view and (mirrored, in JS) the web editor.

    `hours_per_day` controls what counts as a "day". Default is 24,
    meaning calendar days. Pass a smaller value (e.g. 8) to count
    days as work-days — same format structure, just a different
    bucket size for the `d` field.

    Examples (hours_per_day=24, the default):
        0     -> "00m"
        45    -> "45m"
        90    -> "01h 30m"
        720   -> "12h 00m"
        1440  -> "01d 00h 00m"
        1500  -> "01d 01h 00m"
        9999  -> "06d 22h 39m"

    Examples (hours_per_day=8, workday mode):
        480   -> "01d 00h 00m"   (1 workday)
        540   -> "01d 01h 00m"   (1 workday + 1 h)
        1000  -> "02d 00h 40m"   (16 h 40 m → 2 workdays + 0 h + 40 m)

    Cascading omission: only LEADING zero fields are dropped. Once a
    higher field is non-zero, lower fields are always shown — so
    "01d 00h 30m" is preferred over the ambiguous "01d 30m".

    Each field is zero-padded to 2 digits. Days grow beyond 2 digits
    naturally — `{d:02d}` pads UP TO 2 without truncating.
    """
    m = max(0, int(minutes))
    mins_per_day = max(1, int(hours_per_day)) * 60
    d = m // mins_per_day
    remainder = m % mins_per_day
    h = remainder // 60
    mins = remainder % 60
    if d > 0:
        return f"{d:02d}d {h:02d}h {mins:02d}m"
    if h > 0:
        return f"{h:02d}h {mins:02d}m"
    return f"{mins:02d}m"


def _parse_dhm(s: str) -> int:
    """Inverse of _format_dhm. Returns integer minutes.

    Accepts the same input forms as the web editor's parseDhm JS:
        "01d 02h 30m"  -> 1590  (suffix form, padded or not)
        "1d 2h 30m"    -> 1590
        "2h 30m"       -> 150
        "30m"          -> 30
        "01:02:30"     -> 1590  (legacy DD:HH:MM colon form)
        "2:30"         -> 150   (HH:MM)
        "90"           -> 90    (raw integer minutes)
        ""             -> 0

    Suffix detection runs first; if any d/h/m suffix is present,
    missing fields default to zero. Colon fallback applies only
    when no suffixes are found. Anything unparseable returns 0
    (callers gate on result > 0 to decide whether to commit).
    """
    import re
    t = (s or "").strip()
    if not t:
        return 0
    if t.isdigit():
        return int(t)
    d_match = re.search(r'(\d+)\s*d', t, re.IGNORECASE)
    h_match = re.search(r'(\d+)\s*h', t, re.IGNORECASE)
    m_match = re.search(r'(\d+)\s*m', t, re.IGNORECASE)
    if d_match or h_match or m_match:
        d = int(d_match.group(1)) if d_match else 0
        h = int(h_match.group(1)) if h_match else 0
        mn = int(m_match.group(1)) if m_match else 0
        return d * 1440 + h * 60 + mn
    parts = t.split(':')
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 3:
        return nums[0] * 1440 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    return 0


def _color_swatch_icon(color: QColor, size: int = 14) -> QIcon:
    """Build a QIcon containing a solid filled square of `color`.
    Used as the icon on each item in the archive's Change-color
    submenu so the user picks by sight, not by name."""
    pixmap = QPixmap(size, size)
    pixmap.fill(color)
    return QIcon(pixmap)


def _undo_arrow_icon(size: int = 16) -> QIcon:
    """A counter-clockwise circular arrow — the Undo glyph (spec §5).

    Drawn rather than loaded so there's no asset dependency. Returning a
    single Normal pixmap lets Qt auto-generate the greyed Disabled variant
    a QPushButton shows when the button is disabled (empty undo stack)."""
    from PySide6.QtCore import QRectF   # local: only the archive uses this
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor("#3A3A3A"))
    pen.setWidthF(max(1.4, size / 10))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    # ~270° arc, leaving a gap at the top-right where the arrowhead sits.
    m = size * 0.22
    rect = QRectF(m, m, size - 2 * m, size - 2 * m)
    p.drawArc(rect, 90 * 16, 270 * 16)   # start at top, sweep CCW 270°
    # Arrowhead at the arc's end (top, pointing left → counter-clockwise).
    cx, cy = size / 2.0, m
    a = size * 0.18
    p.drawLine(int(cx), int(cy), int(cx + a), int(cy - a))
    p.drawLine(int(cx), int(cy), int(cx + a), int(cy + a))
    p.end()
    return QIcon(pm)


if QAnimationDriver is not None:
    class HighRefreshAnimationDriver(QAnimationDriver):
        """Drives Qt animations at the display's refresh rate.

        Qt's default driver advances animations every ~16 ms regardless
        of monitor. On a high-refresh-rate display (120 / 144 / 165 /
        240 Hz) each animation frame is held for 2-4 display refreshes
        before the next one lands — the motion is mathematically smooth
        but reads as stepped because the same frame is shown multiple
        times.

        Driving the animation system at the display's actual refresh
        rate eliminates the held-frame pattern. Interval is clamped
        between 4 ms (250 Hz upper bound — guards against display
        APIs reporting odd values) and 17 ms (the default-ish rate for
        60 Hz displays — no point being slower than Qt's own default).

        Idle cost: zero. Qt calls start() only when the first animation
        begins and stop() when the last one ends, so the precise timer
        only runs during the brief windows an animation is actually in
        flight.

        NOTE: this class is only defined when PySide6 exposes
        QAnimationDriver. Current PySide6 releases do NOT expose it
        — see the conditional import above. When unavailable, the
        constant HighRefreshAnimationDriver below is None, and the
        install in App.__init__ is skipped.
        """

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self._timer = QTimer(self)
            # PreciseTimer — within Qt's tolerance, this asks the OS for
            # ~1 ms-resolution scheduling rather than the default ~16 ms
            # coarse timer that would defeat the purpose entirely.
            self._timer.setTimerType(Qt.PreciseTimer)
            screen = QGuiApplication.primaryScreen()
            refresh_hz = screen.refreshRate() if screen is not None else 60.0
            if refresh_hz <= 0:
                refresh_hz = 60.0
            interval_ms = max(4, min(17, int(round(1000.0 / refresh_hz))))
            self._timer.setInterval(interval_ms)
            self._timer.timeout.connect(self.advance)

        def start(self) -> None:  # noqa: D401 — Qt API name
            self._timer.start()
            super().start()

        def stop(self) -> None:  # noqa: D401 — Qt API name
            self._timer.stop()
            super().stop()
else:
    # Placeholder so the install path can do a simple is-not-None
    # check. When QAnimationDriver isn't available, this is None and
    # App.__init__ skips installing a custom driver — Qt's default
    # ~60 Hz pulse drives animations instead.
    HighRefreshAnimationDriver = None  # type: ignore


class AddRecordDialog(QDialog):
    """Modal dialog for adding a manual record under a known tag.

    Two inputs:
    - **Date** — defaults to today, calendar popup for picking
      historical days.
    - **Duration** — free-form text, parsed by _parse_dhm so the
      same forms work across menu / archive / web editor.

    The committed minutes are MERGED into the existing (tag, date)
    row via storage.commit_session — adding 30 min on a day that
    already has 60 min results in a 90-min row, not a duplicate.
    To overwrite instead of add, the user can use the web editor's
    direct cell edit.
    """

    def __init__(self, tag: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Add record \u2014 {tag}")
        self._tag = tag

        layout = QFormLayout(self)

        self._date_edit = QDateEdit(QDate.currentDate(), self)
        self._date_edit.setCalendarPopup(True)
        self._date_edit.setDisplayFormat("yyyy-MM-dd")
        # Allow picking any historical date; future dates are
        # legal too (rare but no reason to forbid).
        layout.addRow("Date:", self._date_edit)

        self._duration_edit = QLineEdit(self)
        self._duration_edit.setPlaceholderText("e.g. 01h 30m")
        layout.addRow("Duration:", self._duration_edit)
        # Focus the duration field on open — date is usually today
        # (already set) so the user can immediately start typing.
        self._duration_edit.setFocus()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def values(self) -> tuple[str, int]:
        """Return (date_str, minutes) after the user accepts."""
        return (
            self._date_edit.date().toString("yyyy-MM-dd"),
            _parse_dhm(self._duration_edit.text()),
        )


# ---- Archive palette (brief addendum) -------------------------------------
#
# 16 dark pastels at consistent L≈20%, S≈18%, hues evenly spaced
# around the wheel and reordered to alternate warm/cool so adjacent
# tags read as visually distinct. Each tag gets a stable color from
# this list, assigned by first-appearance order in the global newest-
# first sort. Same tag has the same color across every section
# (Recent, Year → Month). Total summary rows inherit the tag's color;
# bold-italic text alone marks them as summaries.
#
# Wraps modulo 16 for tag counts beyond the palette. With 16 distinct
# hues, collisions only become visually relevant for archives with
# more than 16 distinct tags AND the same color landing on adjacent
# tags in one section — uncommon in practice.

_ARCHIVE_TAG_PALETTE: List[QColor] = [
    QColor("#3C2A2A"),  # red            (  0.0°)
    QColor("#2A3C3C"),  # cyan           (180.0°)
    QColor("#3C312A"),  # orange-red     ( 22.5°)
    QColor("#2A353C"),  # azure          (202.5°)
    QColor("#3C382A"),  # orange         ( 45.0°)
    QColor("#2A2E3C"),  # blue           (225.0°)
    QColor("#3A3C2A"),  # amber          ( 67.5°)
    QColor("#2C2A3C"),  # indigo         (247.5°)
    QColor("#333C2A"),  # yellow-green   ( 90.0°)
    QColor("#332A3C"),  # violet         (270.0°)
    QColor("#2C3C2A"),  # lime           (112.5°)
    QColor("#3A2A3C"),  # purple         (292.5°)
    QColor("#2A3C2E"),  # green          (135.0°)
    QColor("#3C2A38"),  # magenta        (315.0°)
    QColor("#2A3C35"),  # teal           (157.5°)
    QColor("#3C2A31"),  # rose           (337.5°)
]

# Human-readable names matching _ARCHIVE_TAG_PALETTE by index. Used
# as labels in the archive context menu's "Change color" submenu —
# each item shows a colored swatch icon plus the name.
_ARCHIVE_TAG_PALETTE_NAMES: List[str] = [
    "Red", "Cyan", "Orange-red", "Azure",
    "Orange", "Blue", "Amber", "Indigo",
    "Yellow-green", "Violet", "Lime", "Purple",
    "Green", "Magenta", "Teal", "Rose",
]

# Light-gray stroke color for section-header rows (Recent, Year, Month)
# in the archive. Matches the column-header border at the top of the
# window so the section divisions read as part of the same visual
# system. Drawn by _ArchiveItemDelegate.
_ARCHIVE_SECTION_STROKE = QColor("#3A3A3A")

# Cream text color preserved on selected rows — same as the widget
# face cream. Prevents Qt's default HighlightedText (black/white,
# theme-dependent) from washing out tag-colored rows on selection.
_ARCHIVE_SELECTION_TEXT = QColor("#E9E8E4")

# QColor.darker() factor for the selection background. >100 means
# darker; 120 = ~20% darker than the row's own bg. Subtle enough to
# read as "this row, deeper" without losing the tag color identity.
_ARCHIVE_SELECTION_DARKEN = 120


class _ArchiveItemDelegate(QStyledItemDelegate):
    """Custom paint for archive rows.

    Responsibilities:
      1. Section-header rows (Recent / Year / Month — any row with
         children) get a 1-px light-gray stroke at top AND bottom,
         matching the column-header line at the top of the dialog so
         the section divisions read as part of the same visual system.
      2. Leaf rows (sessions, Totals — no children) get a 1-px
         separator at the bottom in the tree's `palette(base)` color,
         producing the gap-between-chips look on adjacent colored
         tag rows.
      3. Selected rows preserve the cream text color and darken the
         row's OWN bg by ~20 % for the selection highlight, rather
         than switching to the system blue with black/white text.
         For section headers (no explicit bg), the tree's base color
         is darkened — gives a subtle "selected" cue without a
         jarring color shift.

    All three are handled in the delegate (rather than via stylesheet)
    because any stylesheet rule on `QTreeWidget::item` causes Qt to
    switch to its stylesheet style, which IGNORES per-item
    `setBackground()` data. The colored tag backgrounds only show
    when the tree has no `::item` stylesheet rules at all.
    """

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        if not (option.state & QStyle.StateFlag.State_Selected):
            return
        # Determine the row's effective bg color and darken it for
        # the selection highlight. Leaf rows expose their tag color
        # via Qt.BackgroundRole; section headers fall back to the
        # tree's base color.
        bg_data = index.data(Qt.BackgroundRole)
        if bg_data is not None:
            base_color = (
                bg_data.color()
                if isinstance(bg_data, QBrush)
                else QColor(bg_data)
            )
        else:
            base_color = option.palette.base().color()
        if base_color.isValid():
            option.palette.setColor(
                QPalette.ColorRole.Highlight,
                base_color.darker(_ARCHIVE_SELECTION_DARKEN),
            )
        option.palette.setColor(
            QPalette.ColorRole.HighlightedText, _ARCHIVE_SELECTION_TEXT
        )

    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        painter.save()
        r = option.rect
        if index.model().hasChildren(index):
            # Section header — light gray top + bottom
            painter.setPen(_ARCHIVE_SECTION_STROKE)
            painter.drawLine(r.topLeft(), r.topRight())
            painter.drawLine(r.bottomLeft(), r.bottomRight())
        else:
            # Leaf row — thin separator at bottom in tree's base color,
            # producing the gap-between-chips look on adjacent colored
            # tag rows.
            painter.setPen(option.palette.base().color())
            painter.drawLine(r.bottomLeft(), r.bottomRight())
        painter.restore()


def _set_windows_app_user_model_id() -> None:
    """Tell Windows this process is its own app.

    Without an explicit AppUserModelID, Windows groups Tranqli's
    taskbar entry under "python.exe" (dev mode) or treats each launch
    as a fresh anonymous app (frozen mode). With one set, the taskbar
    can group windows under a single entry, remember the icon between
    sessions, and link to a pinned shortcut.

    Must be called BEFORE the first window is shown — ideally before
    QApplication is constructed. No-op on non-Windows platforms.

    Note: a different AppUserModelID is a different app to Windows.
    The user's previous "com.traenky.Traenky.1" pin (if any) will
    keep its old icon and stop opening this binary. They'll need to
    unpin the old shortcut and re-pin the new one — one-time cost
    of the rename.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "com.tranqli.Tranqli.1"
        )
    except Exception:
        # Not fatal — falls back to default identity. Common reasons:
        # very old Windows, ctypes restricted in a sandbox, etc.
        pass


def _app_icon_path() -> Optional[Path]:
    """Resolve assets/Tranqli.ico for both dev runs and PyInstaller
    builds. Returns None if the file isn't where we expect — caller
    should fall back to a default.

    Uses the same `Path(__file__).parent` approach as _load_font:
    PyInstaller patches __file__ for frozen modules to point at the
    bundled location, so `here / "assets" / ...` resolves to
    `green_tracker/assets/...` in dev and `_MEIPASS/green_tracker/
    assets/...` in frozen builds — both of which match the
    `--add-data "green_tracker\\assets;green_tracker\\assets"` flag
    in build.md.

    Looks for Tranqli.ico first; falls back to the legacy
    Traenky.ico filename so the user can rename the asset on their
    own schedule. Once renamed, the legacy entry becomes dead code
    (no-op).
    """
    here = Path(__file__).resolve().parent  # green_tracker/
    candidates = [
        here / "assets" / "Tranqli.ico",          # primary — new name
        here.parent / "assets" / "Tranqli.ico",   # primary — project root layout
        here / "assets" / "Traenky.ico",          # legacy — pre-rename name
        here.parent / "assets" / "Traenky.ico",   # legacy — root-layout fallback
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# ---- App orchestrator -----------------------------------------------------

class App:
    """Owns the runtime state and signal routing for the whole app."""

    def __init__(self, argv: List[str]) -> None:
        # Set the Windows AppUserModelID BEFORE QApplication so the
        # taskbar treats Tranqli as its own app from the very first
        # window. Doing this after QApplication is too late — the
        # default identity is already locked in.
        _set_windows_app_user_model_id()
        self.qapp = QApplication(argv)
        # Apply the .ico as the default for every window in the app.
        # The widget inherits this when it shows, giving the taskbar
        # entry a proper icon. The same .ico drives the EXE's icon in
        # Explorer when built with `--icon green_tracker/assets/
        # Tranqli.ico`.
        icon_path = _app_icon_path()
        if icon_path is not None:
            self.qapp.setWindowIcon(QIcon(str(icon_path)))
        # Hide-to-tray closes the widget window but must NOT quit the app.
        # Only the menu's Quit action does that.
        self.qapp.setQuitOnLastWindowClosed(False)

        # ---- High-refresh animation driver ------------------------------
        # Replaces Qt's default ~60 Hz animation driver with one
        # paced to the primary display's refresh rate, so animations
        # don't read as stepped on 120 / 144 / 165 / 240 Hz monitors.
        # Stored on self so it isn't garbage-collected; install()
        # makes it the active driver. Zero idle cost — the underlying
        # timer only runs while at least one animation is in flight.
        #
        # Conditional: HighRefreshAnimationDriver is None when the
        # current PySide6 doesn't expose QAnimationDriver. In that
        # case, Qt's default ~60 Hz pulse drives animations and we
        # rely on the motion-blur halo + parity-snapped width math
        # to mitigate the stepping.
        if HighRefreshAnimationDriver is not None:
            self._anim_driver = HighRefreshAnimationDriver(parent=self.qapp)
            self._anim_driver.install()
        else:
            self._anim_driver = None

        # ---- Persistence -------------------------------------------------
        # One-time data migration from the pre-rename Traenky directory.
        # Runs every startup but is a no-op once the new location
        # exists. MUST happen before any storage.load_* call, so the
        # rename lands before we try to read sessions or config.
        storage.migrate_legacy_data_if_needed()
        self.config: Dict[str, Any] = storage.load_config()
        # Size and shape live in main.py because (a) size is persisted via
        # config, (b) the MenuContext needs read access via callables, and
        # (c) the widget's own attributes for these are private (_size_name,
        # _shape). Mirroring them here keeps the public surface clean.
        self._current_size: str = self.config.get("widget_size", "medium")
        self._current_shape: str = "rect"  # never persisted (brief §10)
        # Colour scheme — persisted to config like widget_size. Falls
        # back to the default Earthen palette if the persisted key was
        # renamed, removed, or never set. Validated against the
        # registry up front so we can never pass garbage into
        # TrackerWidget; widget.set_scheme() has its own fallback for
        # belt-and-suspenders, but catching it here means clean state
        # everywhere downstream.
        scheme_key = self.config.get("color_scheme", DEFAULT_SCHEME_NAME)
        if scheme_key not in COLOR_SCHEMES:
            scheme_key = DEFAULT_SCHEME_NAME
        self._current_scheme: str = scheme_key
        # Tracks whether the user has confirmed a tag for this app-session
        # via the on-start picker. Reset on save_session, so each new
        # session starts with a fresh "which tag?" prompt.
        self._session_started: bool = False
        # Seed-mode display total. When a tag is set, this is filled with
        # whatever's already saved for (tag, today) in storage so the
        # widget reads the day's cumulative time, not just this session's
        # tracked time. Cleared on save / discard / new-session. The
        # tracker itself stays unaware of this — it still only tracks
        # the live intervals; carry is summed in at the time-provider
        # boundary so on-disk semantics (storage.commit_session adds
        # new minutes to the existing row) stay unchanged.
        self._carry_seconds: int = 0
        # Custom name for the currently-in-progress session, set via
        # "Change session" in the right-click menu BEFORE the row for
        # (tag, today) exists. Used at next on_save_session to override
        # the default "tag-date" session_name for today's row only.
        # When a row already exists, Change session renames it
        # immediately via storage.rename_session and leaves this None.
        self._pending_session_name: Optional[str] = None

        # The archive's QTabWidget while the dialog is open, else None.
        # Mutation helpers use it to refresh every tab after an edit; the
        # dialog clears it on close so a stale widget is never touched.
        self._archive_tabs: Optional[QTabWidget] = None
        # The tab-strip search box, live alongside the tabs above.
        self._archive_search: Optional[QLineEdit] = None
        # The Archive's Undo button, greyed to mirror the undo stack.
        self._archive_undo_btn: Optional[QPushButton] = None

        # ---- Font --------------------------------------------------------
        self._font_family: Optional[str] = self._load_font()

        # ---- Defensive backup -------------------------------------------
        # Refresh sessions.csv.bak if no recent one exists (24 h
        # rotation). Cheap insurance: a known-good rollback point
        # for the user if anything ever mangles the live CSV. Best-
        # effort — failures are swallowed, the app still starts.
        storage.maybe_backup_sessions()

        # ---- Crash-safety recovery (deferred) ---------------------------
        # If a snapshot file exists, the previous run died between
        # save points. Read the snapshot now (fast — small JSON) and
        # stash the parsed data. The actual recovery dialog is
        # scheduled with QTimer.singleShot(0, ...) AFTER widget.show()
        # below — so the widget appears on screen first and the
        # recovery prompt comes as a follow-up. Helps the perceived
        # launch time even on the slowest "snapshot exists" path
        # (where the dialog used to block widget.show() entirely).
        self._pending_crash_recovery: Optional[Dict[str, Any]] = (
            self._check_for_crash_recovery()
        )

        # ---- Data model --------------------------------------------------
        self.tracker = Tracker(tag=self.config.get("last_tag"))

        # ---- Widget ------------------------------------------------------
        self.widget = TrackerWidget(
            # Carry adds the day's already-saved minutes to the displayed
            # total so the widget reads as a live cumulative for the
            # current (tag, today). Tracker keeps reporting just the
            # in-progress session — storage stays single-source-of-truth
            # for accumulated history.
            time_provider_full=lambda: _format_full(
                self.tracker.elapsed_seconds() + self._carry_seconds
            ),
            time_provider_short=lambda: _format_short(
                self.tracker.elapsed_seconds() + self._carry_seconds
            ),
            font_family=self._font_family,
            start_pos=self._restore_position(),
            size_name=self._current_size,
            running=False,
            scheme_name=self._current_scheme,
        )

        # ---- Menu context (shared by tray + widget right-click) ----------
        self.menu_ctx = MenuContext(
            current_size=lambda: self._current_size,
            current_shape=lambda: self._current_shape,
            elapsed_seconds=lambda: self.tracker.elapsed_seconds(),
            # tag_lifetimes goes through _get_tag_lifetimes (not
            # storage.tag_totals directly) so the right-click Tags
            # submenu's per-tag duration string respects the same
            # display-mode toggle as the archive — without it, the
            # menu always shows calendar days (24h/day) while the
            # archive in workday mode shows workdays, and the user
            # reads the menu's smaller d-field as "way less total"
            # even though the underlying minutes are identical.
            tag_lifetimes=self._get_tag_lifetimes,
            # Read from config on each menu build rather than captured
            # once — the MRU reorders on every switch, and a stale list
            # would show the wrong five in the wrong order. Capped here
            # rather than in tray.py so how-many-to-show stays one
            # decision, shared with the launch gate's picker.
            recent_tags=lambda: self.config.get(
                "recent_tags", [],
            )[:storage.RECENT_TAGS_SHOWN],
            current_tag=lambda: self.tracker.tag,
            has_active_session=self._has_active_session,
            can_undo=storage.can_undo,
            # Recomputed live on every menu build — see populate_menu.
            pending_update=lambda: updater.pending_update(
                self.config, __version__,
            ),
            is_running=lambda: self.tracker.state == State.RUNNING,
            save_session=self.on_save_session,
            new_session=self.on_new_session,
            set_tag=self.on_set_tag,
            switch_tag=self.on_switch_tag,
            new_tag=self.on_new_tag,
            undo=self.on_undo,
            prompt_new_tag=self.on_prompt_new_tag,
            rename_tag=self.on_rename_tag,
            delete_tag=self.on_delete_tag,
            merge_tags=self.on_merge_tags,
            add_record=self.on_add_record,
            rename_session=self.on_rename_session,
            retime_session=self.on_retime_session,
            delete_session=self.on_delete_session,
            set_size=self.on_set_size,
            set_shape=self.on_set_shape,
            current_scheme=lambda: self._current_scheme,
            set_color_scheme=self.on_set_color_scheme,
            open_archive=self.on_open_archive,
            open_csv_editor=lambda: self.csv_editor.open_in_browser(),
            open_release_page=self._open_release_page,
            about=self.on_about,
            minimize_to_tray=self.widget.hide,
            quit_app=self.on_quit,
        )

        # ---- Tray icon (skip cleanly if the desktop has no tray) ---------
        self.tray: Optional[TrayIcon]
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = TrayIcon(self.menu_ctx)
            self.tray.setToolTip("Tranqli")
            self.tray.show()
        else:
            self.tray = None
            print("[main] System tray not available — running without tray.")

        # ---- Idle monitor ------------------------------------------------
        self.idle_monitor = IdleMonitor()

        # ---- Hour-mark chime timer (brief visual tick on each wall-clock
        # hour while running — driven from main.py because wall-clock
        # semantics don't belong in the widget). One-shot, rescheduled
        # after each fire and on resume / sleep-gap recovery; cancelled
        # whenever the tracker isn't running.
        self._hour_chime_timer = QTimer(self.qapp)
        self._hour_chime_timer.setSingleShot(True)
        self._hour_chime_timer.timeout.connect(self._on_hour_chime)

        # ---- Crash-safety snapshot timer --------------------------------
        # While the tracker is running, snapshot the live session to
        # `%APPDATA%\Tranqli\active_session.json` every 3 min. If the
        # process dies between Save Session commits, the next launch
        # picks up the snapshot via _check_for_crash_recovery (file
        # read in __init__) and prompts via _prompt_crash_recovery_
        # if_needed (deferred dialog after widget.show), then folds
        # the unsaved minutes into storage if the user picks Save.
        # Started / stopped by _update_running_state. Worst-case
        # loss: ~3 min of work.
        self._snapshot_timer = QTimer(self.qapp)
        self._snapshot_timer.setInterval(180_000)  # 3 min
        self._snapshot_timer.timeout.connect(self._write_snapshot)

        # ---- Midnight rollover timer ------------------------------------
        # The widget displays the day's cumulative time for the active
        # tag (tracker elapsed + storage carry). Without intervention,
        # a session that spans midnight keeps incrementing the displayed
        # total across the boundary — the user would see yesterday's
        # running time included in today's display until they manually
        # saved.
        #
        # This timer ticks every 60 s and detects calendar-date changes
        # via _check_midnight_rollover. When one is detected, the active
        # session's per-day portions are committed to storage and the
        # tracker is reset for a fresh today. The carry refresh then
        # reflects what was just committed for today (typically near
        # zero, since rollover fires within ~60 s of midnight).
        #
        # Always-on at 60 s: a single date comparison per minute when
        # no session is active. Trivial cost, well within the brief's
        # "near-zero idle CPU" budget. The 60 s detection lag at
        # midnight is acceptable — typical users aren't watching the
        # widget at midnight.
        self._last_seen_date = datetime.now().date()
        self._midnight_tick_timer = QTimer(self.qapp)
        self._midnight_tick_timer.setInterval(60_000)
        self._midnight_tick_timer.timeout.connect(
            self._check_midnight_rollover,
        )
        self._midnight_tick_timer.start()

        # ---- CSV editor server (Flask, daemon thread) --------------------
        self.csv_editor = CsvEditorServer(
            read_rows=self._read_rows_for_web,
            write_rows=self._write_rows_for_web,
            rename_tag=self._rename_tag_for_web,
            undo=self._undo_for_web,
            can_undo=storage.can_undo,
            tag_color=self._tag_color_for_web,
        )

        # ---- Wire signals ------------------------------------------------
        self._wire()

        # ---- Auto-resume today's last tag if any time tracked -----------
        # If launch happens on a day where any time has already been
        # tracked, set up the most-recently-used tag in paused mode
        # with carry seeded to today's total — so the widget displays
        # the day's accumulated time at launch rather than 00:00. The
        # user can then click to resume tracking, picking up exactly
        # where they left off. No-op if crash recovery already
        # restored an active session.
        self._auto_resume_today_if_any()

        # ---- Show --------------------------------------------------------
        self.widget.show()

        # ---- Deferred startup tasks -------------------------------------
        # Anything that doesn't need to block widget.show() runs here,
        # after the event loop is up. Right now that's only the
        # crash-recovery dialog — the snapshot data was already read
        # synchronously above (cheap), only the QMessageBox is
        # deferred. QTimer.singleShot(0, ...) means "next event loop
        # iteration", which is after the widget gets its first paint.
        if self._pending_crash_recovery is not None:
            QTimer.singleShot(0, self._prompt_crash_recovery_if_needed)

        # Update check — deferred well past first paint so it never delays
        # startup, and run off-thread (see _run_update_check). Held on the
        # instance so the QThread isn't garbage-collected mid-run.
        self._update_worker: Optional[updater.UpdateCheckWorker] = None
        QTimer.singleShot(1500, self._run_update_check)

    # ---- Wiring ---------------------------------------------------------

    def _wire(self) -> None:
        # Widget interactions
        self.widget.left_clicked.connect(self.on_toggle)
        self.widget.right_clicked.connect(self.on_widget_menu)
        self.widget.position_changed.connect(self.on_position_changed)
        # Tray interactions
        if self.tray is not None:
            self.tray.left_clicked.connect(self.widget.ensure_on_screen)
        # Idle / sleep
        self.idle_monitor.on_idle_detected.connect(self.on_idle_detected)
        self.idle_monitor.on_sleep_gap.connect(self.on_sleep_gap)
        # Slow green→rust crossfade during the 2 min before auto-pause:
        # idle monitor emits a 0..1 progress value every 2s; widget
        # interpolates the running-bg color toward rust accordingly.
        self.idle_monitor.idle_progress_changed.connect(
            self.widget.set_idle_progress,
        )
        # Re-assert HWND_TOPMOST whenever the foreground window
        # changes — see widget._assert_topmost for the rationale.
        # Some Windows apps (Photos, Snipping Tool, certain
        # installers) push themselves above always-on-top windows
        # and don't reliably restore z-order on close, so without
        # this our widget stays buried until the user manually
        # interacts with it. focusWindowChanged fires on every
        # app-switch, including the "other app just closed,
        # focus is moving" transition — perfect hook.
        self.qapp.focusWindowChanged.connect(
            lambda _w: self.widget._assert_topmost()
        )

    # ---- Startup helpers -------------------------------------------------

    def _load_font(self) -> Optional[str]:
        """Load Uncut Sans Medium, return the registered family.

        Tries the brief §12 layout first (`green_tracker/assets/`) and
        falls back to the project root (`green-tracker/assets/`).
        """
        here = Path(__file__).resolve().parent  # green_tracker/
        candidates = [
            here / "assets" / FONT_FILE,         # brief §12 — inside the package
            here.parent / "assets" / FONT_FILE,  # fallback — project root
        ]
        for asset_path in candidates:
            if asset_path.is_file():
                fid = QFontDatabase.addApplicationFont(str(asset_path))
                fams = QFontDatabase.applicationFontFamilies(fid)
                return fams[0] if fams else None
        print(f"[main] Warning: font {FONT_FILE} not found in "
              f"{[str(p) for p in candidates]}")
        return None

    def _restore_position(self) -> Optional[QPoint]:
        pos = self.config.get("widget_pos")
        if isinstance(pos, dict) and "x" in pos and "y" in pos:
            return QPoint(int(pos["x"]), int(pos["y"]))
        return None

    # ---- State queries / propagation ------------------------------------

    def _today_str(self) -> str:
        """Today's date in YYYY-MM-DD (storage row key format)."""
        return datetime.now().date().isoformat()

    def _archive_format_duration(self, minutes: int) -> str:
        """Format a duration for the archive view, respecting the
        user's archive_display_mode toggle and archive_hours_per_day
        setting.

        - mode "hours" (default): 24 h per day — the original
          behavior. Same output as calling _format_dhm directly.
        - mode "workdays": uses the configured hours-per-day
          (default 8) so totals are read as "this many workdays
          + remainder", not calendar days.

        Scope is intentionally limited to the archive view — the
        right-click Tags submenu, crash recovery dialog, and other
        duration displays elsewhere in the app continue to use the
        default calendar-day formatting. Mixing modes would be
        more confusing than letting each view pick what suits it.

        Defensive clamp on hours-per-day matches the spinbox range
        (1..23) so a corrupt config can't produce nonsense durations.
        """
        mode = self.config.get("archive_display_mode", "hours")
        if mode == "workdays":
            hpd = int(self.config.get("archive_hours_per_day", 8))
            hpd = max(1, min(23, hpd))
        else:
            hpd = 24
        return _format_dhm(minutes, hours_per_day=hpd)

    def _get_tag_lifetimes(self) -> Dict[str, str]:
        """Return {tag: formatted duration} for the right-click Tags
        submenu, using the archive's display-mode setting so menu
        and archive read consistently.

        Same aggregation shape as `storage.tag_totals()` (sum minutes
        per tag across the full session history) — the difference is
        that the format goes through `_archive_format_duration`
        rather than `storage.format_tag_total`, so the d-field
        respects the user's Hours / Workdays choice. In default
        Hours mode the output is byte-for-byte identical to the old
        path; in Workdays mode the menu and the archive's Tags
        overview now show the same numbers.

        Built fresh on each call (the tray rebuilds its menu every
        invocation, so this runs each time the right-click menu
        opens) — so it picks up any storage changes made since the
        last menu open without explicit invalidation.
        """
        raw: Dict[str, int] = {}
        for s in storage.load_sessions():
            raw[s.tag] = raw.get(s.tag, 0) + s.minutes
        return {t: self._archive_format_duration(m) for t, m in raw.items()}

    def _has_active_session(self) -> bool:
        """Is there anything worth saving right now?"""
        return (
            self.tracker.state == State.RUNNING
            or self.tracker.accumulated_seconds > 0
        )

    def _write_snapshot(self) -> None:
        """Persist the live session state to the crash-safety file.

        Called on every state transition while a session is active
        (from _update_running_state) and on each 3-min tick of
        `_snapshot_timer` while running. Stores tracker.elapsed_seconds
        (NOT carry — that's a re-lookup-from-storage thing on recovery)
        together with the tag and a date stamp.

        Skipped silently when there's no tag or no elapsed time yet
        (nothing useful to recover)."""
        tag = self.tracker.tag
        if not tag:
            return
        elapsed = int(self.tracker.elapsed_seconds())
        if elapsed <= 0:
            return
        storage.write_active_snapshot({
            "tag": tag,
            "elapsed_seconds": elapsed,
            "date": self._today_str(),
            "snapshot_at": datetime.now().isoformat(timespec="seconds"),
        })

    def _check_for_crash_recovery(self) -> Optional[Dict[str, Any]]:
        """Read the snapshot file at startup and return parsed
        recovery data, or None if there's nothing to recover.

        Only does the FILE READ — small JSON, fast — so it can run
        in __init__ before widget.show(). The actual modal dialog
        and any storage commit happens later in
        `_prompt_crash_recovery_if_needed`, scheduled with
        QTimer.singleShot(0, ...) after the event loop starts.
        That way the widget appears on screen immediately even
        when a recovery prompt is pending — the user sees the
        app launch, then handles the prompt as a follow-up.

        Returns a dict with normalized fields, or None if no
        recoverable work exists. Clears the snapshot file
        eagerly in the "snapshot present but nothing meaningful
        to recover" cases (corrupt / zero-minute) so we don't
        re-prompt on the next launch.
        """
        snap = storage.read_active_snapshot()
        if not snap:
            return None
        tag = snap.get("tag")
        elapsed = int(snap.get("elapsed_seconds", 0))
        date_str = snap.get("date") or self._today_str()
        if not tag or elapsed <= 0:
            storage.clear_active_snapshot()
            return None
        minutes = _seconds_to_minutes(elapsed)
        if minutes <= 0:
            # Less than 30 s rounds to 0 — not worth a recovery prompt.
            storage.clear_active_snapshot()
            return None
        return {
            "tag": tag,
            "minutes": minutes,
            "date_str": date_str,
        }

    def _prompt_crash_recovery_if_needed(self) -> None:
        """Show the recovery dialog (Save / Discard / Cancel) for any
        snapshot data found by `_check_for_crash_recovery`. Runs AFTER
        widget.show() via QTimer.singleShot, so the user has already
        seen the app launch before this prompt fires.

        Save → commit recovered minutes via storage.commit_session
        (merges into any existing (tag, date) row), pin last_tag,
        and refresh the widget's carry-seconds so the displayed
        total reflects the recovered work (if the recovered tag is
        the same as the active one, or seeded from auto-resume).
        Discard → drop the recovered work entirely.
        Cancel → leave the snapshot in place; next launch will
        prompt again.
        """
        data = self._pending_crash_recovery
        if data is None:
            return
        self._pending_crash_recovery = None
        reply = QMessageBox.question(
            self.widget,
            "Recover unsaved session?",
            (f"The previous run ended with unsaved time:\n\n"
             f"Tag: {data['tag']}\n"
             f"Time: {_format_dhm(data['minutes'])}\n"
             f"Date: {data['date_str']}\n\n"
             f"Save this to your session log?"),
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return  # snapshot left in place; will prompt again next launch
        if reply == QMessageBox.StandardButton.Save:
            storage.commit_session(
                tag=data["tag"],
                date_str=data["date_str"],
                minutes=data["minutes"],
            )
            # Pin the recovered tag as most-recent so the next session
            # starts with it (no UI surprise — the user "continues" by
            # picking it). push_recent_tag mirrors last_tag for us.
            storage.push_recent_tag(self.config, data["tag"])
            storage.save_config(self.config)
            # If the active tag matches the recovered tag (e.g.
            # auto-resume already set us up for the same tag today),
            # refresh the displayed total so the just-committed
            # minutes appear in the widget without waiting for a
            # state change.
            self._refresh_carry_from_storage()
        storage.clear_active_snapshot()

    def _auto_resume_today_if_any(self) -> None:
        """If today has any tracked time for the last-used tag, set
        up an in-progress session in PAUSED mode with carry seeded —
        so the widget displays today's accumulated total on launch
        instead of 00:00 and the user resumes with a single click.

        Triggered just before widget.show() in App.__init__. No-op
        when:
        - A session is already started (e.g., crash recovery
          restored one) — auto-resume must not clobber it.
        - There's no last_tag in config (fresh user, or every prior
          launch ended without saving anything).
        - last_tag has zero minutes for today (clean slate today).

        Otherwise: set the active tag, mark _session_started, seed
        _carry_seconds from storage's today total for the tag. The
        tracker stays PAUSED — the user clicks to start the live
        ticking. The widget's time_provider sums carry + tracker
        elapsed, so the initial paint shows today's total straight
        away.
        """
        if self._session_started:
            return
        last_tag = self.config.get("last_tag")
        if not last_tag:
            return
        today_str = self._today_str()
        today_mins = storage.today_minutes_for_tag(last_tag, today_str)
        if today_mins == 0:
            return
        self.tracker.set_tag(last_tag)
        self._session_started = True
        self._carry_seconds = today_mins * 60
        # Auto-resume binds the tag directly rather than through
        # on_set_tag, so apply its scheme here too — otherwise a resumed
        # tag with its own scheme would launch in the global one until the
        # next switch.
        self._apply_tag_scheme(last_tag)
        # Mirrors the post-set-tag bookkeeping in on_set_tag so the
        # rest of the app's state stays consistent. _update_running_
        # _state pushes the (still PAUSED) state to widget, tray,
        # idle monitor, and hour chime — important so the tray icon
        # and widget background colour reflect "session in progress,
        # not running" rather than "no session at all".
        self._update_running_state()

    def _update_running_state(self) -> None:
        """Push RUNNING/PAUSED to widget, tray, idle monitor, and the
        hour-chime scheduler in one shot."""
        running = self.tracker.state == State.RUNNING
        self.widget.set_running(running)
        if self.tray is not None:
            self.tray.set_running(running)
        self.idle_monitor.set_running(running)
        if running:
            self._schedule_hour_chime()
        else:
            self._hour_chime_timer.stop()
        # Crash-safety snapshot: refresh the file on every state
        # transition (so a fresh pause's elapsed is captured), and
        # run the 3-min refresh timer only while RUNNING. We leave
        # the file in place during PAUSED — paused state is still
        # recoverable; it's only on save / discard / commit that the
        # file gets cleared (those paths call clear_active_snapshot
        # directly).
        if self._has_active_session():
            self._write_snapshot()
            if running:
                self._snapshot_timer.start()
            else:
                self._snapshot_timer.stop()
        else:
            self._snapshot_timer.stop()

    # ---- Hour-mark chime -------------------------------------------------

    def _schedule_hour_chime(self) -> None:
        """Arm a one-shot timer for the next wall-clock hour mark.

        Computes ms-to-top-of-next-hour from the system clock. Replaces
        any pending timer, so it's safe to call repeatedly (e.g. after a
        sleep-gap recovery shifts wall time).
        """
        now = datetime.now()
        next_hour = (now.replace(minute=0, second=0, microsecond=0)
                     + timedelta(hours=1))
        ms = max(1, int((next_hour - now).total_seconds() * 1000))
        self._hour_chime_timer.start(ms)

    def _on_hour_chime(self) -> None:
        """Timer fired — show the chime and rearm for the next hour.

        Re-gated on RUNNING in case state changed between schedule and
        fire (the explicit cancel in _update_running_state should already
        cover this, but the defensive check is cheap).
        """
        if self.tracker.state != State.RUNNING:
            return
        self.widget.show_hour_chime()
        self._schedule_hour_chime()

    # ---- Action handlers -------------------------------------------------

    def on_toggle(self) -> None:
        """Widget left-click: start / pause / resume.

        On the first toggle of an app-session, prompt to confirm or pick a
        tag (defaults to last_tag) before starting. Subsequent toggles in
        the same session (pause/resume) skip the prompt.
        """
        if not self._session_started:
            chosen = self._pick_session_tag()
            if chosen is None:
                return  # user cancelled — leave state unchanged
            self.on_set_tag(chosen)
            self._session_started = True
        self.tracker.toggle()
        self._update_running_state()

    def _apply_tag_scheme(self, tag: Optional[str]) -> None:
        """Repaint in `tag`'s colour scheme (§2c step 4).

        tag_schemes holds per-tag overrides; a tag without one shows the
        global color_scheme rather than keeping whatever is on screen, so
        an unstyled tag always looks the same instead of inheriting the
        appearance of whichever tag you happened to switch from.

        Stores scheme *keys* ("earthen"), not the display names ("Earthen")
        spec §1's example shows. config["color_scheme"] and set_scheme()
        both speak keys, and COLOR_SCHEMES is keyed by them — a display
        name would miss every lookup and silently fall back to Earthen,
        which looks like "per-tag schemes don't work" rather than like a
        type error.

        Deliberately does not write config. This reflects a choice; it
        does not make one. Writing the current scheme back onto a tag with
        no preference — §2c step 4's optional "so it sticks" — would pin
        every tag to whatever was showing the first time it was picked,
        which is not a preference, just an accident of order.
        """
        schemes = self.config.get("tag_schemes") or {}
        key = schemes.get(tag) if tag else None
        if not key:
            key = self.config.get("color_scheme", DEFAULT_SCHEME_NAME)
        if key not in COLOR_SCHEMES:
            # Corrupt or hand-edited config, or a scheme we retired.
            key = DEFAULT_SCHEME_NAME
        if key == self._current_scheme:
            return
        self._current_scheme = key
        self.widget.set_scheme(key)

    def _prompt_new_tag_text(self, first_ever: bool = False) -> Optional[str]:
        """Ask for a new tag's name. None if cancelled or left blank.

        The single place a tag gets named, shared by the launch gate and
        the menu's New Tag\u2026, so both surfaces create tags identically
        rather than drifting into two dialogs with two behaviours.
        """
        text, ok = QInputDialog.getText(
            self.widget,
            "First tag" if first_ever else "New tag",
            "No tags yet \u2014 name your first one:"
            if first_ever else "Tag name:",
        )
        if not ok:
            return None
        return text.strip() or None

    def _pick_session_tag(self) -> Optional[str]:
        """The launch gate's picker (\u00a72a): a recent tag, or a new one.

        Shown before the first left-click of a session may start tracking,
        offering the same choices as the menu's Switch Tags picker \u2014 the
        most-recent tags, most-recent first \u2014 plus a row to name a new
        one.

        That last row is the point of this phase. This previously listed
        every existing tag alphabetically with no way out, so a returning
        user who wanted to start something new had to pick a wrong tag and
        fix it afterwards, or leave and use the web editor. Wanting a new
        tag is likeliest at exactly this moment: you are about to start
        work, and the thing you are starting may be new.

        With no history at all there is nothing to pick from, so the list
        is skipped and the name asked for directly (\u00a72a).

        Returns None if the user backs out, leaving the caller's state
        untouched.
        """
        recent = list(self.config.get("recent_tags", ()))[
            :storage.RECENT_TAGS_SHOWN
        ]
        if not recent:
            return self._prompt_new_tag_text(first_ever=True)

        # recent[0] is the last-used tag \u2014 push_recent_tag keeps it
        # there \u2014 so index 0 is already the right default, no lookup.
        chosen, ok = QInputDialog.getItem(
            self.widget, "Start tracking",
            "Which tag are we recording into?",
            recent + [_NEW_TAG_ITEM], 0, editable=False,
        )
        if not ok:
            return None
        if chosen == _NEW_TAG_ITEM:
            return self._prompt_new_tag_text()
        return chosen

    def on_widget_menu(self, pos: QPoint) -> None:
        """Widget right-click: show the same menu the tray does."""
        show_context_menu(pos, self.menu_ctx, parent=self.widget)

    def on_position_changed(self, pos: QPoint) -> None:
        """Persist new widget position to config."""
        self.config["widget_pos"] = {"x": pos.x(), "y": pos.y()}
        storage.save_config(self.config)

    # ---- Saving / tag management ----------------------------------------

    def on_save_session(self) -> None:
        """Commit the current session per brief §4 + §10.

        - Pause first so the live stretch folds into the daily total.
        - Walk get_daily_seconds(), commit each day with minutes > 0.
        - <30 s on a day rounds to 0 and skips that row.
        - Persist last_tag for the next launch.
        - Reset the tracker.
        """
        if self.tracker.tag is None:
            self.on_prompt_new_tag()
            if self.tracker.tag is None:
                return  # user cancelled the tag prompt

        if self.tracker.state == State.RUNNING:
            self.tracker.pause()

        today = self._today_str()
        daily = self.tracker.get_daily_seconds()
        for d, secs in daily.items():
            mins = _seconds_to_minutes(secs)
            if mins > 0:
                # Today's row honors a pending Change-session name;
                # other days fall back to the default "tag-date".
                # Only takes effect when no row exists yet — if a row
                # is already on disk, merge_into preserves its name.
                name = (
                    self._pending_session_name
                    if d.isoformat() == today else None
                )
                storage.commit_session(
                    tag=self.tracker.tag,
                    date_str=d.isoformat(),
                    minutes=mins,
                    session_name=name,
                )

        storage.push_recent_tag(self.config, self.tracker.tag)
        storage.save_config(self.config)

        self.tracker.reset()
        self._session_started = False
        self._carry_seconds = 0
        self._pending_session_name = None
        storage.clear_active_snapshot()
        self._update_running_state()

    def _check_midnight_rollover(self) -> None:
        """Detect calendar-date change and commit the active session's
        per-day portions so the widget shows time per day, not per
        session.

        Called from the 60 s midnight tick timer. Cheap no-op when the
        date hasn't changed or no session is active. When the date
        HAS changed and there's an active session:

        - Pauses to fold the live running stretch into accumulated.
        - Walks tracker.get_daily_seconds() — which already splits
          accumulated time at calendar-day boundaries — and commits
          each day's portion to storage.
        - Resets the tracker and re-sets the active tag, so its
          accumulated counter starts at zero for today.
        - Reloads carry from storage (now includes whatever was just
          committed for today, typically near zero since rollover
          fires within ~60 s of midnight).
        - Resumes tracking if it was running before.

        Net effect: the displayed total (carry + tracker.elapsed)
        drops to ~zero at midnight and starts accumulating today's
        portion fresh, instead of including yesterday's running time.
        """
        current_date = datetime.now().date()
        if current_date == self._last_seen_date:
            return
        self._last_seen_date = current_date

        # No active session → nothing to commit, just keep tracking
        # the date for next time.
        if self.tracker.tag is None or not self._session_started:
            return

        was_running = self.tracker.state == State.RUNNING
        if was_running:
            self.tracker.pause()

        saved_tag = self.tracker.tag
        today_str = current_date.isoformat()

        # Commit each day's portion. get_daily_seconds() already does
        # the split — yesterday gets pre-midnight seconds, today gets
        # post-midnight seconds. Sub-minute portions are dropped
        # (consistent with on_save_session's behavior).
        daily = self.tracker.get_daily_seconds()
        for d, secs in daily.items():
            mins = _seconds_to_minutes(secs)
            if mins > 0:
                # Pending session name applies only to today's row,
                # matching on_save_session. Other days use the default
                # "tag-date" via commit_session's fallback.
                name = (
                    self._pending_session_name
                    if d.isoformat() == today_str else None
                )
                storage.commit_session(
                    tag=saved_tag,
                    date_str=d.isoformat(),
                    minutes=mins,
                    session_name=name,
                )

        # Reset tracker, re-set tag, and consume the pending name (it
        # was used in today's commit above if applicable). Subsequent
        # midnight rollovers would re-use this same path, but with no
        # pending name unless the user explicitly sets one again.
        self.tracker.reset()
        self.tracker.set_tag(saved_tag)
        self._pending_session_name = None

        # Refresh carry to today's committed value (typically 0-1 min
        # since rollover fires within ~60 s of midnight).
        self._carry_seconds = storage.today_minutes_for_tag(
            saved_tag, today_str,
        ) * 60

        if was_running:
            self.tracker.toggle()

        # Force a repaint so the display reflects the post-rollover
        # state immediately — without this the widget would keep
        # showing the pre-rollover total until something else
        # triggered a paint (hover, animation, idle tick, etc.).
        self.widget.update()

    def on_new_session(self) -> None:
        """Start a fresh session from a clean slate.

        If there's an active session (running OR paused with accumulated
        time), prompt the user to save it first. Three-way choice:

        - **Save**: commit the current session via on_save_session, then
          reset. Same end state as if they'd hit Save themselves.
        - **Discard**: throw away the current session's time and reset.
          The tag stays in `last_tag` for convenience but no row is
          written.
        - **Cancel**: do nothing, current session continues as-is.

        After save or discard, the tracker is reset and
        `_session_started` is False — the next left-click on the widget
        re-opens the tag picker. If there's no active session, this
        is effectively a no-op (the next click will already pick a tag).
        """
        if self._has_active_session():
            reply = QMessageBox.question(
                None,
                "New session",
                "Save the current session before starting a new one?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                return
            if reply == QMessageBox.StandardButton.Save:
                self.on_save_session()
                # on_save_session already reset everything.
                return
            # Discard: pause if running, drop the accumulated time.
            if self.tracker.state == State.RUNNING:
                self.tracker.pause()
            self.tracker.reset()
            self._session_started = False
            self._carry_seconds = 0
            self._pending_session_name = None
            storage.clear_active_snapshot()
            self._update_running_state()

    def on_set_tag(self, tag: str) -> None:
        """Rebind the live tracker's tag, keeping accumulated time.

        Surfaced as "Retag session" — it corrects which tag the current
        session belongs to. The tracker's accumulated time is left alone,
        so on save it lands on whatever tag is set now: the in-progress
        work is *re-attributed* to the new tag. That is the point of the
        entry — you started tracking, then noticed it was the wrong tag.

        Contrast on_switch_tag ("Switch task"), which banks the current
        tag's time first and starts the new one at zero. Both change the
        active tag; only this one moves the time already on the clock.

        Also seeds `_carry_seconds` from whatever's saved for
        (tag, today) so the widget's displayed total reads as the
        day's cumulative for this tag — not just this session's
        tracked time.
        """
        self.tracker.set_tag(tag)
        storage.push_recent_tag(self.config, tag)
        storage.save_config(self.config)
        # Hooked here rather than in on_switch_tag so every bind repaints:
        # the launch gate, Switch Tags, New Tag and Retag session all pass
        # through this method, and a widget showing the previous tag's
        # colours after any of them would be wrong in the same way.
        self._apply_tag_scheme(tag)
        self._carry_seconds = (
            storage.today_minutes_for_tag(tag, self._today_str()) * 60
        )
        # New tag means the snapshot's tag field is stale — refresh.
        if self._has_active_session():
            self._write_snapshot()

    def on_switch_tag(self, tag: str) -> None:
        """Switch task (spec §2c): bank the current tag, start the new one.

        Surfaced as "Switch task". Unlike on_set_tag / "Retag session",
        the time already on the clock stays with the tag that earned it.

        1. Commit whatever is unsaved via on_save_session — the same code
           path as the menu's own Save, so rounding, midnight splitting
           and (tag, date) merging behave identically.
        2. Rebind to `tag`. on_save_session has already reset the tracker,
           so the new session starts at zero with no start_timestamp —
           PAUSED, per the confirmed behaviour. Switching never resumes:
           a mis-click in the menu must not silently start recording time
           against the wrong tag.
        3. on_set_tag pushes the MRU and re-seeds the carry, so the widget
           immediately reads the new tag's total for today.

        The tag guard keeps on_save_session's no-tag branch unreachable
        from here: it would open a picker mid-switch, and cancelling that
        picker returns with the tracker untouched, leaving this method to
        rebind over time it had failed to bank. Untagged time cannot be
        committed anywhere anyway — there is no tag to file it under.

        _session_started stays True so the next left-click starts the new
        tag straight away rather than re-opening the picker (§2b) — the
        user has just chosen a tag; asking again would be noise.

        Picking the tag already running is a no-op. It is the checked
        entry in the picker, so it is easy to click by reflex, and the
        honest reading of "switch to what I'm already on" is "nothing to
        do". Banking and rebinding would stop a running clock and reset
        it to 00:00 — indistinguishable from a bug.
        """
        if tag == self.tracker.tag:
            return

        if self.tracker.tag is not None and self._has_active_session():
            self.on_save_session()

        self.on_set_tag(tag)
        self._session_started = True
        self._update_running_state()

    def _tag_has_live_unsaved_session(self, tag: str) -> bool:
        """Is `tag` the tag of an in-progress session with unbanked time?

        The §4 safety condition for delete and merge: not merely "is this
        tag active" but "would acting on it destroy or move time that is
        only on the clock". A tag sitting active with nothing accumulated
        has nothing to lose.
        """
        return self.tracker.tag == tag and self._has_active_session()

    def on_delete_tag(self, tag: str) -> None:
        """Delete a tag and all its history (§4).

        Two confirmations, deliberately: the live-session warning is about
        time that exists nowhere else yet, while the ordinary confirmation
        is about stored rows. They are different losses and collapsing
        them into one dialog would bury the worse of the two.

        The spec offers "save first, or cancel" here. Save first is not
        offered for delete, because it does not help: banking the session
        writes a row for this tag, and the delete then removes every row
        for this tag — including the one just written. The user would be
        told their work was saved and it would be gone regardless. The
        honest choice is to state that the in-progress session dies with
        the tag, and let them cancel. Merge, where saving genuinely does
        preserve the time, does offer it.
        """
        rows = [s for s in storage.load_sessions() if s.tag == tag]
        if self._tag_has_live_unsaved_session(tag):
            elapsed = _format_dhm(
                _seconds_to_minutes(self.tracker.elapsed_seconds()),
            )
            reply = QMessageBox.question(
                self.widget, "Delete tag",
                f"'{tag}' is the tag of your session in progress "
                f"({elapsed} not yet saved).\n\n"
                f"Deleting '{tag}' removes its {len(rows)} stored "
                f"session(s) and discards the one in progress. The "
                f"in-progress time cannot be recovered by Undo.\n\n"
                f"Delete anyway?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if reply != QMessageBox.Yes:
                return
        else:
            reply = QMessageBox.question(
                self.widget, "Delete tag",
                f"Delete '{tag}' and its {len(rows)} stored session(s)?\n\n"
                f"This can be reversed with Undo.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        storage.delete_tag(tag)
        storage.forget_tag_in_config(self.config, tag)
        storage.save_config(self.config)

        # A tracker left pointing at a deleted tag would recreate it on
        # the next save — the deletion would appear to undo itself.
        if self.tracker.tag == tag:
            self.tracker.reset()
            self._session_started = False
            self._carry_seconds = 0
            storage.clear_active_snapshot()
            self._update_running_state()
        self._refresh_carry_from_storage()

    def on_merge_tags(self, absorbed: str) -> None:
        """Merge `absorbed` into a target tag chosen by the user (§4).

        Invoked from the absorbed tag's own menu entry, so that tag is
        fixed and the target is picked — the dialog says so explicitly,
        because merging the wrong way round destroys the wrong name and
        the direction is not recoverable from the result.

        Unlike delete, saving first is offered and does help: the banked
        time lands on `absorbed`, and the merge then folds it into the
        target along with everything else.
        """
        others = sorted(t for t in storage.tag_totals() if t != absorbed)
        if not others:
            QMessageBox.information(
                self.widget, "Merge tags",
                f"'{absorbed}' is the only tag — nothing to merge into.",
            )
            return

        target, ok = QInputDialog.getItem(
            self.widget, "Merge tags",
            f"Merge '{absorbed}' into which tag?\n"
            f"'{absorbed}' is absorbed and disappears; its time moves "
            f"to the tag you pick.",
            others, 0, editable=False,
        )
        if not ok or not target:
            return

        if self._tag_has_live_unsaved_session(absorbed):
            elapsed = _format_dhm(
                _seconds_to_minutes(self.tracker.elapsed_seconds()),
            )
            reply = QMessageBox.question(
                self.widget, "Merge tags",
                f"'{absorbed}' has a session in progress ({elapsed} not "
                f"yet saved).\n\n"
                f"Save it before merging, so it moves to '{target}' too?",
                QMessageBox.Save | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if reply != QMessageBox.Save:
                return
            self.on_save_session()

        if not storage.merge_tags(target, absorbed):
            return
        storage.rename_tag_in_config(self.config, absorbed, target)
        storage.save_config(self.config)

        # Same reasoning as rename: the absorbed name no longer exists,
        # so a tracker still on it would resurrect it on the next save.
        if self.tracker.tag == absorbed:
            self.tracker.set_tag(target)
        self._refresh_carry_from_storage()

    def on_undo(self) -> None:
        """Undo the last CSV mutation (spec §5).

        Pops the newest whole-file snapshot and writes it back through the
        same crash-safe path as a normal save. The stack is global and
        in-process, so this undoes the last mutation from any surface —
        a widget save, a tag switch's auto-save, an archive edit, a web
        editor save — not merely the last one made from this menu.

        Re-seeds the carry afterwards for the same reason
        _write_rows_for_web does: the restored CSV may hold a different
        total for the live (tag, today) row than the one on screen, and
        without this the widget would keep displaying the pre-undo
        figure until the next state change.

        The tracker itself is untouched. Undo restores *stored* history;
        it is not a time machine for the session on the clock, and
        rewinding a running session would be a surprise nobody asked for.
        """
        if not storage.undo():
            return  # nothing on the stack; the menu item is greyed anyway
        self._refresh_carry_from_storage()

    def _on_archive_undo(self) -> None:
        """Archive Undo button: undo, then rebuild the open archive.

        Shares on_undo with the menu — same global stack — and follows it
        with _refresh_archive so the tabs and this button's enabled state
        reflect the restored CSV immediately.
        """
        self.on_undo()
        self._refresh_archive()

    def _sync_archive_undo_button(self) -> None:
        """Match the Archive Undo button's enabled state to the stack.

        Called on every archive rebuild, so any mutation (which pushes a
        snapshot) enables it and an undo that empties the stack greys it.
        """
        if self._archive_undo_btn is not None:
            self._archive_undo_btn.setEnabled(storage.can_undo())

    def on_new_tag(self) -> None:
        """"New tag…" in the picker: free-text entry, then switch to it.

        Creates the tag mid-session, where the launch gate's own New tag…
        row covers the same need at the start of one. Both go through
        _prompt_new_tag_text, so a tag is named the same way wherever you
        happen to be when you decide you need it.

        The tag needs no separate registration: tags are implicit, defined
        by whatever strings exist in the CSV's tag column (spec §1), so
        switching to a new name and tracking against it is what brings it
        into being. Nothing is written until the session is saved.
        """
        tag = self._prompt_new_tag_text()
        if tag is None:
            return
        self.on_switch_tag(tag)

    def on_prompt_new_tag(self) -> None:
        """Prompt to set the tag — delegates to the start-of-session picker.

        Kept as a callback because `MenuContext` still exposes it, and the
        save-session fallback for the pathological "no tag set" case uses
        it. New tags should otherwise come from the web editor.
        """
        chosen = self._pick_session_tag()
        if chosen:
            self.on_set_tag(chosen)

    def on_rename_tag(self, old_tag: Optional[str] = None) -> None:
        """Rename a tag across every stored session.

        Two flows:
        - **Tag supplied** (menu submenu, archive context menu): skip
          the picker and go straight to the "new name" prompt.
        - **Tag not supplied**: pick from the list of existing tags
          first, then ask for the new name. Empty / whitespace / same-
          name input is silently dropped — storage.rename_tag would
          no-op anyway.

        Storage handles (tag, date) collisions by summing minutes into
        a single row. After the rename, we re-point the tracker if the
        active tag was the one renamed, then re-seed `_carry_seconds`
        from the live storage state so the displayed cumulative reflects
        any merged minutes immediately.
        """
        if old_tag is None:
            existing = sorted(storage.tag_totals().keys())
            if not existing:
                QMessageBox.information(
                    self.widget, "Rename tag", "No tags to rename.",
                )
                return
            old_tag, ok = QInputDialog.getItem(
                self.widget, "Rename tag",
                "Which tag do you want to rename?",
                existing, 0, editable=False,
            )
            if not ok or not old_tag:
                return
        new_tag, ok = QInputDialog.getText(
            self.widget, "Rename tag",
            f"Rename '{old_tag}' to:",
            text=old_tag,
        )
        if not ok:
            return
        new_tag = new_tag.strip()
        if not new_tag or new_tag == old_tag:
            return
        if not storage.rename_tag(old_tag, new_tag):
            return

        # The MRU follows the rename whether or not the renamed tag is
        # the live one — otherwise the picker keeps offering a name that
        # no longer exists in storage.
        storage.rename_tag_in_config(self.config, old_tag, new_tag)
        storage.save_config(self.config)

        # Re-point the live tracker if it was using the old tag.
        if self.tracker.tag == old_tag:
            self.tracker.set_tag(new_tag)
        # Carry may need re-seeding — either the active tag was just
        # renamed, or it absorbed merged minutes from another row.
        self._refresh_carry_from_storage()

    def on_add_record(self, tag: str) -> None:
        """Open the Add record dialog for `tag` and commit on accept.

        Duration is parsed flexibly (suffix, colon, or raw minutes)
        and merged into the existing (tag, date) row via the standard
        commit_session path — never destructive, only additive. If
        the record lands on today's date for the actively-tracked
        tag, the carry is re-seeded so the widget's cumulative
        display reflects the added minutes immediately.

        Empty / invalid duration shows a notice rather than silently
        no-op'ing — accidental empty submits would otherwise leave
        the user wondering whether the record was added.
        """
        dlg = AddRecordDialog(tag, parent=self.widget)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        date_str, minutes = dlg.values()
        if minutes <= 0:
            QMessageBox.information(
                self.widget, "Add record",
                "Duration is empty or unparseable \u2014 no record added.",
            )
            return
        storage.commit_session(tag=tag, date_str=date_str, minutes=minutes)
        # Re-seed carry + repaint if this affects today's view of the
        # active tag. The helper no-ops when the date or tag doesn't
        # match, so the guard here is just to avoid the function call.
        if self.tracker.tag == tag and date_str == self._today_str():
            self._refresh_carry_from_storage()

    # ---- Session rename / delete (brief §7) -----------------------------

    # ---- Current-session edits (right-click menu) -----------------------
    #
    # These act on the in-progress session (active tag + today). The
    # archive view's right-click menu handles editing arbitrary past
    # sessions; these are the convenience shortcuts for the one you're
    # actively tracking.

    def on_rename_session(self) -> None:
        """Rename the currently-active (tag, today) session.

        Two cases:
        - A saved row exists for (active tag, today): rename it in
          place via storage.rename_session.
        - No row exists yet (session in progress, never saved): remember
          the name in _pending_session_name. The next on_save_session
          uses it for today's row only — earlier days from a midnight-
          spanning session keep the default `tag-date`.

        For renaming arbitrary past sessions, use the archive's
        right-click menu instead.
        """
        if self.tracker.tag is None:
            QMessageBox.information(
                self.widget, "Rename session",
                "No active tag yet \u2014 set a tag first.",
            )
            return
        today = self._today_str()
        existing = next(
            (s for s in storage.load_sessions()
             if s.tag == self.tracker.tag and s.date == today),
            None,
        )
        current_name = (
            existing.session_name if existing
            else self._pending_session_name
            or f"{self.tracker.tag}-{today}"
        )
        new_name, ok = QInputDialog.getText(
            self.widget, "Rename session",
            "Name for the current session:",
            text=current_name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == current_name:
            return
        if existing:
            storage.rename_session(existing.session_name, new_name)
        # Stash for the next save too — if a row gets created later
        # (e.g. session resumed across midnight), today's row should
        # also get this name.
        self._pending_session_name = new_name

    def on_retime_session(self) -> None:
        """Set the recorded time for (active tag, today) outright.

        Replaces the storage row's minutes (creating it if absent)
        — does NOT touch the in-progress tracker. The widget's
        displayed total = carry (newly-set storage value) + tracker
        elapsed, so:

        - If paused with no elapsed: display shows the new value.
        - If running with N minutes elapsed: display jumps to
          new + N (because elapsed continues to count from where
          it was). On the next save, commit_session will add the
          elapsed minutes to the new total, keeping the storage
          value consistent with the display.

        Empty input cancels. Setting to zero drops the row (matches
        the no-row-for-empty-day invariant)."""
        if self.tracker.tag is None:
            QMessageBox.information(
                self.widget, "Retime session",
                "No active tag yet \u2014 set a tag first.",
            )
            return
        today = self._today_str()
        current_mins = storage.today_minutes_for_tag(self.tracker.tag, today)
        new_text, ok = QInputDialog.getText(
            self.widget, "Retime session",
            f"Total time for today's '{self.tracker.tag}' session:",
            text=_format_dhm(current_mins),
        )
        if not ok or not new_text.strip():
            return
        new_mins = _parse_dhm(new_text)
        storage.set_minutes_for_tag_date(
            self.tracker.tag, today, new_mins,
            session_name=self._pending_session_name,
        )
        self._refresh_carry_from_storage()

    def on_delete_session(self) -> None:
        sessions = storage.load_sessions()
        names = [s.session_name for s in sessions]
        if not names:
            QMessageBox.information(self.widget, "Delete", "No saved sessions.")
            return
        name, ok = QInputDialog.getItem(
            self.widget, "Delete session",
            "Select session to delete:",
            names, 0, editable=False,
        )
        if not ok:
            return
        reply = QMessageBox.question(
            self.widget, "Delete session",
            f"Delete '{name}'?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            storage.delete_session(name)
            self._refresh_carry_from_storage()

    # ---- Size / shape ---------------------------------------------------

    def on_set_size(self, name: str) -> None:
        self._current_size = name
        self.widget.set_size(name)
        self.config["widget_size"] = name
        storage.save_config(self.config)

    def on_set_shape(self, name: str) -> None:
        self._current_shape = name
        self.widget.set_shape(name)
        # Shape intentionally NOT persisted (brief §10 — app always boots
        # in rectangle mode).

    def on_set_color_scheme(self, name: str) -> None:
        """Switch the widget's colour scheme. Persisted to config so
        the choice survives across launches.

        No-op if the key is unknown (defensive — menu items are
        wired from COLOR_SCHEMES so this shouldn't happen, but
        prevents a corrupt menu hand-off from poisoning state) or
        if the scheme is already active.
        """
        if name not in COLOR_SCHEMES or name == self._current_scheme:
            return
        self._current_scheme = name
        self.widget.set_scheme(name)
        self.config["color_scheme"] = name
        storage.save_config(self.config)

    # ---- Update check ---------------------------------------------------

    def _run_update_check(self) -> None:
        """Deferred startup step (see __init__): check GitHub once a day.

        If we've already checked today, skip the network entirely and just
        evaluate what we already know. Otherwise spin up the worker thread;
        the network call never runs on the UI thread.
        """
        if not updater.should_check_today(self.config):
            self._evaluate_update()
            return
        worker = updater.UpdateCheckWorker()
        worker.finished_check.connect(self._on_update_check_finished)
        self._update_worker = worker  # keep a ref so Qt can't collect it
        worker.start()

    def _on_update_check_finished(self, latest: object) -> None:
        """Worker result handler (UI thread). Records the check — stamping
        today's date whatever the outcome, so a failed/offline check waits
        until tomorrow — persists, then evaluates."""
        updater.record_check_result(self.config, latest)
        storage.save_config(self.config)
        self._evaluate_update()

    def _evaluate_update(self) -> None:
        """Act on the known state. The menu item needs no explicit refresh
        here: populate_menu recomputes pending_update on every open, so it
        always reflects current config. This only decides the popup, which
        IS throttled (should_show_popup) independently of the daily check.
        """
        latest = updater.pending_update(self.config, __version__)
        if latest and updater.should_show_popup(self.config, latest):
            updater.record_popup_shown(self.config)
            storage.save_config(self.config)
            self._show_update_popup(latest)

    def _open_release_page(self) -> None:
        webbrowser.open(updater.GITHUB_RELEASE_URL)

    def _show_update_popup(self, latest: str) -> None:
        """Interruptive "new version available" popup.

        Parentless and non-topmost like About / Archive (addendum §3, §9) —
        a normal coverable window. Skip and Update both dismiss `latest`
        (once acted on, don't re-nag for that version); Update also opens
        the releases page first.
        """
        dialog = QDialog()
        dialog.setWindowTitle("Update available")
        dialog.setFixedSize(340, 150)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        msg = QLabel(
            f"Hello there! There is a new Tranqli version ({latest}) "
            f"available for you.\n\nWould you like to update?"
        )
        msg.setWordWrap(True)
        layout.addWidget(msg)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        skip = QPushButton("Skip")
        update = QPushButton("Update")
        update.setDefault(True)
        buttons.addWidget(skip)
        buttons.addWidget(update)
        layout.addLayout(buttons)

        def _dismiss_and_close() -> None:
            updater.dismiss(self.config, latest)
            storage.save_config(self.config)
            dialog.accept()

        def _on_skip() -> None:
            _dismiss_and_close()

        def _on_update() -> None:
            self._open_release_page()
            _dismiss_and_close()

        skip.clicked.connect(_on_skip)
        update.clicked.connect(_on_update)
        dialog.exec()

    # ---- Archive (brief §8) ---------------------------------------------

    def on_about(self) -> None:
        """Small About dialog: name, version, release date, and links.

        The app's only About surface. Version and release date come from
        green_tracker._version (the single source the installer also
        reads), so they can't drift from the release.

        Parentless and non-topmost, like the Archive (§ addendum 3) — a
        normal, coverable window rather than something pinned above
        everything by the widget's always-on-top hint.
        """
        dialog = QDialog()
        dialog.setWindowTitle("About Tranqli")
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(8)

        name = QLabel("Tranqli")
        f = name.font()
        f.setPointSizeF(f.pointSizeF() + 6)
        f.setBold(True)
        name.setFont(f)
        layout.addWidget(name)

        layout.addWidget(QLabel(f"Version {__version__}"))
        layout.addWidget(QLabel(f"Released {__release_date__}"))

        # Rich-text labels with real hyperlinks. openExternalLinks lets the
        # OS browser handle the click; the dialog needs no click wiring.
        site = QLabel(
            '<a href="https://martins-fyi.github.io/tranqli/">'
            "martins-fyi.github.io/tranqli</a>"
        )
        site.setOpenExternalLinks(True)
        layout.addWidget(site)

        licence = QLabel(
            'Developed under the '
            '<a href="https://opensource.org/license/mit/">MIT License</a>.'
        )
        licence.setOpenExternalLinks(True)
        layout.addWidget(licence)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        dialog.setFixedSize(dialog.sizeHint())
        dialog.exec()

    def on_open_archive(self) -> None:
        """Grouped archive view: 5 most recent sessions individually at
        the top, full year → month tree, and a tag-totals overview at
        the bottom.

        Bottom bar carries a Display-mode toggle (Hours / Workdays) and
        a Hours-per-day spinbox for the workday-mode divisor. Both
        persist to config.json and trigger a tree rebuild on change.

        Right-click any session row for Rename / Retag / Retime /
        Delete. Closing the dialog has no side effects — actions
        persist as they happen.
        """
        # Deliberately NOT parented to self.widget. The tracking widget
        # carries Qt.WindowStaysOnTopHint (brief-addendum §3, widget only),
        # and on Windows a dialog owned by a topmost window is kept above
        # everything too — so parenting made the Archive un-coverable even
        # though it has no topmost flag of its own. As a top-level window
        # it behaves normally: other windows can cover it, and the widget
        # stays on top as intended.
        dialog = QDialog()
        dialog.setWindowTitle("Archive")
        dialog.resize(640, 520)
        layout = QVBoxLayout(dialog)

        # One tab per tag plus an "All" tab (§6a). The tab set is rebuilt
        # from storage on every mutation, since deleting/merging/renaming
        # a tag changes which tabs exist — so it is held on the instance
        # for the mutation helpers to reach, and cleared when the dialog
        # closes so a stale widget is never repopulated.
        tabs = QTabWidget()
        self._archive_tabs = tabs
        # Scroll arrows when the strip overflows the window. Qt scrolls
        # the tab viewport without changing the current tab, which is the
        # §6a requirement that browsing the strip never moves selection.
        tabs.setUsesScrollButtons(True)
        tabs.setElideMode(Qt.ElideRight)

        # Search box in the tab bar's corner: filter tabs by typed text
        # (§6a). Hides non-matching per-tag tabs; All always stays.
        search = QLineEdit()
        search.setPlaceholderText("Search tags…")
        search.setClearButtonEnabled(True)
        search.setFixedWidth(160)
        search.textChanged.connect(lambda _t: self._apply_archive_filter())
        self._archive_search = search
        tabs.setCornerWidget(search, Qt.TopRightCorner)

        def _on_close(_=0) -> None:
            self._archive_tabs = None
            self._archive_search = None
            self._archive_undo_btn = None
        dialog.finished.connect(_on_close)
        layout.addWidget(tabs)

        # ---- Bottom bar: display-mode toggle + Close ---------------
        # Left-aligned controls for changing how the Duration column
        # is formatted; right-aligned Close. A horizontal stretch in
        # between pushes Close to the right edge so it remains where
        # the user expects.
        bottom = QHBoxLayout()

        bottom.addWidget(QLabel("Display:"))
        mode_combo = QComboBox()
        # userData carries the canonical token written to config so
        # changing the visible label later doesn't break persisted
        # values.
        mode_combo.addItem("Hours", userData="hours")
        mode_combo.addItem("Workdays", userData="workdays")
        current_mode = self.config.get("archive_display_mode", "hours")
        mode_combo.setCurrentIndex(0 if current_mode == "hours" else 1)
        bottom.addWidget(mode_combo)

        bottom.addSpacing(12)

        bottom.addWidget(QLabel("Hours/day:"))
        hpd_spin = QSpinBox()
        # 1..23 — a "day" with 0 or 24+ hours collapses the meaning
        # of the format. 8 is the default workday.
        hpd_spin.setRange(1, 23)
        hpd_spin.setValue(int(self.config.get("archive_hours_per_day", 8)))
        # Only meaningful when the Workdays mode is selected — disable
        # when in Hours mode so it's visually clear the value isn't
        # being applied.
        hpd_spin.setEnabled(current_mode == "workdays")
        bottom.addWidget(hpd_spin)

        bottom.addStretch(1)

        # Undo (spec §5): circular-arrow icon, greyed when the stack is
        # empty. Same global storage.undo() the menu item uses, so it
        # reverts the last CSV mutation from any surface. _refresh_archive
        # rebuilds the tabs and re-syncs this button's enabled state.
        undo_btn = QPushButton()
        undo_btn.setIcon(_undo_arrow_icon())
        undo_btn.setToolTip("Undo")
        undo_btn.setAccessibleName("Undo")   # alt-text equivalent for Qt
        undo_btn.setEnabled(storage.can_undo())
        undo_btn.clicked.connect(self._on_archive_undo)
        self._archive_undo_btn = undo_btn
        bottom.addWidget(undo_btn)

        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        bottom.addWidget(close)

        layout.addLayout(bottom)

        # ---- Wire change handlers ---------------------------------
        def _on_mode_changed(idx: int) -> None:
            new_mode = mode_combo.itemData(idx)
            self.config["archive_display_mode"] = new_mode
            storage.save_config(self.config)
            hpd_spin.setEnabled(new_mode == "workdays")
            self._rebuild_archive_tabs()

        def _on_hpd_changed(val: int) -> None:
            self.config["archive_hours_per_day"] = int(val)
            storage.save_config(self.config)
            # Only repopulate when the change is actually visible —
            # in Hours mode the value is parked, not applied.
            if self.config.get("archive_display_mode") == "workdays":
                self._rebuild_archive_tabs()

        mode_combo.currentIndexChanged.connect(_on_mode_changed)
        hpd_spin.valueChanged.connect(_on_hpd_changed)

        self._rebuild_archive_tabs()
        dialog.exec()

    def _archive_tab_order(self) -> List[str]:
        """Tags for the per-tag tabs, most-recently-active first (§6a).

        Recency = the tag's newest session date. Ties broken by name so
        the order is deterministic rather than dependent on CSV order.
        """
        last_seen: Dict[str, str] = {}
        for s in storage.load_sessions():
            if s.date > last_seen.get(s.tag, ""):
                last_seen[s.tag] = s.date
        return sorted(last_seen, key=lambda t: (last_seen[t], t), reverse=True)

    def _tag_color(self, tag: str) -> QColor:
        """THE resolver for a tag's archive colour. One function, called
        by every surface that paints a tag — All-tab chips, section and
        Total rows, the Tags overview, per-tag tab labels (selected and
        not) and their Total rows. Nothing computes a tag colour any
        other way; that duplication was what let one tag show two colours.

        A user override in tag_color_overrides wins. Otherwise the tag
        takes a slot in the 16-hue palette, indexed by one global order —
        recency of last activity, the same order the tabs use — so the
        colour never depends on which tab or section is asking, or on a
        filtered per-tab session list. NOT tag_schemes: that is the
        widget's colour-scheme system, unrelated (resolved earlier).

        Overridden tags do not consume a palette slot, so adding or
        clearing one override never reshuffles the other tags' colours.
        """
        overrides = self.config.get("tag_color_overrides", {})
        override = overrides.get(tag)
        if override:
            return QColor(override)
        idx = 0
        for t in self._archive_tab_order():
            if t in overrides:
                continue  # overrides don't take a palette slot
            if t == tag:
                return _ARCHIVE_TAG_PALETTE[idx % len(_ARCHIVE_TAG_PALETTE)]
            idx += 1
        # A tag with no stored sessions can't be ordered; fall back to
        # the first hue rather than raising.
        return _ARCHIVE_TAG_PALETTE[0]

    def _make_archive_tree(self) -> QTreeWidget:
        """Build a wired archive tree — the widget behind every tab.

        Extracted so the All tab and each per-tag tab are configured
        identically; only the content differs, via _populate_archive_tree's
        tag_filter. The context menu is wired per tree because the handler
        needs the specific tree the click landed in.
        """
        tree = QTreeWidget()
        tree.setHeaderLabels(["Date", "Tag", "Session", "Duration"])
        tree.setColumnWidth(0, 90)
        tree.setColumnWidth(1, 130)
        tree.setColumnWidth(2, 220)
        tree.setRootIsDecorated(True)
        # Qt's default indentation (~20 px on Windows) leaves an
        # awkward gutter on the left of every leaf row — there's no
        # per-row disclosure widget to fill it, and the date text
        # ends up starting well into the column. 12 px keeps the
        # hierarchy visible without wasting the column.
        tree.setIndentation(12)
        # Tag-based backgrounds replace alternating row colors — leaving
        # Qt's alternating scheme on top would tint our explicit
        # setBackground colors on every other row.
        tree.setAlternatingRowColors(False)
        # All custom painting (section-header strokes, leaf-row
        # separator, and selection coloring) is routed through the
        # delegate. NO `::item` stylesheet rules — any rule there
        # would trip Qt into stylesheet mode and suppress the per-
        # item BackgroundRole colors that carry the tag tints.
        tree.setItemDelegate(_ArchiveItemDelegate(tree))
        tree.setContextMenuPolicy(Qt.CustomContextMenu)
        tree.customContextMenuRequested.connect(
            lambda pos, t=tree: self._archive_context_menu(t, pos)
        )
        return tree

    def _rebuild_archive_tabs(self) -> None:
        """Rebuild the whole tab set from storage, preserving selection.

        Called on open and after any change that can alter the tab set —
        a tag deleted, merged, renamed, or a session edited. Rebuilding
        wholesale rather than patching one tree keeps the tab list correct
        when tags appear or vanish; the cost is trivial at these sizes.

        Selection is preserved by the tab's identity (its tag name, or
        "All"), not its index, since indices shift as tags come and go.
        """
        tabs = self._archive_tabs
        if tabs is None:
            return

        previous = tabs.tabText(tabs.currentIndex()) if tabs.count() else "All"
        # Strip the " Dd Hh" suffix off a per-tag label to recover the tag.
        prev_tag = previous.split("   ")[0] if previous != "All" else "All"

        tabs.blockSignals(True)
        while tabs.count():
            tabs.removeTab(0)

        all_tree = self._make_archive_tree()
        tabs.addTab(all_tree, "All")
        self._populate_archive_tree(all_tree)

        totals: Dict[str, int] = {}
        for s in storage.load_sessions():
            totals[s.tag] = totals.get(s.tag, 0) + s.minutes

        select_index = 0
        for tag in self._archive_tab_order():
            tree = self._make_archive_tree()
            self._populate_archive_tree(tree, tag_filter=tag)
            # Plain default label text — the tag's colour identity lives in
            # the row backgrounds (like the All tab), not the tab label.
            # Lifetime total in the label; per-month subtotals in the tree.
            label = f"{tag}   {storage.format_tag_total(totals.get(tag, 0))}"
            index = tabs.addTab(tree, label)
            if tag == prev_tag:
                select_index = index

        self._apply_archive_filter()
        tabs.blockSignals(False)
        tabs.setCurrentIndex(select_index)
        self._sync_archive_undo_button()

    def _apply_archive_filter(self) -> None:
        """Hide per-tag tabs whose name doesn't contain the search text.

        The All tab (index 0) always stays visible — it is the fallback,
        not a tag. Filtering only changes which tabs are reachable; it
        never changes the current tab, matching the scroll-arrows rule
        that browsing the strip must not move the selection (§6a).
        """
        search = self._archive_search
        query = search.text().strip().lower() if search is not None else ""
        tabs = self._archive_tabs
        if tabs is None:
            return
        bar = tabs.tabBar()
        for i in range(1, tabs.count()):
            bar.setTabVisible(i, query in tabs.tabText(i).lower())

    def _refresh_archive(self) -> None:
        """Rebuild the archive if it is open; no-op otherwise.

        The single refresh entry point for archive mutation helpers,
        replacing their old per-tree repopulate so an edit updates every
        tab and the tab set at once.
        """
        if self._archive_tabs is not None:
            self._rebuild_archive_tabs()

    def _populate_archive_tree(
        self, tree: QTreeWidget, tag_filter: Optional[str] = None,
    ) -> None:
        """Build (or rebuild) the tree contents from current storage state.

        With `tag_filter` set (a per-tag tab), only that tag's sessions
        are shown. Rows carry the tag's background colour just like the
        All tab; the bottom Tags overview is dropped (a one-row summary of
        the tab you are already on), and its lifetime total lives in the
        tab label. Per-month Total rows stay; only the Recent overlay goes
        subtotal-free. With no filter (the All tab) the view is exactly as
        it was before tabs existed.
        """
        tree.clear()
        sessions = storage.load_sessions()
        if tag_filter is not None:
            sessions = [s for s in sessions if s.tag == tag_filter]
        if not sessions:
            msg = (
                "(no sessions for this tag)" if tag_filter is not None
                else "(no saved sessions yet)"
            )
            tree.addTopLevelItem(QTreeWidgetItem([msg]))
            return

        # Sort newest first. Ties broken by session_name for stability.
        ordered = sorted(
            sessions,
            key=lambda s: (s.date, s.session_name),
            reverse=True,
        )
        recent = ordered[:5]

        # Tag → colour cache for this view, filled entirely from the one
        # resolver so every section paints from the same source. It used
        # to be computed inline here with its own palette-index counter,
        # which is exactly the duplicate that made a tag show one colour
        # in a chip and another on its tab. Keyed by every tag present so
        # a per-tag tab's rows resolve against the global order, not the
        # single-tag filtered list.
        tag_colors: Dict[str, QColor] = {
            s.tag: self._tag_color(s.tag) for s in ordered
        }

        # Every row carries its tag's background colour, in both the All
        # tab and per-tag tabs — a per-tag tab is one solid colour block,
        # matching how the All tab tints that tag's group. (This reverses
        # the earlier "no chips in per-tag tabs" call.)
        #
        # Total rows: the All tab and per-tag Year→Month groups both keep
        # their per-month subtotals, which the single lifetime figure
        # hides. Only the per-tag Recent overlay stays subtotal-free — a
        # subtotal of the latest five under the header adds nothing.
        recent_totals = tag_filter is None

        # --- Recent (always expanded, no nesting) ---
        if recent:
            recent_root = QTreeWidgetItem(["Recent"])
            self._make_header_bold(recent_root)
            tree.addTopLevelItem(recent_root)
            self._add_tag_groups(
                recent_root, recent, tag_colors,
                show_chips=True, show_totals=recent_totals,
            )
            recent_root.setExpanded(True)

        # --- Year → Month groups for everything ---
        # Iterates ALL ordered sessions (was previously `rest` = ordered[5:]).
        # Recent is a quick-access overlay on top of the chronological tree,
        # not a list that drains sessions from it. So the same session can
        # appear in Recent AND in its Year/Month group — and monthly Total
        # rows correctly reflect all sessions in that month, including any
        # that are also in Recent. Group by year, then within each year by
        # month. Both sorted desc.
        by_year: Dict[str, Dict[str, List[storage.SessionRow]]] = {}
        for s in ordered:
            year = s.date[:4]
            month = s.date[5:7]
            by_year.setdefault(year, {}).setdefault(month, []).append(s)

        for year in sorted(by_year.keys(), reverse=True):
            # Display year as the full 4 digits — section headers are
            # their own anchor and read more clearly than a bare "26".
            # Inner leaf-row dates still use YY-MM-DD for column
            # compactness; the duplication isn't a concern there
            # because the year section above already tells the user
            # which century-decade they're in.
            year_item = QTreeWidgetItem([year])
            self._make_header_bold(year_item)
            tree.addTopLevelItem(year_item)
            months = by_year[year]
            for month in sorted(months.keys(), reverse=True):
                # calendar.month_name is 1-indexed: ['', 'January', ...].
                month_item = QTreeWidgetItem([calendar.month_name[int(month)]])
                year_item.addChild(month_item)
                self._add_tag_groups(
                    month_item, months[month], tag_colors,
                    show_chips=True, show_totals=True,
                )
            # Years collapsed by default — keeps the recent list dominant.

        # --- Tags overview (lifetime totals per tag) -----------------
        # A flat per-tag summary at the bottom of the archive, sorted
        # by lifetime total descending so the user's heaviest-tracked
        # tags appear first. Same tag → color mapping as the rest of
        # the archive, so visual identity carries across sections.
        # An empty 24-px spacer row above provides graphical
        # separation from the year/month tree (more breathing room
        # than the standard 1-px section-stroke divider).
        #
        # Skipped in a per-tag tab: a one-row overview of the tag whose
        # tab you are already on is noise, and its lifetime total is in
        # the tab label (§6b).
        if tag_filter is not None:
            return

        spacer = QTreeWidgetItem([])
        spacer.setSizeHint(0, QSize(0, 24))
        # Non-selectable so clicking the gap doesn't highlight an
        # empty row. Keeps default enabled flag so it still paints
        # normally (no dimmed look).
        spacer.setFlags(spacer.flags() & ~Qt.ItemIsSelectable)
        tree.addTopLevelItem(spacer)

        tags_root = QTreeWidgetItem(["Tags"])
        self._make_header_bold(tags_root)
        tree.addTopLevelItem(tags_root)

        # Compute lifetime totals across ALL sessions, then sort
        # descending by minutes so the most-tracked tags are at the
        # top of the section.
        tag_totals: Dict[str, int] = {}
        for s in ordered:
            tag_totals[s.tag] = tag_totals.get(s.tag, 0) + s.minutes
        for tag, total in sorted(
            tag_totals.items(), key=lambda kv: kv[1], reverse=True,
        ):
            row = QTreeWidgetItem(["", tag, "", self._archive_format_duration(total)])
            brush = QBrush(tag_colors[tag])
            for col in range(4):
                row.setBackground(col, brush)
            # No UserRole data — the archive context menu's session-
            # name lookup returns None on this row, so right-click is
            # a safe no-op (matches the per-section Total rows). These
            # are summary views, not editable session entries.
            tags_root.addChild(row)
        tags_root.setExpanded(True)

    def _add_tag_groups(
        self,
        parent_item: QTreeWidgetItem,
        sessions: List[storage.SessionRow],
        tag_colors: Dict[str, QColor],
        show_chips: bool = True,
        show_totals: bool = True,
    ) -> None:
        """Insert sessions under `parent_item`, grouped by tag.

        Each tag's sessions share a background color from `tag_colors`,
        the stable global mapping built once in `_populate_archive_tree`.
        The same tag has the same color in every section — the live
        session is not singled out; the Archive reviews sessions, and the
        widget already shows running vs paused.

        Tags with 2+ sessions in this section get a Total summary row
        at the end of their group, on the SAME color as the tag —
        bold-italic text on the label and value marks it as a summary
        without changing the row's color identity.

        Sessions within a tag are kept in the order they arrived
        (already date-sorted by the caller).

        `show_chips` False leaves session rows on the tree's default
        background. `show_totals` False omits the Total summary rows.
        """
        # Group by tag, preserving first-appearance order.
        by_tag: Dict[str, List[storage.SessionRow]] = {}
        tag_order: List[str] = []
        for s in sessions:
            if s.tag not in by_tag:
                by_tag[s.tag] = []
                tag_order.append(s.tag)
            by_tag[s.tag].append(s)

        for tag in tag_order:
            tag_sessions = by_tag[tag]
            brush = QBrush(tag_colors[tag])
            # Session rows, tinted with this tag's color. The live session
            # gets no special marker: the widget already shows running vs
            # paused, and the Archive is for reviewing sessions, so one
            # tag paints one colour everywhere with no exceptions.
            for s in tag_sessions:
                item = self._make_session_item(s)
                if show_chips:
                    for col in range(4):
                        item.setBackground(col, brush)
                parent_item.addChild(item)
            # Total row when 2+ sessions share this tag in this section.
            if show_totals and len(tag_sessions) >= 2:
                total_mins = sum(s.minutes for s in tag_sessions)
                # "Total" label sits in the Tag column (col 1), not
                # the Session column — this row summarizes a tag, so
                # the label belongs in the same column where each
                # session's tag name appears. The Session column is
                # blank.
                total_item = QTreeWidgetItem(
                    ["", "Total", "",
                     self._archive_format_duration(total_mins)]
                )
                # SAME color as the tag — the bold-italic label / value
                # carries the "this is a summary" signal, no separate
                # background needed.
                for col in range(4):
                    total_item.setBackground(col, brush)
                f = total_item.font(1)
                f.setBold(True)
                f.setItalic(True)
                total_item.setFont(1, f)  # Tag column ("Total" label)
                total_item.setFont(3, f)  # Duration column (value)
                # No UserRole data — the context menu's session_name
                # lookup returns None for this row, so right-click is
                # a safe no-op.
                parent_item.addChild(total_item)

    def _make_header_bold(self, item: QTreeWidgetItem) -> None:
        """Bold the first column of a group header so it reads as a
        section rather than a row."""
        f = item.font(0)
        f.setBold(True)
        item.setFont(0, f)

    def _make_session_item(self, s: storage.SessionRow) -> QTreeWidgetItem:
        """Build a leaf row for a single session. Stashes the
        session_name in UserRole on column 0 so context-menu handlers
        can find the storage key from any selection.

        Date column is shown YY-MM-DD (century dropped) to keep the
        column compact. The full date stays in storage and in the
        session_name; this is purely a display tweak.

        Duration column on a leaf session row ALWAYS uses calendar-
        day formatting (`_format_dhm` directly, not the archive
        helper). The Hours / Workdays toggle is meaningful for
        TOTALS — accumulated time across many sessions — where the
        question "how many workdays does this represent" makes
        sense. A single day's record reads more naturally as hours
        + minutes regardless of the toggle. So the toggle is scoped
        to per-section Total rows and the Tags overview only.
        """
        date_short = s.date[2:] if len(s.date) >= 4 else s.date
        item = QTreeWidgetItem(
            [date_short, s.tag, s.session_name, _format_dhm(s.minutes)]
        )
        item.setData(0, Qt.UserRole, s.session_name)
        return item

    def _archive_context_menu(self, tree: QTreeWidget, pos: QPoint) -> None:
        """Right-click menu on session rows. No-op on group headers."""
        item = tree.itemAt(pos)
        if item is None:
            return
        session_name = item.data(0, Qt.UserRole)
        if not session_name:
            return  # group header
        tag = item.text(1)
        menu = QMenu(tree)
        # Session-row edits — act on this specific session.
        menu.addAction("Rename session",
                       lambda: self._archive_rename(tree, session_name))
        menu.addAction("Retag",
                       lambda: self._archive_retag(tree, session_name))
        menu.addAction("Retime",
                       lambda: self._archive_retime(tree, session_name))
        if tag:
            menu.addSeparator()
            # Tag-level edits — affect every session sharing this tag.
            menu.addAction("Rename tag",
                           lambda: self._archive_rename_tag(tree, tag))
            # Change color submenu — 16 palette swatches with names,
            # plus a "Reset to default" footer that removes any user
            # override and lets the auto-assign cycle pick again.
            color_menu = menu.addMenu("Change color")
            current_hex = (
                self.config.get("tag_color_overrides", {}).get(tag, "")
            ).lower()
            for color, name in zip(
                _ARCHIVE_TAG_PALETTE, _ARCHIVE_TAG_PALETTE_NAMES,
            ):
                hex_str = color.name()  # "#rrggbb" lowercase
                action = color_menu.addAction(
                    _color_swatch_icon(color), name,
                    lambda c=hex_str: self._archive_set_tag_color(
                        tree, tag, c,
                    ),
                )
                action.setCheckable(True)
                action.setChecked(hex_str == current_hex)
            color_menu.addSeparator()
            reset_action = color_menu.addAction(
                "Reset to default",
                lambda: self._archive_set_tag_color(tree, tag, None),
            )
            # Disable Reset when there's nothing to reset (no override
            # set for this tag) — keeps the menu honest about state.
            reset_action.setEnabled(bool(current_hex))
        menu.addSeparator()
        menu.addAction("Delete",
                       lambda: self._archive_delete(tree, session_name))
        menu.exec(tree.viewport().mapToGlobal(pos))

    def _archive_set_tag_color(
        self, tree: QTreeWidget, tag: str, color_hex: Optional[str],
    ) -> None:
        """Persist a user-chosen color for `tag` and refresh the tree.

        `color_hex` of None clears the override entry — the tag falls
        back to auto-assignment from _ARCHIVE_TAG_PALETTE on the next
        repopulate. Otherwise expects a lower-case "#rrggbb" string
        from the palette (we don't validate further; the menu only
        offers palette entries).
        """
        overrides = self.config.get("tag_color_overrides", {})
        if color_hex is None:
            overrides.pop(tag, None)
        else:
            overrides[tag] = color_hex
        self.config["tag_color_overrides"] = overrides
        storage.save_config(self.config)
        self._refresh_archive()

    def _archive_rename_tag(self, tree: QTreeWidget, tag: str) -> None:
        """Wrap on_rename_tag with a post-rename tree refresh so the
        archive immediately reflects the renamed tag and any merged
        rows. The handler itself doesn't know about the tree."""
        self.on_rename_tag(old_tag=tag)
        self._refresh_archive()

    def _archive_rename(self, tree: QTreeWidget, session_name: str) -> None:
        new_name, ok = QInputDialog.getText(
            tree.window(), "Rename session",
            "New name:",
            text=session_name,
        )
        if ok and new_name.strip() and new_name.strip() != session_name:
            storage.rename_session(session_name, new_name.strip())
            self._refresh_archive()

    def _archive_retag(self, tree: QTreeWidget, session_name: str) -> None:
        """Switch the tag of one session. Picker shows existing tags
        only — the brief's separation of concerns (tags created via
        web editor, picked elsewhere) applies here too."""
        existing = sorted(storage.tag_totals().keys())
        if not existing:
            QMessageBox.information(
                tree.window(), "Retag",
                "No tags available — add some via the web editor first.",
            )
            return
        # Pre-select the session's current tag if it's in the list.
        current = next(
            (s.tag for s in storage.load_sessions()
             if s.session_name == session_name),
            "",
        )
        idx = existing.index(current) if current in existing else 0
        new_tag, ok = QInputDialog.getItem(
            tree.window(), "Retag session",
            "New tag:",
            existing, idx, editable=False,
        )
        if ok and new_tag and new_tag != current:
            storage.retag_session(session_name, new_tag)
            self._refresh_carry_from_storage()
            self._refresh_archive()

    def _archive_delete(self, tree: QTreeWidget, session_name: str) -> None:
        reply = QMessageBox.question(
            tree.window(), "Delete session",
            f"Delete '{session_name}'?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            storage.delete_session(session_name)
            self._refresh_carry_from_storage()
            self._refresh_archive()

    def _archive_retime(self, tree: QTreeWidget, session_name: str) -> None:
        """Change the recorded time on a specific session row.

        Replaces the row's minutes outright (storage.set_minutes_for_
        tag_date is a replace, not an add). The session's session_name
        is preserved — Retime is purely about the duration field.

        Setting the new value to zero drops the row (the no-row-for-
        empty-day invariant); the tree picks that up on the next
        repopulate.

        If this session is for (active tag, today), the carry is
        re-seeded so the widget's display reflects the change."""
        target = next(
            (s for s in storage.load_sessions()
             if s.session_name == session_name),
            None,
        )
        if target is None:
            return
        new_text, ok = QInputDialog.getText(
            tree.window(), "Retime",
            f"New time for '{session_name}':",
            text=_format_dhm(target.minutes),
        )
        if not ok or not new_text.strip():
            return
        new_mins = _parse_dhm(new_text)
        storage.set_minutes_for_tag_date(
            target.tag, target.date, new_mins,
            session_name=target.session_name,
        )
        self._refresh_carry_from_storage()
        self._refresh_archive()

    # ---- Idle / sleep handlers ------------------------------------------

    def on_idle_detected(self, last_input_unix: float) -> None:
        """Brief §3: pause + backdate, then restore widget (brief §6).

        Also turns on the widget's rust auto-pause indicator (a
        background tint that's distinct from the standard paused
        purple), so the user can tell at a glance that tracking
        stopped due to inactivity, not a deliberate click. The
        indicator clears on the first mouse-enter, OR on any
        transition to RUNNING.
        """
        last_input = datetime.fromtimestamp(last_input_unix)
        self.tracker.idle_pause(last_input=last_input)
        self._update_running_state()
        self.widget.set_auto_paused(True)
        self.widget.ensure_on_screen()

    def on_sleep_gap(self, gap_start_unix: float, gap_end_unix: float) -> None:
        """Brief §3: discard the sleep gap from any running stretch.

        Pause at gap start, resume at gap end. If we weren't running when
        the gap happened, nothing to do — the gap was already excluded.
        """
        if self.tracker.state != State.RUNNING:
            return
        self.tracker.pause(at=datetime.fromtimestamp(gap_start_unix))
        self.tracker.resume(now=datetime.fromtimestamp(gap_end_unix))
        # State stays RUNNING net — widget / tray / idle don't need an
        # update_running_state ping. But the hour-chime timer may have
        # ticked late (or not at all) during sleep, so re-aim it at the
        # next wall-clock hour from this fresh `now`.
        self._schedule_hour_chime()

    # ---- Webserver bridge -----------------------------------------------

    def _read_rows_for_web(self) -> List[Dict[str, Any]]:
        return [r._asdict() for r in storage.load_sessions()]

    def _write_rows_for_web(self, rows: List[Dict[str, Any]]) -> None:
        sessions = [storage.SessionRow(**r) for r in rows]
        storage.save_sessions(sessions)
        # Carry was seeded from storage when the active tag was set;
        # any web edit to today's (active-tag) row leaves the widget
        # display stale. Re-seed now so the next paint reflects the
        # saved total.
        self._refresh_carry_from_storage()

    def _undo_for_web(self) -> bool:
        """Web editor's POST /api/undo: undo the last CSV mutation (§5).

        Same global storage.undo() the menu and Archive button use, so it
        reverts the most recent change from any surface. Returns whether a
        snapshot was popped. Re-seeds the carry for the same reason the web
        write path does — the restored CSV may hold a different total for
        the live (tag, today) row than the widget is showing. Runs on the
        Flask thread, matching _write_rows_for_web's existing behaviour.
        """
        if not storage.undo():
            return False
        self._refresh_carry_from_storage()
        return True

    def _tag_color_for_web(self, tag: str) -> str:
        """Web editor's tag colour for the filter chips: the archive's
        _tag_color as a "#rrggbb" string (the resolver returns a QColor)."""
        return self._tag_color(tag).name()

    def _rename_tag_for_web(self, old_tag: str, new_tag: str) -> bool:
        """Web wrapper for storage.rename_tag that also refreshes the
        carry — same reasoning as _write_rows_for_web above."""
        affected = storage.rename_tag(old_tag, new_tag)
        if affected:
            # The MRU follows the rename regardless of what's live, so
            # the picker can't keep offering the old name.
            storage.rename_tag_in_config(self.config, old_tag, new_tag)
            storage.save_config(self.config)
            # The active tracker tag itself may have been renamed; if
            # so, re-point it before re-seeding the carry.
            if self.tracker.tag == old_tag:
                self.tracker.set_tag(new_tag)
            self._refresh_carry_from_storage()
        return affected

    def _refresh_carry_from_storage(self) -> None:
        """Re-seed _carry_seconds from storage for the active tag's
        today row, then ask the widget to repaint.

        Called after any storage mutation that could have changed the
        live (tag, today) row: web saves, web tag renames, archive
        retag / delete, and add-record. No-op when there's no active
        tag. The widget.update() at the end ensures the display
        reflects the new value even when the widget is paused (no
        running refresh tick) or not currently hovered."""
        if self.tracker.tag is None:
            return
        self._carry_seconds = (
            storage.today_minutes_for_tag(
                self.tracker.tag, self._today_str(),
            ) * 60
        )
        self.widget.update()

    # ---- Quit ------------------------------------------------------------

    def on_quit(self) -> None:
        """Offer to save unsaved work before tearing down."""
        if self._has_active_session() and self.tracker.tag is not None:
            reply = QMessageBox.question(
                self.widget, "Quit",
                "Save current session before quitting?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if reply == QMessageBox.Cancel:
                return
            if reply == QMessageBox.Yes:
                self.on_save_session()  # clears snapshot internally
            else:
                # No = explicit discard. Clear the snapshot so the
                # next launch doesn't offer to recover work the user
                # just told us to throw away.
                storage.clear_active_snapshot()
        self.qapp.quit()

    # ---- Run -------------------------------------------------------------

    def run(self) -> int:
        # Make Ctrl+C cleanly quit during development. Qt's event loop is
        # written in C and doesn't return to the Python interpreter often
        # enough for Python's default SIGINT handler to fire — we install
        # an explicit one that calls quit(), and a 200 ms keep-alive timer
        # gives the interpreter regular opportunities to notice the signal.
        signal.signal(signal.SIGINT, lambda *_: self.qapp.quit())
        self._sigint_keepalive = QTimer(self.qapp)
        self._sigint_keepalive.start(200)
        self._sigint_keepalive.timeout.connect(lambda: None)
        return self.qapp.exec()


def main() -> int:
    return App(sys.argv).run()


if __name__ == "__main__":
    sys.exit(main())
