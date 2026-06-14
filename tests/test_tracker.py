from datetime import date, datetime, timedelta

import pytest

from green_tracker.tracker import State, Tracker, split_at_midnight


# ---------------------------------------------------------------------------
# split_at_midnight
# ---------------------------------------------------------------------------

class TestSplitAtMidnight:
    def test_same_day(self):
        start = datetime(2026, 5, 28, 9, 0)
        end = datetime(2026, 5, 28, 17, 0)
        result = split_at_midnight(start, end)
        assert result == [(date(2026, 5, 28), 8 * 3600)]

    def test_crosses_one_midnight(self):
        start = datetime(2026, 5, 28, 23, 0)
        end = datetime(2026, 5, 29, 2, 0)
        result = split_at_midnight(start, end)
        assert result == [
            (date(2026, 5, 28), 1 * 3600),
            (date(2026, 5, 29), 2 * 3600),
        ]

    def test_crosses_two_midnights(self):
        start = datetime(2026, 5, 28, 22, 0)
        end = datetime(2026, 5, 30, 3, 0)
        result = split_at_midnight(start, end)
        assert len(result) == 3
        assert result[0] == (date(2026, 5, 28), 2 * 3600)
        assert result[1] == (date(2026, 5, 29), 24 * 3600)
        assert result[2] == (date(2026, 5, 30), 3 * 3600)

    def test_starts_at_midnight(self):
        start = datetime(2026, 5, 29, 0, 0, 0)
        end = datetime(2026, 5, 29, 1, 0, 0)
        result = split_at_midnight(start, end)
        assert result == [(date(2026, 5, 29), 3600)]

    def test_ends_at_midnight(self):
        # Ending exactly at midnight emits a 0.0-second boundary entry for the
        # next day — harmless in storage (0 min rounds to nothing).
        start = datetime(2026, 5, 28, 23, 0)
        end = datetime(2026, 5, 29, 0, 0)
        result = split_at_midnight(start, end)
        assert result[0] == (date(2026, 5, 28), 3600.0)
        assert result[1] == (date(2026, 5, 29), 0.0)

    def test_zero_duration(self):
        t = datetime(2026, 5, 28, 12, 0)
        result = split_at_midnight(t, t)
        assert result == [(date(2026, 5, 28), 0.0)]

    def test_end_before_start_returns_zero(self):
        start = datetime(2026, 5, 28, 12, 0)
        end = datetime(2026, 5, 28, 11, 0)
        result = split_at_midnight(start, end)
        assert result == [(date(2026, 5, 28), 0.0)]

    def test_seconds_sum_equals_total_duration(self):
        start = datetime(2026, 5, 27, 20, 0)
        end = datetime(2026, 5, 30, 4, 0)
        result = split_at_midnight(start, end)
        total = sum(s for _, s in result)
        assert total == pytest.approx((end - start).total_seconds())


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

