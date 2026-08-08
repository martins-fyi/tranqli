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
and is deliberately not exercised here. It also keeps the plain retype-
the-value prompt: the Add row added in v0.2.4 (addendum §13d) belongs to
the active-session dialog only.

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


def _drive_dialog(monkeypatch, total=None, add=None, quick=None, ok=True):
    """Drive the REAL RetimeSessionDialog without an event loop.

    Only `exec` is replaced: the dialog itself is constructed, its fields
    are filled the way a user would fill them, and the accept goes through
    the dialog's own fold-in and validation. So these tests cover the
    widget wiring (quick-button connections, the inline error blocking the
    accept) as well as the handler.

    `total` overwrites the top row, `add` types into the Add field,
    `quick` clicks the +N button for that many minutes (15 / 30 / 60);
    leaving one as None leaves that field alone. `ok=False` cancels.

    Returns a dict carrying the prefill the dialog opened on and the
    dialog instance itself, for asserting on the error label.
    """
    seen = {}

    def fake_exec(dialog):
        seen["prefill"] = dialog.total_edit.text()
        seen["dialog"] = dialog
        if total is not None:
            dialog.total_edit.setText(total)
        if add is not None:
            dialog.add_edit.setText(add)
        if not ok:
            dialog.reject()
        elif quick is not None:
            dialog.quick_buttons[quick].click()
        else:
            dialog.buttons.accepted.emit()
        # QDialog.result() is whatever accept()/reject() last set, and 0
        # (Rejected) if the dialog refused to accept — exactly what a real
        # exec() would have returned.
        return dialog.result()

    monkeypatch.setattr(M.RetimeSessionDialog, "exec", fake_exec)
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
        seen = _drive_dialog(monkeypatch, ok=False)

        app.on_retime_session()

        # 30 banked + 60 live = 90.
        assert seen["prefill"] == M._format_dhm(90) == "01h 30m"

    def test_no_banked_row_still_shows_live_time(self, app, monkeypatch):
        """The original "opens on 00" symptom: nothing saved yet today."""
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions()
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 30))
        seen = _drive_dialog(monkeypatch, ok=False)

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
        seen = _drive_dialog(monkeypatch, ok=False)

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
        seen = _drive_dialog(monkeypatch, ok=False)

        app.on_retime_session()

        # Not 93 (yesterday's banked), not 48 (the whole live stretch).
        assert seen["prefill"] == "20m"


class TestConfirm:
    def test_replaces_rather_than_adds(self, app, monkeypatch):
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", 100))
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 30))
        seen = _drive_dialog(monkeypatch, total="45m")

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
        _drive_dialog(monkeypatch, total="02h 21m")

        app.on_retime_session()

        assert _minutes_on("2026-07-31") == 141
        assert _minutes_on("2026-07-30") == 93    # untouched

    def test_cancel_changes_nothing(self, app, monkeypatch):
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", 100))
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 30))
        _drive_dialog(monkeypatch, total="45m", ok=False)

        app.on_retime_session()

        assert _minutes_on("2026-07-31") == 100
        assert app.tracker.elapsed_seconds() == 1800   # tracker untouched

    def test_zero_drops_the_row(self, app, monkeypatch):
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", 100))
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 30))
        _drive_dialog(monkeypatch, total="0m")

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
        _drive_dialog(monkeypatch, total="45m")

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
        _drive_dialog(monkeypatch, total="45m")

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
        _drive_dialog(monkeypatch, total="45m")

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

        seen = _drive_dialog(monkeypatch, total="02h 21m")
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
        _drive_dialog(monkeypatch, total="02h 21m")

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
        called = _drive_dialog(monkeypatch, total="45m")

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
        _drive_dialog(monkeypatch, total="03h 07m")

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
        _drive_dialog(monkeypatch, total="03h 07m")

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
        _drive_dialog(monkeypatch, total="03h 07m")
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

    def test_add_then_confirm_clears_the_snapshot(self, app, monkeypatch):
        """v0.2.4: the Add row must not reintroduce the doubling. It is a
        different input to the same write, so the §13b clear still runs."""
        self._track_and_snapshot(app, monkeypatch)

        _drive_dialog(monkeypatch, add="30m")
        app.on_retime_session()

        assert storage.read_active_snapshot() is None
        assert _minutes_on("2026-07-31") == 217        # 187 + 30, once

    def test_quick_button_then_confirm_clears_the_snapshot(
        self, app, monkeypatch,
    ):
        """Same for the apply-and-close buttons, which accept the dialog
        themselves rather than going through OK."""
        self._track_and_snapshot(app, monkeypatch)

        _drive_dialog(monkeypatch, quick=60)
        app.on_retime_session()

        assert storage.read_active_snapshot() is None
        assert _minutes_on("2026-07-31") == 247        # 187 + 60, once

    def test_a_genuine_crash_after_an_add_is_still_recoverable(
        self, app, monkeypatch,
    ):
        """Clearing on the add path must not cost real crash safety
        either: time tracked after the add still gets offered back."""
        self._track_and_snapshot(app, monkeypatch)
        _drive_dialog(monkeypatch, add="30m")
        app.on_retime_session()
        assert _minutes_on("2026-07-31") == 217

        # Twenty more minutes, the tick persists them, then a crash.
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 12, 27))
        app._write_snapshot()
        monkeypatch.setattr(
            M.QMessageBox, "question",
            staticmethod(lambda *a, **k: M.QMessageBox.StandardButton.Save),
        )
        app._pending_crash_recovery = app._check_for_crash_recovery()
        assert app._pending_crash_recovery["minutes"] == 20
        app._prompt_crash_recovery_if_needed()

        assert _minutes_on("2026-07-31") == 237        # 217 + 20, once


