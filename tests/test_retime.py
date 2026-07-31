"""Tests for Retime session on the ACTIVE / in-progress session.

`App.on_retime_session` had no test coverage at all, which is how it kept
two bugs through the whole tag-management overhaul:

- the dialog pre-filled from banked CSV minutes only, so it opened on
  "00m" whenever nothing had been saved for today yet, even though the
  widget was showing real tracked time;
- confirming replaced the banked half but left the tracker's unbanked
  elapsed alone, so the display jumped to entered + elapsed and the next
  save merged that elapsed back on top — a replace that read as an add.

The archive's row-level Retime (`_archive_retime`) is a separate, already
correct path — it edits a static past row with no live tracker involved —
and is deliberately not exercised here.

Runs headless via the offscreen Qt platform, sharing conftest's
session-scoped `app` (QApplication is per-process).
"""

import datetime
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

# Must be set before the QApplication is constructed.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from green_tracker import main as M, storage  # noqa: E402
from green_tracker import tracker as T  # noqa: E402


TAG = "ste vibe coding"


@pytest.fixture(autouse=True)
def quiet_app(app):
    """Leave the tracker idle and no modal pending, around every test.

    The App is shared across the session, so a live session left behind by
    one test would leak into the next one's prefill.
    """
    app._pending_crash_recovery = None
    app._pending_session_name = None
    app._carry_seconds = 0
    app.tracker.reset()
    yield
    app._pending_crash_recovery = None
    app._pending_session_name = None
    app._carry_seconds = 0
    app.tracker.reset()


def _freeze(monkeypatch, when):
    """Pin `datetime.now()` to `when` in both main and tracker.

    Both modules do `from datetime import datetime`, so each holds its own
    reference and both need patching: main decides which day "today" is,
    tracker decides where a running stretch ends when it is paused. A
    subclass keeps `combine`, `fromtimestamp` and arithmetic intact.
    """

    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(M, "datetime", Frozen)
    monkeypatch.setattr(T, "datetime", Frozen)
    return when


def _stub_dialog(monkeypatch, reply="", ok=True):
    """Replace QInputDialog with a stub, capturing what it was pre-filled
    with and returning a canned answer."""
    seen = {}

    def get_text(parent, title, label, text="", **kwargs):
        seen["prefill"] = text
        seen["label"] = label
        return reply, ok

    monkeypatch.setattr(
        M, "QInputDialog", SimpleNamespace(getText=get_text),
    )
    return seen


def _sessions(*rows):
    storage.save_sessions([
        storage.SessionRow(date=d, tag=t, session_name=n, minutes=m)
        for d, t, n, m in rows
    ])


def _minutes_on(date_str, tag=TAG):
    return storage.today_minutes_for_tag(tag, date_str)


def _start_running(app, at):
    """Put the app in a live RUNNING session for TAG, started at `at`."""
    app.tracker.set_tag(TAG)
    app.tracker._intervals = []
    app.tracker._start = at
    app._session_started = True


class TestPrefill:
    def test_includes_live_unbanked_time_same_day(self, app, monkeypatch):
        """Prefill is banked + live, not banked alone."""
        now = _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", 30))
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 0))
        seen = _stub_dialog(monkeypatch, ok=False)

        app.on_retime_session()

        # 30 banked + 60 live = 90.
        assert seen["prefill"] == M._format_dhm(90) == "01h 30m"

    def test_no_banked_row_still_shows_live_time(self, app, monkeypatch):
        """The original "opens on 00" symptom: nothing saved yet today."""
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions()
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 30))
        seen = _stub_dialog(monkeypatch, ok=False)

        app.on_retime_session()

        assert seen["prefill"] == "30m"

    def test_paused_session_prefills_banked_plus_accumulated(
        self, app, monkeypatch,
    ):
        """Unbanked time counts whether or not the clock is running."""
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 12, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", 10))
        app.tracker.set_tag(TAG)
        app.tracker._intervals = [(
            datetime.datetime(2026, 7, 31, 9, 0),
            datetime.datetime(2026, 7, 31, 9, 45),
        )]
        app.tracker._start = None
        app._session_started = True
        seen = _stub_dialog(monkeypatch, ok=False)

        app.on_retime_session()

        assert seen["prefill"] == M._format_dhm(55) == "55m"

    def test_splits_at_midnight_offering_only_todays_portion(
        self, app, monkeypatch,
    ):
        """A session that ran across midnight must not offer yesterday's
        time for editing — Retime is scoped to today's row."""
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 0, 20))
        _sessions(("2026-07-30", TAG, f"{TAG}-2026-07-30", 93))
        # Started yesterday at 23:32:17, still running: 1663 s belong to
        # 07-30 and exactly 1200 s (20 min) to 07-31.
        _start_running(app, datetime.datetime(2026, 7, 30, 23, 32, 17))
        seen = _stub_dialog(monkeypatch, ok=False)

        app.on_retime_session()

        # Not 93 (yesterday's banked), not 48 (the whole live stretch).
        assert seen["prefill"] == "20m"