class TestStateTransitions:
    def test_initial_state_is_paused(self):
        t = Tracker()
        assert t.state == State.PAUSED
        assert t.tag is None

    def test_start_sets_running(self):
        t = Tracker()
        t.start("email", now=datetime(2026, 5, 28, 9, 0))
        assert t.state == State.RUNNING
        assert t.tag == "email"

    def test_pause_stops_clock(self):
        t = Tracker()
        t.start("email", now=datetime(2026, 5, 28, 9, 0))
        t.pause(at=datetime(2026, 5, 28, 9, 30))
        assert t.state == State.PAUSED

    def test_resume_restarts_clock(self):
        t = Tracker()
        t.start("email", now=datetime(2026, 5, 28, 9, 0))
        t.pause(at=datetime(2026, 5, 28, 9, 30))
        t.resume(now=datetime(2026, 5, 28, 10, 0))
        assert t.state == State.RUNNING

    def test_toggle_pause_then_resume(self):
        t = Tracker()
        t.start("email", now=datetime(2026, 5, 28, 9, 0))
        t.toggle(now=datetime(2026, 5, 28, 9, 30))
        assert t.state == State.PAUSED
        t.toggle(now=datetime(2026, 5, 28, 10, 0))
        assert t.state == State.RUNNING

    def test_pause_noop_when_already_paused(self):
        t = Tracker()
        t.start("email", now=datetime(2026, 5, 28, 9, 0))
        t.pause(at=datetime(2026, 5, 28, 9, 30))
        before = t.accumulated_seconds
        t.pause(at=datetime(2026, 5, 28, 9, 45))
        assert t.accumulated_seconds == before

    def test_resume_noop_when_already_running(self):
        t = Tracker()
        t.start("email", now=datetime(2026, 5, 28, 9, 0))
        t.resume(now=datetime(2026, 5, 28, 9, 5))  # should be a no-op
        assert t.state == State.RUNNING

    def test_reset_clears_everything(self):
        t = Tracker()
        t.start("email", now=datetime(2026, 5, 28, 9, 0))
        t.reset()
        assert t.state == State.PAUSED
        assert t.tag is None
        assert t.elapsed_seconds() == 0.0

    def test_start_overwrites_previous_session(self):
        t = Tracker()
        t.start("email", now=datetime(2026, 5, 28, 9, 0))
        t.pause(at=datetime(2026, 5, 28, 9, 30))
        t.start("coding", now=datetime(2026, 5, 28, 10, 0))
        # The 30-min email interval must be discarded
        assert t.elapsed_seconds(now=datetime(2026, 5, 28, 10, 15)) == pytest.approx(15 * 60)
        assert t.tag == "coding"

    def test_set_tag(self):
        t = Tracker()
        t.start(now=datetime(2026, 5, 28, 9, 0))
        t.set_tag("meetings")
        assert t.tag == "meetings"


# ---------------------------------------------------------------------------
# Elapsed time math
# ---------------------------------------------------------------------------

class TestElapsedTime:
    def test_elapsed_while_running(self):
        t = Tracker()
        t.start("email", now=datetime(2026, 5, 28, 9, 0))
        assert t.elapsed_seconds(now=datetime(2026, 5, 28, 9, 30)) == pytest.approx(1800)

    def test_elapsed_frozen_after_pause(self):
        t = Tracker()
        t.start("email", now=datetime(2026, 5, 28, 9, 0))
        t.pause(at=datetime(2026, 5, 28, 9, 30))
        # Time keeps advancing but elapsed must not change
        assert t.elapsed_seconds(now=datetime(2026, 5, 28, 12, 0)) == pytest.approx(1800)

    def test_elapsed_accumulates_across_pauses(self):
        t = Tracker()
        t.start("email", now=datetime(2026, 5, 28, 9, 0))
        t.pause(at=datetime(2026, 5, 28, 9, 30))    # +30 min
        t.resume(now=datetime(2026, 5, 28, 10, 0))
        assert t.elapsed_seconds(now=datetime(2026, 5, 28, 10, 30)) == pytest.approx(3600)

    def test_elapsed_hhmm_format(self):
        t = Tracker()
        t.start("email", now=datetime(2026, 5, 28, 9, 0))
        assert t.elapsed_hhmm(now=datetime(2026, 5, 28, 10, 5)) == "01:05"

    def test_elapsed_hhmm_no_session(self):
        assert Tracker().elapsed_hhmm() == "00:00"

    def test_elapsed_hhmm_over_100_hours(self):
        t = Tracker()
        t.start("marathon", now=datetime(2026, 5, 1, 0, 0))
        assert t.elapsed_hhmm(now=datetime(2026, 5, 5, 12, 0)) == "108:00"

    def test_accumulated_seconds_property(self):
        t = Tracker()
        t.start("coding", now=datetime(2026, 5, 28, 9, 0))
        t.pause(at=datetime(2026, 5, 28, 9, 45))
        assert t.accumulated_seconds == pytest.approx(45 * 60)


# ---------------------------------------------------------------------------
# Idle pause (backdating)
# ---------------------------------------------------------------------------