class TestAddDeltaParser:
    """The dialog's grammar (addendum §13d) as a pure function — one
    grammar, shared by the top row and the Add field.

    Deliberately not routed through the dialog: this is the canonical
    input syntax and the one thing a user can get wrong by typing, so it
    is pinned directly, one case per accepted and rejected form.

    Hours and minutes only; the days cases below are rejections, and
    24 h is nowhere in here — the ceiling is an accept-time rule on the
    folded total, not part of the grammar.
    """

    @pytest.mark.parametrize("text, minutes", [
        # Empty is "no delta", not an error.
        ("", 0),
        ("   ", 0),
        # Bare integer = minutes.
        ("90", 90),
        ("0", 0),
        ("-20", -20),
        # Colon notation, H:MM.
        ("1:30", 90),
        ("0:45", 45),
        ("-1:30", -90),
        # Unit-suffixed, h/m in any combo. No d — see test_rejected.
        ("45m", 45),
        ("2h", 120),
        ("1h30", 90),       # trailing bare number after h is minutes
        ("1h30m", 90),
        ("1h 30m", 90),
        ("-1h30m", -90),
        # Hours are not capped by the grammar; 24 h is an accept-time
        # rule on the folded total, not something the parser knows.
        ("25h", 1500),
        ("36h 30m", 2190),
        ("1500", 1500),
        # A leading '+' is a no-op sign, symmetric with '-': each of
        # these is identical to its unprefixed form above.
        ("+30", 30),
        ("+1:30", 90),
        ("+45m", 45),
        ("+1h30m", 90),
        ("+ 20", 20),
        ("- 20", -20),
        # Case-insensitive and whitespace-tolerant.
        ("  2H 30M  ", 150),
        ("1 h 30 m", 90),
    ])
    def test_accepted(self, text, minutes):
        assert M._parse_add_delta(text) == minutes

    @pytest.mark.parametrize("text", [
        # Days: a Retime edits one (tag, date) row, so there is no unit
        # for them here. Every one of these parsed before v0.2.4's
        # third amendment.
        "1d",
        "1d2h",
        "1d 2h 30m",
        "1D 2H 30M",
        "-1d",
        "+1d 2h 30m",
        "1:75",        # minutes >= 60 in colon form is a typo signal
        "0:60",
        "1:30m",       # mixed colon and suffix
        "1:2:3",       # D:H:M went out with the days unit
        "1.5h",        # decimals
        "1,5h",
        "1x",          # unknown letters
        "abc",
        "h",           # a unit with no number
        "m",
        "-",           # a sign with no number
        "+",
        "--20",        # stray signs
        "++30",
        "+-20",
        "-+20",
        "1h-30",
        "1h+30",       # a '+' anywhere but the very front
        "30+",
        "20-",
        "30m 1h",      # out-of-order units
        "1h 2h",       # repeated units
        "30m20",       # bare trailing number only follows h
        "1 30",
        "٩٠",          # non-ASCII digits
    ])
    def test_rejected(self, text):
        with pytest.raises(M.AddDeltaError):
            M._parse_add_delta(text)


