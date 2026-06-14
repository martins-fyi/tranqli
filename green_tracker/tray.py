"""
tray.py — system-tray status indicator + shared right-click menu (brief §7, §11).

Two consumers:
- The widget's right-click — instantiates a transient `QMenu` per click via
  `show_context_menu`.
- The system-tray icon — keeps a persistent `QMenu` and refreshes it on
  `aboutToShow` so the menu always reflects current state (size-check, shape
  label, tag list).

Both go through `populate_menu(menu, ctx)`, which reads dynamic state via
getter callables and fires actions via callable fields on the supplied
`MenuContext`. That keeps `tray.py` ignorant of tracker / storage / widget
internals — `main.py` does all the wiring.

Wiring (done in main.py):

    ctx = MenuContext(
        current_size=lambda: widget.size_name,
        current_shape=lambda: widget.shape,
        tag_lifetimes=storage.tag_lifetimes,
        has_active_session=tracker.is_running,
        save_session=on_save_session,
        set_tag=tracker.set_tag,
        prompt_new_tag=on_prompt_new_tag,
        rename_session=on_rename_session,
        delete_session=on_delete_session,
        set_size=widget.set_size,
        set_shape=widget.set_shape,
        open_archive=on_open_archive,
        open_csv_editor=webserver.open_in_browser,
        minimize_to_tray=widget.hide,
        quit_app=app.quit,
    )

    widget.right_clicked.connect(lambda pos: show_context_menu(pos, ctx))

    tray = TrayIcon(ctx)
    tray.left_clicked.connect(widget.ensure_on_screen)
    tray.setToolTip("Tranqli")
    tray.show()
    tracker.running_changed.connect(tray.set_running)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import (
    QActionGroup, QColor, QIcon, QPainter, QPixmap,
)
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from .widget import COLOR_SCHEMES, make_scheme_icon


# Tray icon — colours are 25% brighter than the widget's running/paused
# backgrounds so the tiny dot reads clearly against the system tray, while
# still echoing the widget's state-colour scheme.
TRAY_RUNNING_COLOR = QColor("#105B36")   # widget #0d492b × 1.25
TRAY_PAUSED_COLOR  = QColor("#584C88")   # widget #463D6D × 1.25
TRAY_ICON_PX       = 22                  # base size; Windows scales for HiDPI


# ---- Wiring contract ------------------------------------------------------

@dataclass
class MenuContext:
    """Callbacks + state getters that the menu needs. Constructed by main.py
    with bound methods/closures from tracker, storage, widget, etc.

    Getters are re-invoked each time the menu is rebuilt so the menu always
    reflects current state."""

    # ---- State getters ---------------------------------------------------
    current_size:       Callable[[], str]            # "small" | "medium" | "large"
    current_shape:      Callable[[], str]            # "rect"  | "circle"
    elapsed_seconds:    Callable[[], float]          # tracker's live elapsed
    tag_lifetimes:      Callable[[], Mapping[str, str]]  # tag -> "02d 02h"
    has_active_session: Callable[[], bool]           # gates Save / Set tag
    is_running:         Callable[[], bool]           # drives Save vs Stop&Save label

    # ---- Action callbacks ------------------------------------------------
    save_session:     Callable[[], None]
    new_session:      Callable[[], None]             # main.py prompts to save first
    set_tag:          Callable[[str], None]          # existing tag chosen
    prompt_new_tag:   Callable[[], None]             # main.py opens input dialog
    rename_tag:       Callable[[str], None]          # main.py prompts for new name
    add_record:       Callable[[str], None]          # main.py opens AddRecordDialog
    rename_session:   Callable[[], None]             # rename current in-progress session
    retime_session:   Callable[[], None]             # retime current in-progress session
    delete_session:   Callable[[], None]             # main.py opens picker
    set_size:         Callable[[str], None]
    set_shape:        Callable[[str], None]
    # Colour scheme getters/setters. current_scheme returns the
    # config key ("earthen", "twilight", ...); set_color_scheme
    # takes one and tells main.py to persist + apply.
    current_scheme:   Callable[[], str]
    set_color_scheme: Callable[[str], None]
    open_archive:     Callable[[], None]
    open_csv_editor:  Callable[[], None]
    minimize_to_tray: Callable[[], None]
    quit_app:         Callable[[], None]


# ---- Menu construction ----------------------------------------------------

def populate_menu(menu: QMenu, ctx: MenuContext) -> None:
    """Fill `menu` with the brief §7 actions, reflecting current state.

    Must be called fresh on each invocation — the tags list, size-check,
    and shape-toggle label all depend on live state."""

    active = ctx.has_active_session()
    running = ctx.is_running()
    tags = ctx.tag_lifetimes()

    # --- Save session / Stop & Save --------------------------------------
    # Label flips to "Stop & Save" when actively tracking — makes the
    # implicit pause-first step explicit. Same handler either way
    # (on_save_session already pauses before committing).
    save_label = "Stop & Save" if running else "Save session"
    save_action = menu.addAction(save_label, ctx.save_session)
    save_action.setEnabled(active)

    # --- New session -----------------------------------------------------
    # Always available. The handler prompts to save the current session
    # first if one is active (running or paused with data), then resets
    # the tracker so the next widget click re-opens the tag picker.
    menu.addAction("New session", ctx.new_session)

    # --- Set tag (submenu: existing tags only — new tags via web UI) ------
    set_tag_menu = menu.addMenu("Set tag")
    if tags:
        for tag in sorted(tags.keys()):
            # `t=tag` defaults the lambda's free var so each closure binds
            # its own tag — without this, every lambda would capture the
            # final loop value (Python closure-in-loop gotcha).
            set_tag_menu.addAction(tag, lambda t=tag: ctx.set_tag(t))
    set_tag_menu.setEnabled(active and bool(tags))

    # --- Tags submenu — per-tag actions nested under each tag's label.
    # Label shows lifetime total ("work    01h 30m"). Opening a tag's
    # entry reveals three actions: Rename tag, Add record, Open Archive.
    # The old "click tag = set as active" gesture is gone — that
    # functionality lives in the dedicated "Set tag" submenu just above.
    if tags:
        tags_menu = menu.addMenu("Tags")
        for tag in sorted(tags.keys()):
            label = f"{tag}    {tags[tag]}"
            tag_submenu = tags_menu.addMenu(label)
            tag_submenu.addAction(
                "Rename tag", lambda t=tag: ctx.rename_tag(t),
            )
            tag_submenu.addAction(
                "Add record", lambda t=tag: ctx.add_record(t),
            )
            tag_submenu.addAction("Open Archive", ctx.open_archive)

    menu.addSeparator()

    # --- Current-session actions / Delete --------------------------------
    # Rename session and Retime session act on the in-progress session
    # (active tag + today). For editing arbitrary past sessions, the
    # archive's right-click menu is the entry point.
    menu.addAction("Rename session", ctx.rename_session)
    menu.addAction("Retime session", ctx.retime_session)
    menu.addAction("Delete session", ctx.delete_session)

    menu.addSeparator()

    # --- Size submenu (radio group, current size checked) -----------------
    size_menu = menu.addMenu("Size")
    size_group = QActionGroup(size_menu)
    size_group.setExclusive(True)
    current_size = ctx.current_size()
    for size_name in ("small", "medium", "large"):
        a = size_menu.addAction(size_name.capitalize())
        a.setCheckable(True)
        a.setChecked(size_name == current_size)
        a.triggered.connect(lambda _checked=False, n=size_name: ctx.set_size(n))
        size_group.addAction(a)

    # --- Color schemes submenu (radio group, current scheme checked) ------
    # Each entry has a 3-circle icon preview (running / paused / auto-
    # pause for that scheme) followed by the scheme name. Qt scales
    # the 48×18 preview pixmap to the menu's default icon size
    # (~16 px on Windows, aspect-preserved → ~16×6) — the three
    # colours stay distinguishable at that size. QMenu doesn't expose
    # setIconSize() in PySide6, so we can't bump that — see commit
    # history if a larger preview becomes important.
    schemes_menu = menu.addMenu("Color schemes")
    schemes_group = QActionGroup(schemes_menu)
    schemes_group.setExclusive(True)
    current_scheme = ctx.current_scheme()
    for scheme in COLOR_SCHEMES.values():
        a = schemes_menu.addAction(scheme.name)
        a.setIcon(make_scheme_icon(scheme))
        a.setCheckable(True)
        a.setChecked(scheme.key == current_scheme)
        # Default arg locks the closure to this scheme's key — without
        # it every lambda would capture the final loop value.
        a.triggered.connect(
            lambda _checked=False, k=scheme.key: ctx.set_color_scheme(k)
        )
        schemes_group.addAction(a)

    # --- Shape toggle — only meaningful once the widget is showing HH:MM.
    # Below 1 h the rectangle mode collapses to a circle anyway, so the
    # menu option would be visually a no-op. Once in circle button mode,
    # always offer the way back regardless of elapsed time.
    shape = ctx.current_shape()
    elapsed = ctx.elapsed_seconds()
    if shape == "circle":
        menu.addAction("Switch to rectangle",
                       lambda: ctx.set_shape("rect"))
    elif elapsed >= 3600:
        menu.addAction("Switch to circle button",
                       lambda: ctx.set_shape("circle"))

    menu.addSeparator()

    # --- Archive / Web editor --------------------------------------------
    menu.addAction("Archive", ctx.open_archive)
    menu.addAction("Edit data (web)", ctx.open_csv_editor)

    menu.addSeparator()

    # --- Tray / Quit ------------------------------------------------------
    menu.addAction("Minimize to tray", ctx.minimize_to_tray)
    menu.addAction("Quit", ctx.quit_app)


def show_context_menu(pos: QPoint, ctx: MenuContext,
                      parent: Optional[QWidget] = None) -> None:
    """Build a transient menu and exec it at the given global position.

    Used by `widget.right_clicked` — each right-click builds a fresh menu
    rather than reusing one, so state captured in lambdas (current tag list,
    current size etc.) is always live."""
    menu = QMenu(parent)
    populate_menu(menu, ctx)
    menu.exec(pos)


# ---- System tray indicator ------------------------------------------------

class TrayIcon(QSystemTrayIcon):
    """System-tray indicator (brief §11). Colour mirrors RUNNING / PAUSED.

    Left-click emits `left_clicked` (wire to `widget.ensure_on_screen`).
    Right-click is handled by Qt via `setContextMenu`; the menu rebuilds
    its contents on each `aboutToShow` so state is always current."""

    left_clicked = Signal()

    def __init__(self, ctx: MenuContext,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._running = False
        self.setIcon(self._make_icon(running=False))

        # Persistent menu — refresh items on aboutToShow so the size-check,
        # shape label, and tag list always reflect current state.
        self._menu = QMenu()
        self._menu.aboutToShow.connect(self._refresh_menu)
        self.setContextMenu(self._menu)

        self.activated.connect(self._on_activated)

    # ---- Public API -----------------------------------------------------

    def set_running(self, running: bool) -> None:
        """Update the indicator colour to mirror the tracker state."""
        if running == self._running:
            return
        self._running = running
        self.setIcon(self._make_icon(running))

    # ---- Internal -------------------------------------------------------

    def _refresh_menu(self) -> None:
        self._menu.clear()
        populate_menu(self._menu, self._ctx)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Right-click → Qt shows _menu automatically (setContextMenu).
        # Trigger is left-click on Windows; restore the widget.
        if reason == QSystemTrayIcon.Trigger:
            self.left_clicked.emit()

    def _make_icon(self, running: bool) -> QIcon:
        size = TRAY_ICON_PX
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(TRAY_RUNNING_COLOR if running else TRAY_PAUSED_COLOR)
        painter.setPen(Qt.NoPen)
        # Inset by 1 px on each side so anti-aliased edge has room.
        painter.drawEllipse(1, 1, size - 2, size - 2)
        painter.end()
        return QIcon(pix)
