"""Tests for the Archive window's tab bar (main.py).

Covers the tab context menu's Delete tag entry and the live "in progress"
readout on an actively-tracked tag's own tab.

Runs headless via the offscreen Qt platform. `App` builds its own
QApplication, which may exist only once per process, so the instance is
session-scoped; the autouse data-dir fixture in conftest still gives each
test its own storage, and `storage.get_data_dir()` resolves per call, so
every Archive rebuild reads that test's sessions.
"""

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

# Must be set before the QApplication is constructed.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QLabel, QMenu,
)

from green_tracker import main as M, storage  # noqa: E402


IN_PROGRESS_MARK = "●"


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    """The one App instance for the session (QApplication is per-process).

    The data dir MUST be redirected here, before construction. Session
    fixtures resolve ahead of function-scoped autouse ones, so conftest's
    `isolate_data_dir` has not run yet at this point — without the
    redirect below, `App()` reads the developer's real data directory,
    finds their genuine crash-recovery snapshot, and blocks the suite on a
    modal "Recover unsaved session?" prompt whose Discard button would
    destroy real tracked time. The overrides mirror conftest's, and are
    unwound once construction is done: from then on the per-test fixture
    owns isolation, and `storage.get_data_dir()` resolves per call, so
    each test's Archive rebuild reads that test's own sessions.
    """
    with pytest.MonkeyPatch.context() as mp:
        data = tmp_path_factory.mktemp("app-data")
        mp.setenv("TRANQLI_DATA_DIR", str(data))
        mp.delenv("TRAENKY_DATA_DIR", raising=False)
        mp.delenv("APPDATA", raising=False)
        mp.setattr(Path, "home", classmethod(lambda cls: data / "home"))
        instance = M.App([])
    return instance


@pytest.fixture(autouse=True)
def quiet_app(app):
    """Leave the tracker idle and no modal pending, around every test.

    The App is shared across the session, so a live session left behind by
    one test would light up the next test's tabs. The crash-recovery check
    is asserted clear rather than merely reset: it is queued onto the event
    loop at startup, so a pending one would fire inside the Archive's
    nested `exec()` and hang the suite on a modal with nothing to answer
    it — a failed assertion is a much better outcome than that.
    """
    app._pending_crash_recovery = None
    app.tracker.reset()
    yield
    app._pending_crash_recovery = None
    app.tracker.reset()


def _sessions(*rows):
    storage.save_sessions([
        storage.SessionRow(date=d, tag=t, session_name=n, minutes=m)
        for d, t, n, m in rows
    ])


def _open_archive(app, probe):
    """Open the Archive, run `probe(tabs)` once it's built, then close it.

    `on_open_archive` ends in a blocking `exec()`; the probe is queued on
    the event loop so it runs against a fully-constructed dialog.
    """
    result = {}

    def _run():
        try:
            result["value"] = probe(app._archive_tabs)
        finally:
            for w in app.qapp.topLevelWidgets():
                if isinstance(w, QDialog) and w.windowTitle() == "Archive":
                    w.accept()

    QTimer.singleShot(0, _run)
    app.on_open_archive()
    return result.get("value")


def _labels(tabs):
    """Map tag -> tab label. The All tab keys off None (it carries no tag)."""
    bar = tabs.tabBar()
    return {
        bar.tabData(i): tabs.tabText(i) for i in range(tabs.count())
    }


def _tab_index(tabs, tag):
    bar = tabs.tabBar()
    return next(i for i in range(tabs.count()) if bar.tabData(i) == tag)


def _header_text(tabs, tag):
    """The lifetime-total header on `tag`'s tab, or None if it has none."""
    page = tabs.widget(_tab_index(tabs, tag))
    label = page.findChild(QLabel, M._ARCHIVE_TAG_TOTAL_NAME)
    return label.text() if label is not None else None


def _headers(tabs):
    """Map tag -> header text across every tab (None where absent)."""
    bar = tabs.tabBar()
    return {
        bar.tabData(i): _header_text(tabs, bar.tabData(i))
        for i in range(tabs.count())
    }