class TestFoldIn:
    """The deferred fold-in arithmetic: base + add + quick, in minutes."""

    def test_overflow_normalises(self):
        assert M._fold_in_minutes("50m", "30m") == 80
        assert M._format_hm(80) == "01h 20m"        # not "80m"

    def test_quick_amount_folds_in_alongside_the_add_field(self):
        assert M._fold_in_minutes("45m", "30m", 60) == 135

    def test_quick_amount_alone(self):
        assert M._fold_in_minutes("45m", "", 15) == 60

    def test_negative_add_subtracts(self):
        assert M._fold_in_minutes("02h 00m", "-20") == 100

    def test_clamps_to_zero(self):
        assert M._fold_in_minutes("10m", "-30") == 0
        assert M._fold_in_minutes("10m", "-99h") == 0

    def test_empty_add_leaves_the_base_alone(self):
        assert M._fold_in_minutes("01h 30m", "") == 90

    def test_unparseable_add_raises_rather_than_reading_as_zero(self):
        with pytest.raises(M.AddDeltaError):
            M._fold_in_minutes("01h 30m", "banana")

    def test_the_top_row_is_held_to_the_same_grammar(self):
        """Both fields go through _parse_add_delta, so a days unit in
        the top row is refused rather than silently misread."""
        with pytest.raises(M.AddDeltaError):
            M._fold_in_minutes("1d", "")
        with pytest.raises(M.AddDeltaError):
            M._fold_in_minutes("1:2:3", "")

    def test_does_not_cap_at_the_ceiling(self):
        """The ceiling is the dialog's, not the arithmetic's — folding
        stays honest so the dialog can refuse rather than truncate."""
        assert M._fold_in_minutes("24h", "30m") == 1470


class TestFormatHm:
    """The days-free display format for the active Retime dialog."""

    @pytest.mark.parametrize("minutes, text", [
        (0, "00m"),
        (45, "45m"),
        (90, "01h 30m"),
        (130, "02h 10m"),
        (1439, "23h 59m"),
        (1440, "24h 00m"),        # no rollover to "01d 00h 00m"
        (1500, "25h 00m"),
        (3000, "50h 00m"),
    ])
    def test_formats_without_a_days_field(self, minutes, text):
        assert M._format_hm(minutes) == text

    def test_round_trips_through_the_parser(self):
        for minutes in (0, 45, 90, 1439, 1440, 1500):
            assert M._parse_add_delta(M._format_hm(minutes)) == minutes


