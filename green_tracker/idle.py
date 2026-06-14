"""
idle.py — Win32 idle detection + sleep-gap handling (brief section 3).

While tracking is RUNNING, polls `GetLastInputInfo` every 30 s. If the user
has been idle ≥ `IDLE_THRESHOLD_S`, fires the `on_idle_detected` signal with
the unix timestamp of the last input. The tracker applies the 1-minute
grace policy itself and discards the rest of the idle gap.

Also catches sleep/suspend gaps via a wall-clock backstop: if the wall
clock advances by far more than the poll interval between two ticks, the
excess is treated as a non-worked gap (the OS was suspended). Emits
`on_sleep_gap` with the (start, end) timestamps so main.py can pause the
tracker at the gap start and resume at the gap end.

This is the layer-(b) sleep handling from the brief. Layer-(a) — listening
to Windows `WM_POWERBROADCAST` messages — would require a Qt native event
filter and isn't strictly necessary because the backstop catches every
actual suspend (the wall-clock always advances during sleep). Layer-(a)
can be added later if a tighter response time is needed.

Non-Windows platforms get a no-op shim that lets the module import and
instantiate cleanly but never fires idle events. This module is intended
for native Windows; WSL/Linux preview runs won't exercise the idle check
but the sleep-gap backstop still works there.

Wiring (done in main.py):

    monitor = IdleMonitor()
    monitor.on_idle_detected.connect(on_idle_detected)
    monitor.on_sleep_gap.connect(on_sleep_gap)
    # Start/stop polling in lock-step with tracker state:
    monitor.set_running(tracker.state == State.RUNNING)
"""

from __future__ import annotations

import sys
import time
import ctypes
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal


# ---- Tunable behaviour (brief section 3) ---------------------------------

POLL_INTERVAL_MS         = 2_000      # 2 s between idle checks (only while
                                      # running). Was 30 s — bumped up so we
                                      # can drive a smooth color transition
                                      # on the widget between minutes 1 and
                                      # 3 of idle (the on_idle_progress
                                      # signal). 2 s × 60 = 120 updates over
                                      # the 2-min transition window =
                                      # ~1.67 % color step each tick, below
                                      # the human-perception threshold for
                                      # slow gradient changes.
IDLE_THRESHOLD_S         = 180.0      # 3 min idle → trigger auto-pause
IDLE_TRANSITION_START_S  = 60.0       # 1 min idle → start crossfading the
                                      # widget bg from green toward rust.
                                      # 0 progress at this threshold, 1
                                      # progress at IDLE_THRESHOLD_S.

# Sleep-gap backstop: if `time.time()` advanced by more than this between
# two ticks, treat the excess as the OS having been suspended. Kept as an
# absolute value (was POLL_INTERVAL_MS × WALL_GAP_FACTOR=3 = 90s) so it
# doesn't track the now-shorter poll interval — at 2s × 3 = 6s, any minor
# scheduling hiccup would false-trigger a sleep gap. Real sleeps are
# always far longer than 90s.
WALL_GAP_THRESHOLD_S     = 90.0


# ---- Public class --------------------------------------------------------