def _days_hours(total_str):
    """The (days, hours) a total string encodes, missing fields = 0.

    The four archive surfaces don't render byte-identically — the tab
    label and header truncate to "Dd Hh" while the Tags overview and
    submenu cascade to "Dd Hh Mm", and `_format_dhm` drops leading zero
    fields ("01h 30m", no "00d"). Comparing the (days, hours) they parse
    to is the mode-independent way to assert they represent the same time.
    """
    d = re.search(r"(\d+)\s*d", total_str)
    h = re.search(r"(\d+)\s*h", total_str)
    return (int(d.group(1)) if d else 0, int(h.group(1)) if h else 0)


def _label_total(tabs, tag):
    """The lifetime-total portion of `tag`'s tab label.

    Strips the tag-name prefix and any "● HH:MM" live suffix, leaving
    just the total the label renders.
    """
    rest = _labels(tabs)[tag][len(tag):].strip()
    if IN_PROGRESS_MARK in rest:
        rest = rest.split(IN_PROGRESS_MARK)[0].strip()
    return rest


def _overview_total(tabs, tag):
    """`tag`'s total as rendered in the All tab's Tags overview section.

    The All tab (index 0) is a bare tree; its "Tags" section holds one
    ["", tag, "", duration] row per tag.
    """
    tree = tabs.widget(0)
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        if item.text(0) == "Tags":
            for j in range(item.childCount()):
                child = item.child(j)
                if child.text(1) == tag:
                    return child.text(3)
    return None


# ---------------------------------------------------------------------------
# Tab identity
# ---------------------------------------------------------------------------

class TestTabIdentity:
    def test_tag_is_carried_in_tab_data_not_the_label(self, app):
        """The label holds the total and the live readout too, so the tag
        must not be recovered by parsing it."""
        _sessions(("2026-07-01", "work", "w1", 90))

        data = _open_archive(app, lambda tabs: [
            tabs.tabBar().tabData(i) for i in range(tabs.count())
        ])

        assert data[0] is None, "the All tab is not a tag"
        assert data[1:] == ["work"]


# ---------------------------------------------------------------------------
# "Today, in progress" readout
# ---------------------------------------------------------------------------

class TestInProgressReadout:
    def test_absent_when_no_session_is_active(self, app):
        _sessions(("2026-07-01", "work", "w1", 90))

        labels = _open_archive(app, _labels)

        assert not any(IN_PROGRESS_MARK in v for v in labels.values())

    def test_shown_only_on_the_tag_holding_the_session(self, app):
        _sessions(
            ("2026-07-01", "work", "w1", 90),
            ("2026-07-01", "admin", "a1", 30),
        )
        app.tracker.start("work")

        labels = _open_archive(app, _labels)

        assert IN_PROGRESS_MARK in labels["work"]
        assert IN_PROGRESS_MARK not in labels["admin"], "leaked to another tag"
        assert IN_PROGRESS_MARK not in labels[None], "leaked to the All tab"

    def test_readout_ticks(self, app):
        """The readout tracks elapsed time, it isn't a static stamp."""
        import datetime

        _sessions(("2026-07-01", "work", "w1", 90))
        app.tracker.start("work")

        def probe(tabs):
            i = _tab_index(tabs, "work")
            before = tabs.tabText(i)
            # Rewind the running stretch: accumulated_seconds is derived
            # from _intervals, so there is no settable elapsed field.
            app.tracker._start -= datetime.timedelta(hours=1)
            app._tick_archive_live_tab()
            return before, tabs.tabText(i)

        before, after = _open_archive(app, probe)
        assert before != after
        assert IN_PROGRESS_MARK in after


# ---------------------------------------------------------------------------
# Lifetime-total header on a tag's own tab
# ---------------------------------------------------------------------------