class TestConfirm:
    def test_replaces_rather_than_adds(self, app, monkeypatch):
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", 100))
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 30))
        seen = _stub_dialog(monkeypatch, reply="45m")

        app.on_retime_session()

        assert seen["prefill"] == "02h 10m"       # 100 banked + 30 live
        assert _minutes_on("2026-07-31") == 45    # not 145, not 175

    def test_writes_to_todays_date_not_the_sessions_start_date(
        self, app, monkeypatch,
    ):
        """The row key is today, even when the tracker started yesterday."""
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 0, 20))
        _sessions(("2026-07-30", TAG, f"{TAG}-2026-07-30", 93))
        _start_running(app, datetime.datetime(2026, 7, 30, 23, 32, 17))
        _stub_dialog(monkeypatch, reply="02h 21m")

        app.on_retime_session()

        assert _minutes_on("2026-07-31") == 141
        assert _minutes_on("2026-07-30") == 93    # untouched

    def test_cancel_changes_nothing(self, app, monkeypatch):
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", 100))
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 30))
        _stub_dialog(monkeypatch, reply="45m", ok=False)

        app.on_retime_session()

        assert _minutes_on("2026-07-31") == 100
        assert app.tracker.elapsed_seconds() == 1800   # tracker untouched

    def test_zero_drops_the_row(self, app, monkeypatch):
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", 100))
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 30))
        _stub_dialog(monkeypatch, reply="0m")

        app.on_retime_session()

        assert _minutes_on("2026-07-31") == 0
        assert not [
            s for s in storage.load_sessions() if s.date == "2026-07-31"
        ]


class TestNotReAddedBySave:
    def test_subsequent_save_does_not_add_the_old_elapsed_back(
        self, app, monkeypatch,
    ):
        """The core regression: retime, then save, must leave the entered
        value standing."""
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", 100))
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 30))
        _stub_dialog(monkeypatch, reply="45m")

        app.on_retime_session()
        assert _minutes_on("2026-07-31") == 45

        app.on_save_session()

        assert _minutes_on("2026-07-31") == 45    # not 75

    def test_time_tracked_after_the_retime_still_accrues(
        self, app, monkeypatch,
    ):
        """Rebasing must not stop the clock — only rewind it to now."""
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", 100))
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 30))
        _stub_dialog(monkeypatch, reply="45m")

        app.on_retime_session()

        assert app.tracker.state is T.State.RUNNING
        # Nothing accrued at the instant of the retime...
        assert app.tracker.elapsed_seconds(
            now=datetime.datetime(2026, 7, 31, 10, 0),
        ) == 0
        # ...but the clock still runs forward from it.
        assert app.tracker.elapsed_seconds(
            now=datetime.datetime(2026, 7, 31, 10, 20),
        ) == 1200

        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 20))
        app.on_save_session()

        assert _minutes_on("2026-07-31") == 65    # 45 entered + 20 tracked

    def test_carry_reflects_the_entered_value(self, app, monkeypatch):
        """The widget reads carry + elapsed; after a retime that is
        exactly the entered value."""
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", 100))
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 30))
        _stub_dialog(monkeypatch, reply="45m")

        app.on_retime_session()

        displayed = app._carry_seconds + app.tracker.elapsed_seconds(
            now=datetime.datetime(2026, 7, 31, 10, 0),
        )
        assert displayed == 45 * 60