class TestAddRowEndToEnd:
    """The Add row through the real dialog and the real write path."""

    NOW = datetime.datetime(2026, 7, 31, 10, 0)
    STARTED = datetime.datetime(2026, 7, 31, 9, 30)     # 30 min live

    def _live_session(self, app, monkeypatch, banked=100):
        """100 banked + 30 live = a dialog prefilled with 02h 10m."""
        _freeze(monkeypatch, self.NOW)
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", banked))
        _start_running(app, self.STARTED)

    def test_add_folds_into_the_prefilled_total(self, app, monkeypatch):
        self._live_session(app, monkeypatch)
        seen = _drive_dialog(monkeypatch, add="30m")

        app.on_retime_session()

        assert seen["prefill"] == "02h 10m"
        assert _minutes_on("2026-07-31") == 160         # 130 + 30

    def test_add_does_not_mutate_the_top_row_before_accept(
        self, app, monkeypatch,
    ):
        """The fold-in is deferred: the top row still reads the stored
        total right up to the moment the dialog is accepted."""
        self._live_session(app, monkeypatch)
        seen = _drive_dialog(monkeypatch, add="30m")

        app.on_retime_session()

        assert seen["dialog"].total_edit.text() == "02h 10m"

    def test_retyped_total_and_add_combine(self, app, monkeypatch):
        """OK = whatever is in the Xd Xh Xm fields, plus the Add field."""
        self._live_session(app, monkeypatch)
        _drive_dialog(monkeypatch, total="50m", add="30m")

        app.on_retime_session()

        assert _minutes_on("2026-07-31") == 80
        assert M._format_dhm(_minutes_on("2026-07-31")) == "01h 20m"

    def test_negative_add_subtracts(self, app, monkeypatch):
        self._live_session(app, monkeypatch)
        _drive_dialog(monkeypatch, add="-20")

        app.on_retime_session()

        assert _minutes_on("2026-07-31") == 110         # 130 - 20

    def test_subtracting_past_zero_clamps_and_drops_the_row(
        self, app, monkeypatch,
    ):
        self._live_session(app, monkeypatch)
        _drive_dialog(monkeypatch, add="-10h")

        app.on_retime_session()

        assert _minutes_on("2026-07-31") == 0
        assert not [
            s for s in storage.load_sessions() if s.date == "2026-07-31"
        ]

    @pytest.mark.parametrize("minutes, expected", [
        (15, 145), (30, 160), (60, 190),
    ])
    def test_quick_buttons_apply_and_close(
        self, app, monkeypatch, minutes, expected,
    ):
        self._live_session(app, monkeypatch)
        seen = _drive_dialog(monkeypatch, quick=minutes)

        app.on_retime_session()

        assert seen["dialog"].result() == M.QDialog.DialogCode.Accepted
        assert _minutes_on("2026-07-31") == expected

    def test_quick_button_matches_typing_the_same_amount(
        self, app, monkeypatch,
    ):
        """The stated equivalence: +30m clicked == "30m" typed + OK."""
        def run(**kwargs):
            app.tracker.reset()
            self._live_session(app, monkeypatch)
            _drive_dialog(monkeypatch, **kwargs)
            app.on_retime_session()
            return _minutes_on("2026-07-31")

        assert run(add="30m") == run(quick=30) == 160

    def test_quick_button_folds_in_the_add_field_too(self, app, monkeypatch):
        """A button click is base + Add + the button's amount, once."""
        self._live_session(app, monkeypatch)
        _drive_dialog(monkeypatch, add="15m", quick=30)

        app.on_retime_session()

        assert _minutes_on("2026-07-31") == 175         # 130 + 15 + 30

    def test_empty_add_leaves_the_old_retype_behaviour_intact(
        self, app, monkeypatch,
    ):
        self._live_session(app, monkeypatch)
        _drive_dialog(monkeypatch, total="45m")

        app.on_retime_session()

        assert _minutes_on("2026-07-31") == 45


class TestNoDaysInThisDialog:
    """A Retime edits one (tag, date) row, so `d` is gone from both
    fields (v0.2.4 third amendment). The Archive dialog keeps it."""

    def _attempt(self, app, monkeypatch, banked=100, **kwargs):
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", banked))
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 30))
        seen = _drive_dialog(monkeypatch, **kwargs)
        app.on_retime_session()
        return seen

    def test_the_prefill_never_renders_a_days_field(self, app, monkeypatch):
        """1500 banked + 30 live = 1530 min — "25h 30m", not "01d ...".
        The prefill is not blocked by the 24h ceiling either."""
        seen = self._attempt(app, monkeypatch, banked=1500, ok=False)

        assert seen["prefill"] == "25h 30m"
        assert "d" not in seen["prefill"]

    def test_the_top_row_refuses_a_days_value(self, app, monkeypatch):
        seen = self._attempt(app, monkeypatch, total="1d")

        assert seen["dialog"].result() != M.QDialog.DialogCode.Accepted
        assert seen["dialog"].error_label.isVisibleTo(seen["dialog"])
        assert _minutes_on("2026-07-31") == 100         # untouched

    def test_the_add_field_refuses_a_days_value(self, app, monkeypatch):
        seen = self._attempt(app, monkeypatch, add="1d")

        assert seen["dialog"].result() != M.QDialog.DialogCode.Accepted
        assert _minutes_on("2026-07-31") == 100

    def test_hours_past_24_are_still_typeable_in_the_top_row(
        self, app, monkeypatch,
    ):
        """Refusing days must not mean refusing large hour counts —
        those are how an over-a-day row gets expressed now."""
        self._attempt(app, monkeypatch, total="23h", add="1h")

        assert _minutes_on("2026-07-31") == 1440