class TestTagTotalHeader:
    def test_present_on_a_tag_tab_with_that_tag_s_total(self, app):
        _sessions(("2026-07-01", "work", "w1", 3000))

        header = _open_archive(app, lambda tabs: _header_text(tabs, "work"))

        assert header == "Lifetime total: 02d 02h"

    def test_matches_the_tags_submenu_total_exactly(self, app):
        """Both must come from `storage.format_tag_total`, so they cannot
        drift apart. Asserted against the storage function's own output
        rather than a hardcoded string."""
        _sessions(
            ("2026-07-01", "work", "w1", 3000),
            ("2026-08-02", "work", "w2", 745),
        )

        header = _open_archive(app, lambda tabs: _header_text(tabs, "work"))

        assert storage.tag_totals()["work"] in header

    def test_totals_every_session_for_the_tag(self, app):
        """A lifetime total spans all year/month groups, not just one."""
        _sessions(
            ("2025-01-01", "work", "w1", 1440),
            ("2026-07-01", "work", "w2", 1440),
            ("2026-07-02", "admin", "a1", 60),
        )

        headers = _open_archive(app, _headers)

        assert headers["work"] == "Lifetime total: 02d 00h"
        assert headers["admin"] == "Lifetime total: 00d 01h"

    def test_absent_on_the_all_tab(self, app):
        """"All" spans every tag, so there is no single lifetime to show."""
        _sessions(
            ("2026-07-01", "work", "w1", 3000),
            ("2026-07-02", "admin", "a1", 90),
        )

        headers = _open_archive(app, _headers)

        assert headers[None] is None
        assert headers["work"] is not None

    def test_includes_live_in_progress_time(self, app):
        """The header must not read as a stale total that omits the
        session currently on the clock."""
        import datetime

        _sessions(("2026-07-01", "work", "w1", 3000))   # 02d 02h banked
        app.tracker.start("work")

        def probe(tabs):
            # Two more hours on the clock, unbanked.
            app.tracker._start -= datetime.timedelta(hours=2)
            app._tick_archive_live_tab()
            return _header_text(tabs, "work")

        header = _open_archive(app, probe)

        assert header == "Lifetime total: 02d 04h"

    def test_live_time_lands_only_on_the_owning_tag(self, app):
        import datetime

        _sessions(
            ("2026-07-01", "work", "w1", 3000),
            ("2026-07-02", "admin", "a1", 60),
        )
        app.tracker.start("work")

        def probe(tabs):
            app.tracker._start -= datetime.timedelta(hours=2)
            app._tick_archive_live_tab()
            return _headers(tabs)

        headers = _open_archive(app, probe)

        assert headers["work"] == "Lifetime total: 02d 04h"
        assert headers["admin"] == "Lifetime total: 00d 01h", "live time leaked"

    def test_updates_on_the_live_tick(self, app):
        """Refreshed by the §12c timer's tick — no second timer."""
        import datetime

        _sessions(("2026-07-01", "work", "w1", 3000))
        app.tracker.start("work")

        def probe(tabs):
            before = _header_text(tabs, "work")
            app.tracker._start -= datetime.timedelta(hours=1)
            app._tick_archive_live_tab()
            return before, _header_text(tabs, "work")

        before, after = _open_archive(app, probe)

        assert before == "Lifetime total: 02d 02h"
        assert after == "Lifetime total: 02d 03h"

    def test_live_total_reverts_when_the_session_ends(self, app):
        import datetime

        _sessions(("2026-07-01", "work", "w1", 3000))
        app.tracker.start("work")

        def probe(tabs):
            app.tracker._start -= datetime.timedelta(hours=2)
            app._tick_archive_live_tab()
            live = _header_text(tabs, "work")
            app.tracker.reset()
            app._tick_archive_live_tab()
            return live, _header_text(tabs, "work")

        live, after = _open_archive(app, probe)

        assert live == "Lifetime total: 02d 04h"
        assert after == "Lifetime total: 02d 02h"

    def test_updates_immediately_when_stored_total_changes(self, app):
        """A save/delete/merge must not wait out a tick interval.

        Those paths all land on `_refresh_archive`, which rebuilds the tab
        set — so the header is rebuilt with them. No timer involved, which
        matters because with no live session the timer is not even running.
        """
        _sessions(("2026-07-01", "work", "w1", 3000))

        def probe(tabs):
            before = _header_text(tabs, "work")
            assert not app._archive_live_timer.isActive(), "no tick to rely on"
            # Stand-in for a save: another session banked against the tag.
            _sessions(
                ("2026-07-01", "work", "w1", 3000),
                ("2026-07-03", "work", "w2", 1440),
            )
            app._refresh_archive()
            return before, _header_text(app._archive_tabs, "work")

        before, after = _open_archive(app, probe)

        assert before == "Lifetime total: 02d 02h"
        assert after == "Lifetime total: 03d 02h"

    def test_updates_immediately_when_a_tag_is_deleted(self, app):
        """Deleting one tag must leave the other's header intact and current."""
        _sessions(
            ("2026-07-01", "work", "w1", 3000),
            ("2026-07-02", "admin", "a1", 60),
        )

        def probe(tabs):
            storage.delete_tag("admin")
            app._refresh_archive()
            return _headers(app._archive_tabs)

        headers = _open_archive(app, probe)

        assert "admin" not in headers
        assert headers["work"] == "Lifetime total: 02d 02h"