class TestMidnightCrossingRegression:
    """Pinned to the exact numbers reproduced during diagnosis: a CSV
    holding 2026-07-30 = 93 min, and a live session carrying 2863 s of
    unbanked time that had bled across midnight into 2026-07-31.

    Before the fix this opened on "00m" (no 07-31 row banked yet) and
    writing to it left the 07-30 row in place beside the new one, so the
    tag's total read as 93 + entered — a replace that presented as an add.
    """

    NOW = datetime.datetime(2026, 7, 31, 0, 20)
    STARTED = datetime.datetime(2026, 7, 30, 23, 32, 17)   # NOW - 2863 s

    def test_the_reproduction_end_to_end(self, app, monkeypatch):
        _freeze(monkeypatch, self.NOW)
        _sessions(("2026-07-30", TAG, f"{TAG}-2026-07-30", 93))
        _start_running(app, self.STARTED)
        assert app.tracker.elapsed_seconds(now=self.NOW) == 2863

        seen = _stub_dialog(monkeypatch, reply="02h 21m")
        app.on_retime_session()

        # Prefill offered today's 20 min, not 00m and not the full 48 min.
        assert seen["prefill"] == "20m"
        # The entered total landed on today's row, replacing nothing else.
        assert _minutes_on("2026-07-31") == 141
        assert _minutes_on("2026-07-30") == 93

        app.on_save_session()

        # Today's row stands exactly as entered — the 20 min that was
        # folded into it is not added a second time.
        assert _minutes_on("2026-07-31") == 141
        # Yesterday's 1663 unbanked seconds (28 min) were NOT destroyed by
        # the rebase; they bank against yesterday's row, where they belong.
        assert _minutes_on("2026-07-30") == 93 + 28

    def test_tag_total_is_no_longer_the_sum_of_two_rows(
        self, app, monkeypatch,
    ):
        """The user-visible symptom: the tag total used to grow by the
        entered amount instead of settling on it."""
        _freeze(monkeypatch, self.NOW)
        _sessions(("2026-07-30", TAG, f"{TAG}-2026-07-30", 93))
        _start_running(app, self.STARTED)
        _stub_dialog(monkeypatch, reply="02h 21m")

        app.on_retime_session()

        rows = {s.date: s.minutes for s in storage.load_sessions()}
        assert rows == {"2026-07-30": 93, "2026-07-31": 141}


class TestNoActiveTag:
    def test_is_a_no_op_without_a_tag(self, app, monkeypatch):
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions()
        app.tracker.reset()
        shown = {}
        monkeypatch.setattr(
            M, "QMessageBox",
            SimpleNamespace(
                information=lambda *a, **k: shown.setdefault("hit", True),
            ),
        )
        called = _stub_dialog(monkeypatch, reply="45m")

        app.on_retime_session()

        assert shown.get("hit") is True
        assert "prefill" not in called
        assert storage.load_sessions() == []


class TestSameDayRetimeDoesNotDouble:
    """The 2x report was first blamed on `rebase_day` failing to zero a
    session that started AND was retimed on the same day — i.e. an edge
    case where there is no "before today" portion to keep. These pin that
    down: same-day rebasing zeroes cleanly, and no surface doubles.
    """

    def test_same_day_retime_zeroes_the_tracker(self, app, monkeypatch):
        now = _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 12, 7))
        _sessions()
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 0))
        _stub_dialog(monkeypatch, reply="03h 07m")

        app.on_retime_session()

        # Nothing survives the rebase: there is no earlier day to keep.
        assert app.tracker.get_daily_seconds(now=now) == {
            datetime.date(2026, 7, 31): 0,
        }
        assert app.tracker.elapsed_seconds(now=now) == 0
        assert _minutes_on("2026-07-31") == 187

    def test_widget_and_stored_total_agree_after_same_day_retime(
        self, app, monkeypatch,
    ):
        """Widget reads carry + elapsed; storage reads the row. Both must
        land on 187 — not 187 and 374."""
        now = _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 12, 7))
        _sessions()
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 0))
        _stub_dialog(monkeypatch, reply="03h 07m")

        app.on_retime_session()

        widget_minutes = (
            app._carry_seconds + app.tracker.elapsed_seconds(now=now)
        ) / 60
        assert widget_minutes == 187
        assert _minutes_on("2026-07-31") == 187