class TestTwentyFourHourCeiling:
    """Over 24 h in one day is a typo, so the accept is refused — not
    clamped. Checked on the folded total, on both OK and the buttons."""

    def _attempt(self, app, monkeypatch, banked=100, **kwargs):
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", banked))
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 30))
        seen = _drive_dialog(monkeypatch, **kwargs)
        app.on_retime_session()
        return seen

    def test_one_minute_under_the_limit_accepts(self, app, monkeypatch):
        self._attempt(app, monkeypatch, total="23h 59m")

        assert _minutes_on("2026-07-31") == 1439

    def test_exactly_24h_accepts(self, app, monkeypatch):
        """The boundary is inclusive: 1440 is a legal day."""
        self._attempt(app, monkeypatch, total="24h")

        assert _minutes_on("2026-07-31") == 1440

    def test_one_minute_over_the_limit_is_refused(self, app, monkeypatch):
        seen = self._attempt(app, monkeypatch, total="24h 01m")

        assert seen["dialog"].result() != M.QDialog.DialogCode.Accepted
        assert seen["dialog"].error_label.text() == "24h limit exceeded"
        assert _minutes_on("2026-07-31") == 100         # not written
        assert app.tracker.elapsed_seconds() == 1800    # not rebased

    def test_it_refuses_rather_than_truncating_to_1440(
        self, app, monkeypatch,
    ):
        """The distinction from clamp-at-0: nothing is silently
        rewritten to fit."""
        self._attempt(app, monkeypatch, total="30h")

        assert _minutes_on("2026-07-31") == 100
        assert not [
            s for s in storage.load_sessions() if s.minutes == 1440
        ]

    def test_the_ceiling_applies_to_the_folded_total_not_the_add_alone(
        self, app, monkeypatch,
    ):
        """`+8h` is fine by itself and a mistake on top of 20h."""
        seen = self._attempt(app, monkeypatch, total="20h", add="+8h")

        assert seen["dialog"].error_label.text() == "24h limit exceeded"
        assert _minutes_on("2026-07-31") == 100

    def test_a_modest_add_on_a_modest_total_still_goes_through(
        self, app, monkeypatch,
    ):
        self._attempt(app, monkeypatch, add="+8h")

        assert _minutes_on("2026-07-31") == 610         # 130 + 480

    def test_a_quick_button_can_trip_the_ceiling(self, app, monkeypatch):
        """23h50m + the +15m button = 24h05m — refused, dialog stays
        open, same inline label as an unparseable entry."""
        seen = self._attempt(app, monkeypatch, total="23h 50m", quick=15)

        assert seen["dialog"].result() != M.QDialog.DialogCode.Accepted
        assert seen["dialog"].error_label.isVisibleTo(seen["dialog"])
        assert seen["dialog"].error_label.text() == "24h limit exceeded"
        assert _minutes_on("2026-07-31") == 100

    def test_the_error_clears_once_the_total_is_brought_back_under(
        self, app, monkeypatch,
    ):
        seen = self._attempt(app, monkeypatch, total="25h")
        dialog = seen["dialog"]
        assert dialog.error_label.isVisibleTo(dialog)

        dialog.total_edit.setText("23h")
        dialog.buttons.accepted.emit()

        assert not dialog.error_label.isVisibleTo(dialog)
        assert dialog.result() == M.QDialog.DialogCode.Accepted
        assert dialog.result_minutes() == 1380

    def test_a_negative_result_still_clamps_to_zero(self, app, monkeypatch):
        """The other end is unchanged: clamped, not refused."""
        self._attempt(app, monkeypatch, add="-10h")

        assert _minutes_on("2026-07-31") == 0
        assert not [
            s for s in storage.load_sessions() if s.date == "2026-07-31"
        ]