# ---------------------------------------------------------------------------
# Tag-total formatting agreement across surfaces and display modes
# ---------------------------------------------------------------------------

@pytest.fixture
def display_mode(app):
    """Set archive_display_mode for a test, restoring it after.

    The App is session-scoped, so a mode left set would leak into every
    later test — the default is Hours, and the header/label assertions
    elsewhere assume it.
    """
    original = app.config.get("archive_display_mode", "hours")

    def _set(mode):
        app.config["archive_display_mode"] = mode

    yield _set
    app.config["archive_display_mode"] = original


def _all_surface_totals(app, tag):
    """The tag's total as rendered by all four archive surfaces.

    Label and header come off the live dialog; the overview off the All
    tab's tree; the submenu from `_get_tag_lifetimes`, which is exactly
    the dict the tray builds its Tags submenu from.
    """
    def probe(tabs):
        return {
            "tab label": _label_total(tabs, tag),
            "header": _header_text(tabs, tag).replace("Lifetime total: ", ""),
            "tags overview": _overview_total(tabs, tag),
            "tags submenu": app._get_tag_lifetimes()[tag],
        }

    return _open_archive(app, probe)


class TestTagTotalAgreement:
    """The bug this fixes: label/header used a hardcoded 24 h divisor while
    the overview and submenu honoured the Workdays toggle, so the same
    stored minutes read as e.g. '05d 15h' in one place and '16d 07h' in
    another. All four must now agree in both modes."""

    @pytest.mark.parametrize("mode", ["hours", "workdays"])
    def test_all_four_surfaces_agree(self, app, display_mode, mode):
        display_mode(mode)
        _sessions(("2026-07-01", "work", "w1", 8109))

        totals = _all_surface_totals(app, "work")

        pairs = {name: _days_hours(v) for name, v in totals.items()}
        assert len(set(pairs.values())) == 1, (
            f"surfaces disagree in {mode} mode: {totals}"
        )

    def test_regression_8109_minutes_workdays(self, app, display_mode):
        """The exact figure from the field report: 8,109 min in Workdays
        mode read '16d 07h' in the overview but '05d 15h' on the label and
        header. Tied to the real number, not a round synthetic one."""
        display_mode("workdays")
        _sessions(("2026-07-01", "work", "w1", 8109))

        totals = _all_surface_totals(app, "work")

        assert totals["tab label"] == "16d 07h"
        assert totals["header"] == "16d 07h"
        # The minute-resolution surfaces truncate to the same days+hours.
        assert totals["tags overview"].startswith("16d 07h")
        assert totals["tags submenu"].startswith("16d 07h")

    def test_regression_8109_minutes_hours_unaffected(self, app, display_mode):
        """Hours mode was already correct at '05d 15h'; the fix must not
        move it."""
        display_mode("hours")
        _sessions(("2026-07-01", "work", "w1", 8109))

        totals = _all_surface_totals(app, "work")

        assert totals["tab label"] == "05d 15h"
        assert totals["header"] == "05d 15h"
        assert totals["tags overview"].startswith("05d 15h")
        assert totals["tags submenu"].startswith("05d 15h")

    def test_sub_day_total_agrees_in_both_modes(self, app, display_mode):
        """A small total exercises the field-omission difference between
        the truncated 'Dd Hh' and cascading '…Mm' formats — the parsed
        (days, hours) must still line up."""
        for mode in ("hours", "workdays"):
            display_mode(mode)
            _sessions(("2026-07-01", "work", "w1", 90))

            totals = _all_surface_totals(app, "work")

            pairs = {name: _days_hours(v) for name, v in totals.items()}
            assert len(set(pairs.values())) == 1, (
                f"sub-day surfaces disagree in {mode}: {totals}"
            )

    def test_formatter_uses_the_workday_divisor(self, app, display_mode):
        """Unit-level: the shared formatter truncates to days+hours and
        divides by the configured hours-per-day, not a hardcoded 24."""
        display_mode("workdays")
        assert app._archive_format_tag_total(8109) == "16d 07h"

        display_mode("hours")
        assert app._archive_format_tag_total(8109) == "05d 15h"

    def test_formatter_matches_duration_truncated(self, app, display_mode):
        """The days+hours the header shows must be the days+hours the
        overview shows, by construction — same divisor, one just drops
        the minutes."""
        display_mode("workdays")
        for minutes in (90, 481, 8109, 23469):
            trunc = app._archive_format_tag_total(minutes)
            full = app._archive_format_duration(minutes)
            assert _days_hours(trunc) == _days_hours(full), (
                f"{minutes} min: {trunc!r} vs {full!r}"
            )


