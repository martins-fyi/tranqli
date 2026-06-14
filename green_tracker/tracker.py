from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum


class State(Enum):
    RUNNING = "running"
    PAUSED = "paused"


def split_at_midnight(
    start: datetime, end: datetime
) -> list[tuple[date, float]]:
    """Split a time interval at local midnight boundaries.

    Returns a list of (date, seconds) pairs covering the interval.
    Zero-duration intervals return one entry with 0.0 seconds.
    """
    if end <= start:
        return [(start.date(), 0.0)]
    result: list[tuple[date, float]] = []
    current = start
    while current.date() < end.date():
        next_midnight = datetime.combine(current.date() + timedelta(days=1), time.min)
        result.append((current.date(), (next_midnight - current).total_seconds()))
        current = next_midnight
    result.append((end.date(), (end - current).total_seconds()))
    return result


@dataclass
class Tracker:
    """RUNNING / PAUSED state machine with on-demand elapsed-time math."""

    tag: str | None = None
    _intervals: list[tuple[datetime, datetime]] = field(
        default_factory=list, init=False, repr=False
    )
    _start: datetime | None = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> State:
        return State.RUNNING if self._start is not None else State.PAUSED

    @property
    def accumulated_seconds(self) -> float:
        """Seconds in completed (paused) intervals, not counting current stretch."""
        return sum((e - s).total_seconds() for s, e in self._intervals)

    # ------------------------------------------------------------------
    # Elapsed time
    # ------------------------------------------------------------------

    def elapsed_seconds(self, now: datetime | None = None) -> float:
        if now is None:
            now = datetime.now()
        total = self.accumulated_seconds
        if self._start is not None:
            total += (now - self._start).total_seconds()
        return max(0.0, total)

    def elapsed_hhmm(self, now: datetime | None = None) -> str:
        secs = int(self.elapsed_seconds(now))
        h, rem = divmod(secs, 3600)
        m = rem // 60
        return f"{h:02d}:{m:02d}"

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def start(self, tag: str | None = None, now: datetime | None = None) -> None:
        """Begin a fresh session, discarding any previous state."""
        if now is None:
            now = datetime.now()
        self.tag = tag
        self._intervals = []
        self._start = now

    def pause(self, at: datetime | None = None) -> None:
        """Fold the running stretch into accumulated intervals and stop the clock."""
        if self._start is None:
            return
        if at is None:
            at = datetime.now()
        at = max(at, self._start)  # never fold a negative stretch
        self._intervals.append((self._start, at))
        self._start = None

    def resume(self, now: datetime | None = None) -> None:
        """Restart the clock from PAUSED state."""
        if self._start is not None:
            return  # already running
        if now is None:
            now = datetime.now()
        self._start = now

    def toggle(self, now: datetime | None = None) -> None:
        if now is None:
            now = datetime.now()
        if self._start is not None:
            self.pause(at=now)
        else:
            self.resume(now=now)

    def idle_pause(self, last_input: datetime, now: datetime | None = None) -> None:
        """Auto-pause on idle detection.

        Backdates the end of the running stretch to last_input + 1 minute,
        discarding the idle tail.  The 1-minute grace matches the spec.
        """
        if self._start is None:
            return
        if now is None:
            now = datetime.now()
        effective_end = min(last_input + timedelta(minutes=1), now)
        self.pause(at=effective_end)

    def set_tag(self, tag: str) -> None:
        self.tag = tag

    def reset(self) -> None:
        self.tag = None
        self._intervals = []
        self._start = None

    # ------------------------------------------------------------------
    # Midnight split — used by storage at save time
    # ------------------------------------------------------------------

    def get_daily_seconds(self, now: datetime | None = None) -> dict[date, float]:
        """Return total worked seconds grouped by calendar date.

        Splits every interval (including the live running stretch) at
        midnight so multi-day sessions are attributed correctly.
        """
        if now is None:
            now = datetime.now()
        all_intervals = list(self._intervals)
        if self._start is not None:
            all_intervals.append((self._start, now))
        daily: dict[date, float] = {}
        for s, e in all_intervals:
            for d, secs in split_at_midnight(s, e):
                daily[d] = daily.get(d, 0.0) + secs
        return daily