class TestStaleCrashSnapshotAfterRetime:
    """Regression: Retime used to leave the crash-safety snapshot behind.

    `active_session.json` held the PRE-retime elapsed seconds, which are
    already inside the number Retime banks. If the app went down before
    that file was refreshed, the next launch offered the same time back
    as "unsaved work" and `_prompt_crash_recovery_if_needed` committed it
    with `commit_session` — which ADDS. Storage landed on exactly twice
    the retimed value: a real 374-minute row where 187 was correct
    (widget read 3h7m from a stale carry, Archive read 6h14m from disk).

    Two independent guards now close it, one per test below: Retime
    clears the snapshot explicitly, and `_write_snapshot` clears rather
    than skips when elapsed is zero — so a future caller that rebases
    the tracker without remembering to clear is still safe.
    """

    def _track_and_snapshot(self, app, monkeypatch):
        """Track 187 min and let the 3-min tick persist it."""
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 12, 7))
        _sessions()
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 0))
        app._write_snapshot()                      # the 3-min tick
        assert storage.read_active_snapshot()["elapsed_seconds"] == 11220

    def _retime(self, app, monkeypatch):
        _stub_dialog(monkeypatch, reply="03h 07m")
        app.on_retime_session()

    def test_retime_clears_the_snapshot(self, app, monkeypatch):
        """Guard 1: the explicit clear in on_retime_session."""
        self._track_and_snapshot(app, monkeypatch)

        self._retime(app, monkeypatch)

        assert storage.read_active_snapshot() is None
        assert _minutes_on("2026-07-31") == 187        # banked exactly once

    def test_write_snapshot_clears_rather_than_skips_at_zero_elapsed(
        self, app, monkeypatch,
    ):
        """Guard 2: defence in depth for callers that zero the tracker
        without clearing. Rebase directly, bypassing Retime's own clear."""
        self._track_and_snapshot(app, monkeypatch)

        app.tracker.rebase_day(
            datetime.date(2026, 7, 31),
            now=datetime.datetime(2026, 7, 31, 12, 7),
        )
        assert app.tracker.elapsed_seconds() == 0
        app._write_snapshot()

        assert storage.read_active_snapshot() is None

    def test_no_recovery_is_offered_after_a_retime(self, app, monkeypatch):
        """The reported 3h7m / 6h14m, end to end — now a no-op."""
        self._track_and_snapshot(app, monkeypatch)
        self._retime(app, monkeypatch)

        # Relaunch: read the snapshot, then answer Save on any prompt.
        monkeypatch.setattr(
            M.QMessageBox, "question",
            staticmethod(lambda *a, **k: M.QMessageBox.StandardButton.Save),
        )
        app._pending_crash_recovery = app._check_for_crash_recovery()
        assert app._pending_crash_recovery is None     # nothing to re-add
        app._prompt_crash_recovery_if_needed()

        assert _minutes_on("2026-07-31") == 187        # was 374

    def test_a_genuine_crash_is_still_recoverable(self, app, monkeypatch):
        """The clear must not cost real crash safety: time tracked AFTER
        a retime still gets offered back."""
        self._track_and_snapshot(app, monkeypatch)
        self._retime(app, monkeypatch)

        # Twenty more minutes, then the tick persists them, then a crash.
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 12, 27))
        app._write_snapshot()
        monkeypatch.setattr(
            M.QMessageBox, "question",
            staticmethod(lambda *a, **k: M.QMessageBox.StandardButton.Save),
        )
        app._pending_crash_recovery = app._check_for_crash_recovery()
        assert app._pending_crash_recovery["minutes"] == 20
        app._prompt_crash_recovery_if_needed()

        assert _minutes_on("2026-07-31") == 207        # 187 + 20, once