class TestOverLongExistingRow:
    """A row over 24 h can already exist — the web editor and the
    Archive dialog can both write one. The active dialog has to open on
    it, or the tool for fixing a bad number is the one tool that won't
    load it."""

    def _open_on(self, app, monkeypatch, banked, **kwargs):
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", banked))
        # Paused, so the prefill is exactly the banked number.
        app.tracker.set_tag(TAG)
        app.tracker._intervals = []
        app.tracker._start = None
        app._session_started = True
        seen = _drive_dialog(monkeypatch, **kwargs)
        app.on_retime_session()
        return seen

    def test_it_opens_and_renders_the_over_long_value(self, app, monkeypatch):
        seen = self._open_on(app, monkeypatch, 2000, ok=False)

        assert seen["prefill"] == "33h 20m"
        assert _minutes_on("2026-07-31") == 2000        # cancel: intact

    def test_accepting_it_unchanged_is_refused(self, app, monkeypatch):
        seen = self._open_on(app, monkeypatch, 2000)

        assert seen["dialog"].error_label.text() == "24h limit exceeded"
        assert _minutes_on("2026-07-31") == 2000        # still there to fix

    def test_reducing_it_under_the_ceiling_accepts(self, app, monkeypatch):
        self._open_on(app, monkeypatch, 2000, total="03h 20m")

        assert _minutes_on("2026-07-31") == 200

    def test_subtracting_it_under_the_ceiling_accepts(self, app, monkeypatch):
        self._open_on(app, monkeypatch, 2000, add="-10h")

        assert _minutes_on("2026-07-31") == 1400


class TestEnterKeyInTheAddField:
    """Enter in the Add field fires the dialog's default button (OK).

    Built directly rather than through on_retime_session: this is about
    the widget's key handling, so the key event is the whole point. The
    quick buttons are explicitly not auto-default, which is what keeps
    Enter meaning OK rather than +15m.
    """

    def _dialog(self, add_text):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        dialog = M.RetimeSessionDialog(TAG, "02h 10m")
        dialog.add_edit.setText(add_text)
        QTest.keyClick(dialog.add_edit, Qt.Key_Return)
        return dialog

    def test_enter_accepts_with_the_add_folded_in(self, app):
        dialog = self._dialog("30m")

        assert dialog.result() == M.QDialog.DialogCode.Accepted
        assert dialog.result_minutes() == 160          # 130 + 30

    def test_enter_on_a_bad_value_shows_the_error_instead(self, app):
        dialog = self._dialog("banana")

        assert dialog.result() != M.QDialog.DialogCode.Accepted
        assert dialog.error_label.isVisibleTo(dialog)


class TestUnparseableAddIsBlocked:
    """A bad Add field keeps the dialog open with an inline error — it
    does not accept, does not write, and does not pop a modal."""

    def _attempt(self, app, monkeypatch, **kwargs):
        _freeze(monkeypatch, datetime.datetime(2026, 7, 31, 10, 0))
        _sessions(("2026-07-31", TAG, f"{TAG}-2026-07-31", 100))
        _start_running(app, datetime.datetime(2026, 7, 31, 9, 30))
        seen = _drive_dialog(monkeypatch, **kwargs)
        app.on_retime_session()
        return seen

    def test_ok_does_not_accept(self, app, monkeypatch):
        seen = self._attempt(app, monkeypatch, add="banana")

        assert seen["dialog"].result() != M.QDialog.DialogCode.Accepted
        assert seen["dialog"].result_minutes() is None

    def test_nothing_is_written(self, app, monkeypatch):
        self._attempt(app, monkeypatch, add="1.5h")

        assert _minutes_on("2026-07-31") == 100         # untouched
        assert app.tracker.elapsed_seconds() == 1800    # not rebased

    def test_an_inline_error_is_shown_under_the_field(self, app, monkeypatch):
        seen = self._attempt(app, monkeypatch, add="1:75")

        assert seen["dialog"].error_label.isVisibleTo(seen["dialog"])
        assert seen["dialog"].error_label.text()

    def test_a_quick_button_is_blocked_the_same_way(self, app, monkeypatch):
        seen = self._attempt(app, monkeypatch, add="1:30m", quick=15)

        assert seen["dialog"].result() != M.QDialog.DialogCode.Accepted
        assert seen["dialog"].error_label.isVisibleTo(seen["dialog"])
        assert _minutes_on("2026-07-31") == 100

    def test_the_error_clears_once_the_field_is_fixed(self, app, monkeypatch):
        seen = self._attempt(app, monkeypatch, add="banana")
        dialog = seen["dialog"]
        assert dialog.error_label.isVisibleTo(dialog)

        dialog.add_edit.setText("30m")
        dialog.buttons.accepted.emit()

        assert not dialog.error_label.isVisibleTo(dialog)
        assert dialog.result_minutes() == 160           # 130 + 30