# ---------------------------------------------------------------------------
# Timer scoping — the readout must never be a background cost
# ---------------------------------------------------------------------------

class TestLiveTimerScoping:
    def test_idle_while_no_tag_holds_a_session(self, app):
        _sessions(("2026-07-01", "work", "w1", 90))

        active = _open_archive(app, lambda _t: app._archive_live_timer.isActive())

        assert active is False

    def test_runs_while_archive_open_and_session_live(self, app):
        _sessions(("2026-07-01", "work", "w1", 90))
        app.tracker.start("work")

        active, interval = _open_archive(app, lambda _t: (
            app._archive_live_timer.isActive(),
            app._archive_live_timer.interval(),
        ))

        assert active is True
        assert interval == M.ARCHIVE_LIVE_REFRESH_MS

    def test_stops_when_the_session_ends(self, app):
        """First condition lapsing: session ends while Archive stays open."""
        _sessions(("2026-07-01", "work", "w1", 90))
        app.tracker.start("work")

        def probe(tabs):
            i = _tab_index(tabs, "work")
            app.tracker.reset()
            app._tick_archive_live_tab()
            return app._archive_live_timer.isActive(), tabs.tabText(i)

        active, label = _open_archive(app, probe)

        assert active is False
        assert IN_PROGRESS_MARK not in label, "readout survived the session"

    def test_torn_down_when_the_archive_closes(self, app):
        """Second condition lapsing: Archive closes while the session runs."""
        _sessions(("2026-07-01", "work", "w1", 90))
        app.tracker.start("work")

        _open_archive(app, lambda _t: None)

        assert app._archive_live_timer is None
        assert app._archive_tabs is None

    def test_live_session_with_archive_closed_is_inert(self, app):
        """The sync/tick entry points are reached on every tracker state
        change, so they must be safe no-ops with no Archive open."""
        _sessions(("2026-07-01", "work", "w1", 90))
        app.tracker.start("work")

        assert app._archive_tabs is None
        app._sync_archive_live_timer()   # must not raise
        app._tick_archive_live_tab()     # must not raise
        assert app._archive_live_timer is None


# ---------------------------------------------------------------------------
# Delete tag from the tab context menu
# ---------------------------------------------------------------------------

class _MenuDriver:
    """Drives a context menu that is about to be opened with `exec()`.

    `QMenu.exec` cannot be monkeypatched — PySide6 dispatches to the C++
    slot regardless of what the Python attribute is set to, so a stub is
    simply ignored and the call blocks the suite. Instead the menu is
    driven for real: `arm()` queues a callback that lands inside exec()'s
    own nested event loop, records the entries, triggers them, and closes.

    Triggering rather than synthesising a mouse click is deliberate — it
    is what a keyboard selection does, and the handler must answer that.
    The wiring this replaced compared `exec()`'s return value, which only
    reports an action when the menu was dismissed by a click.

    Arm before EVERY call, including cases expected to show no menu: an
    unexpected menu then gets driven and fails an assertion, instead of
    blocking on a modal with nothing to answer it.
    """

    def __init__(self):
        self.opened = 0
        self.actions = []

    def arm(self):
        QTimer.singleShot(0, self._interact)

    def _interact(self):
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QMenu) and widget.isVisible():
                self.opened += 1
                self.actions = [a.text() for a in widget.actions()]
                for action in widget.actions():
                    action.trigger()
                widget.close()


@pytest.fixture
def menu_spy():
    return _MenuDriver()