class IdleMonitor(QObject):
    """Polls system idle time while tracking is RUNNING.

    Tracker-agnostic — emits signals with raw timestamps and lets main.py
    decide how to translate those into tracker mutations.
    """

    # Idle threshold crossed. Argument: unix timestamp of the last user
    # input (= `now - idle_s`). The tracker owns the grace policy — it
    # adds the 1-minute keep-alive and pauses at that effective end.
    on_idle_detected = Signal(float)

    # Sleep/suspend gap detected via wall-clock check.
    # Arguments: (gap_start_unix, gap_end_unix). The tracker excludes this
    # range from any running stretch.
    on_sleep_gap = Signal(float, float)

    # Idle-transition progress, fired every poll while running.
    # Argument: float in [0, 1]. 0 means idle below the transition-start
    # threshold (the widget should look fully green); 1 means at or past
    # the auto-pause threshold (full rust). Linear in between.
    # main.py wires this to widget.set_idle_progress so the bg color
    # crossfades from green to rust during the last 2 minutes of idle,
    # giving the user a visual countdown before auto-pause fires.
    idle_progress_changed = Signal(float)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        self._last_tick_wall: Optional[float] = None
        self._is_running = False
        # Last progress value we emitted. Tracked so we don't re-emit
        # identical values every 2s (e.g. once we're at 0 with the user
        # actively typing, or stuck at 1 between the auto-pause threshold
        # and the actual pause firing). Float-compared with a small
        # tolerance to swallow noise.
        self._last_progress_emitted: float = 0.0

    # ---- Public API -----------------------------------------------------

    def set_running(self, running: bool) -> None:
        """Tracker calls this when its RUNNING state changes. Polling only
        happens while running — paused state has no idle check (brief §2)."""
        if running == self._is_running:
            return
        self._is_running = running
        if running:
            # Anchor the wall clock so the first tick doesn't spuriously
            # flag the time-between-start-and-first-poll as a sleep gap.
            self._last_tick_wall = time.time()
            self._timer.start()
        else:
            self._timer.stop()
            self._last_tick_wall = None
            # Emit progress=0 on stop so the widget always converges to
            # full-green during pauses, regardless of what idle phase we
            # were in when the user (or auto-pause) stopped tracking.
            # Set-and-emit goes through the same path as a normal tick
            # so downstream behavior is identical.
            if self._last_progress_emitted != 0.0:
                self._last_progress_emitted = 0.0
                self.idle_progress_changed.emit(0.0)

    # ---- Internal -------------------------------------------------------

    def _tick(self) -> None:
        now = time.time()

        # Sleep-gap backstop. The check runs every tick — if the OS was
        # suspended between two polls, `now - last_tick_wall` will be
        # noticeably larger than the poll interval. The whole gap (not
        # just the excess) is reported so the tracker can decide exactly
        # how to cut its running stretch. Threshold is absolute (was
        # POLL_INTERVAL_MS × WALL_GAP_FACTOR) — at 2s polling, the
        # factor approach would have given 6s, far too sensitive to
        # ordinary scheduling lag spikes.
        if self._last_tick_wall is not None:
            elapsed = now - self._last_tick_wall
            if elapsed > WALL_GAP_THRESHOLD_S:
                self.on_sleep_gap.emit(self._last_tick_wall, now)
        self._last_tick_wall = now

        # Idle check (Win32 only — stub returns 0 elsewhere, so the
        # threshold is never crossed during non-Windows development).
        idle_s = _seconds_since_last_input()

        # Idle-transition progress: 0 below the transition-start
        # threshold, linear 0 → 1 between transition-start and the
        # auto-pause threshold, capped at 1 past the threshold. Only
        # emitted when changed enough to matter (tolerance below human
        # perception for a single tick).
        if idle_s < IDLE_TRANSITION_START_S:
            progress = 0.0
        elif idle_s >= IDLE_THRESHOLD_S:
            progress = 1.0
        else:
            span = IDLE_THRESHOLD_S - IDLE_TRANSITION_START_S
            progress = (idle_s - IDLE_TRANSITION_START_S) / span
        if abs(progress - self._last_progress_emitted) > 1e-3:
            self._last_progress_emitted = progress
            self.idle_progress_changed.emit(progress)

        if idle_s >= IDLE_THRESHOLD_S:
            last_input_unix = now - idle_s
            self.on_idle_detected.emit(last_input_unix)


# ---- Platform shim ------------------------------------------------------

if sys.platform == "win32":

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("dwTime", ctypes.c_ulong),
        ]

    _user32   = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    def _seconds_since_last_input() -> float:
        """Seconds since the last keyboard / mouse input was received by
        any window in the user's session.

        `GetTickCount()` and `LASTINPUTINFO.dwTime` are both 32-bit
        millisecond counters that wrap every ~49.7 days. Mask to 32 bits
        before subtracting so the wrap is handled cleanly."""
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        _user32.GetLastInputInfo(ctypes.byref(info))
        now_ms  = _kernel32.GetTickCount() & 0xFFFFFFFF
        last_ms = info.dwTime               & 0xFFFFFFFF
        elapsed_ms = (now_ms - last_ms) & 0xFFFFFFFF
        return elapsed_ms / 1000.0

else:

    def _seconds_since_last_input() -> float:
        """No-op on non-Windows platforms. Always returns 0 so the idle
        threshold is never crossed during development on WSL / macOS / Linux.
        The sleep-gap backstop in `IdleMonitor._tick` continues to work
        cross-platform — only the per-poll idle check is short-circuited."""
        return 0.0