class TestIdlePause:
    def test_idle_pause_applies_one_minute_grace(self):
        # Session: T=9:00 → idle detected at T=9:35
        # Last input: T=9:30; grace end: T=9:31
        t = Tracker()
        t.start("coding", now=datetime(2026, 5, 28, 9, 0))
        last_input = datetime(2026, 5, 28, 9, 30)
        t.idle_pause(last_input=last_input, now=datetime(2026, 5, 28, 9, 35))
        assert t.accumulated_seconds == pytest.approx(31 * 60)
        assert t.state == State.PAUSED

    def test_idle_pause_discards_full_idle_gap(self):
        # 10-min session; user goes idle 5 min in for 5 more min
        # → keep 6 min (5 worked + 1 grace)
        t = Tracker()
        start = datetime(2026, 5, 28, 9, 0)
        t.start("coding", now=start)
        last_input = start + timedelta(minutes=5)
        t.idle_pause(last_input=last_input, now=start + timedelta(minutes=10))
        assert t.accumulated_seconds == pytest.approx(6 * 60)

    def test_idle_pause_noop_when_already_paused(self):
        t = Tracker()
        t.start("coding", now=datetime(2026, 5, 28, 9, 0))
        t.pause(at=datetime(2026, 5, 28, 9, 30))
        before = t.accumulated_seconds
        t.idle_pause(last_input=datetime(2026, 5, 28, 9, 25))
        assert t.accumulated_seconds == before

    def test_idle_pause_clamps_grace_end_to_now(self):
        # Edge: now is before grace_end (e.g., barely 5-min idle threshold hit).
        # effective_end = min(last_input + 1min, now)
        t = Tracker()
        t.start("coding", now=datetime(2026, 5, 28, 9, 0))
        last_input = datetime(2026, 5, 28, 9, 4)   # 4 min ago
        now = datetime(2026, 5, 28, 9, 4, 30)       # only 30s after last input
        t.idle_pause(last_input=last_input, now=now)
        # grace_end = 9:05, but now = 9:04:30 → effective_end = 9:04:30
        assert t.accumulated_seconds == pytest.approx(4.5 * 60)


# ---------------------------------------------------------------------------
# Midnight split via get_daily_seconds
# ---------------------------------------------------------------------------

class TestDailySeconds:
    def test_single_day_paused(self):
        t = Tracker()
        t.start("coding", now=datetime(2026, 5, 28, 9, 0))
        t.pause(at=datetime(2026, 5, 28, 11, 0))
        assert t.get_daily_seconds() == {date(2026, 5, 28): pytest.approx(7200)}

    def test_running_session_uses_now(self):
        t = Tracker()
        t.start("coding", now=datetime(2026, 5, 28, 9, 0))
        now = datetime(2026, 5, 28, 10, 0)
        assert t.get_daily_seconds(now=now) == {date(2026, 5, 28): pytest.approx(3600)}

    def test_session_spanning_midnight(self):
        t = Tracker()
        t.start("coding", now=datetime(2026, 5, 28, 23, 0))
        t.pause(at=datetime(2026, 5, 29, 2, 0))
        daily = t.get_daily_seconds()
        assert daily[date(2026, 5, 28)] == pytest.approx(3600)
        assert daily[date(2026, 5, 29)] == pytest.approx(7200)

    def test_two_intervals_same_day_sum_correctly(self):
        t = Tracker()
        t.start("coding", now=datetime(2026, 5, 28, 9, 0))
        t.pause(at=datetime(2026, 5, 28, 10, 0))    # 1 h
        t.resume(now=datetime(2026, 5, 28, 11, 0))
        t.pause(at=datetime(2026, 5, 28, 12, 0))    # 1 h
        assert t.get_daily_seconds() == {date(2026, 5, 28): pytest.approx(7200)}

    def test_intervals_spanning_separate_days(self):
        # Interval 1: day28 afternoon; interval 2: straddles day28/29 midnight
        t = Tracker()
        t.start("coding", now=datetime(2026, 5, 28, 14, 0))
        t.pause(at=datetime(2026, 5, 28, 15, 0))    # 1 h day28
        t.resume(now=datetime(2026, 5, 28, 23, 0))
        t.pause(at=datetime(2026, 5, 29, 1, 0))     # 1 h day28 + 1 h day29
        daily = t.get_daily_seconds()
        assert daily[date(2026, 5, 28)] == pytest.approx(2 * 3600)
        assert daily[date(2026, 5, 29)] == pytest.approx(1 * 3600)

    def test_empty_session(self):
        assert Tracker().get_daily_seconds() == {}