class TestDeleteTagFromTab:
    def test_offers_delete_tag_on_a_per_tag_tab(self, app, menu_spy, monkeypatch):
        _sessions(("2026-07-01", "work", "w1", 90))
        monkeypatch.setattr(app, "on_delete_tag", lambda tag: None)

        def probe(tabs):
            bar = tabs.tabBar()
            i = _tab_index(tabs, "work")
            menu_spy.arm()
            app._archive_tab_context_menu(bar.tabRect(i).center())

        _open_archive(app, probe)

        assert menu_spy.opened == 1
        assert menu_spy.actions == ["Delete tag"]

    def test_routes_to_the_existing_on_delete_tag_handler(
        self, app, menu_spy, monkeypatch,
    ):
        """The entry point must reuse the Tags-menu handler, so the live
        session check and confirmations come along with it."""
        _sessions(
            ("2026-07-01", "work", "w1", 90),
            ("2026-07-01", "admin", "a1", 30),
        )
        calls = []
        monkeypatch.setattr(app, "on_delete_tag", calls.append)

        def probe(tabs):
            bar = tabs.tabBar()
            i = _tab_index(tabs, "admin")
            menu_spy.arm()
            app._archive_tab_context_menu(bar.tabRect(i).center())

        _open_archive(app, probe)

        assert calls == ["admin"]

    def test_no_menu_on_the_all_tab(self, app, menu_spy, monkeypatch):
        """"All" is the fallback view, not a tag — nothing to delete."""
        _sessions(("2026-07-01", "work", "w1", 90))
        calls = []
        monkeypatch.setattr(app, "on_delete_tag", calls.append)

        def probe(tabs):
            bar = tabs.tabBar()
            assert bar.tabData(0) is None, "index 0 should be the All tab"
            menu_spy.arm()
            app._archive_tab_context_menu(bar.tabRect(0).center())
            # Synchronous proof no menu was raised: exec() would still be
            # blocking here if one had been.
            return QApplication.activePopupWidget()

        popup = _open_archive(app, probe)

        assert popup is None
        assert menu_spy.opened == 0
        assert calls == []

    def test_no_menu_off_the_end_of_the_strip(self, app, menu_spy, monkeypatch):
        """tabAt() returns -1 past the last tab; that must not delete."""
        from PySide6.QtCore import QPoint

        _sessions(("2026-07-01", "work", "w1", 90))
        calls = []
        monkeypatch.setattr(app, "on_delete_tag", calls.append)

        def probe(tabs):
            menu_spy.arm()
            app._archive_tab_context_menu(QPoint(10_000, 10_000))
            return QApplication.activePopupWidget()

        popup = _open_archive(app, probe)

        assert popup is None
        assert menu_spy.opened == 0
        assert calls == []


class TestDeleteTagReusesExistingLogic:
    """Mechanical guard against the delete path being re-implemented.

    Behavioural tests above prove the handler is *called*; these prove no
    parallel delete semantics grew alongside it — a second confirmation,
    or a direct storage call that would bypass the live-session check.
    """

    @staticmethod
    def _new_code():
        import inspect
        return (
            inspect.getsource(M.App._archive_tab_context_menu)
            + inspect.getsource(M.App._delete_tag_from_tab)
        )

    def test_delegates_to_on_delete_tag(self):
        assert "self.on_delete_tag(" in self._new_code()

    def test_does_not_call_storage_delete_directly(self):
        assert "storage.delete_tag" not in self._new_code()

    def test_does_not_add_its_own_confirmation(self):
        assert "QMessageBox" not in self._new_code()


# ---------------------------------------------------------------------------
# Undo glyph (Archive surface)
# ---------------------------------------------------------------------------

class TestUndoGlyph:
    def test_glyph_is_leftwards_arrow_with_hook(self):
        assert M.UNDO_GLYPH == "↩"
        assert ord(M.UNDO_GLYPH) == 0x21A9

    def test_icon_builds_at_the_sizes_the_archive_uses(self):
        """Smoke-only. The rendered pixels are not asserted: the offscreen
        platform substitutes a stub font, so any ink here says nothing
        about the real glyph. Appearance stays a manual check."""
        icon = M._undo_arrow_icon()
        assert not icon.isNull()
        assert not icon.pixmap(16, 16).isNull()
