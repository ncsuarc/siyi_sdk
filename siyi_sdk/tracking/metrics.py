"""Score a point lock: how far off it stays, how fast it recovers, and how much it twitches.

Pure Python, fed one sample per control step, so the same numbers come from a live lock,
an exported log and the simulator. All angles are degrees.

- **Error:** RMS and 95th percentile of the pointing error while tracking, outside the
  recovery from a jump (those are scored as events).
- **Jitter:** direction reversals of the speed command per second, per axis. A smooth lock
  reverses only when the target does; a twitchy one reverses many times a second.
- **Events:** a jump is the error growing past ``jump_deg`` while no earlier jump is still
  being recovered from (the first is usually the lock's initial acquisition). Each event
  records how long the error took to halve (``half_s``), how long until it stayed under
  ``settle_deg`` (``settle_s``), and how far it overshot past the target (``overshoot_deg``,
  along the direction of the jump).
- **Gaps:** the longest time between samples, which shows a stalled control loop.
"""

from __future__ import annotations

import asyncio
import math
from array import array
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _Event:
    start: float
    direction: tuple[float, float]  # unit vector of the error when the jump was seen
    peak: float
    half_s: float | None = None
    settle_s: float | None = None
    overshoot: float = 0.0
    settled_since: float | None = None


class LockMetrics:
    """Accumulate per-step samples of one lock and summarize them."""

    def __init__(
        self, *, jump_deg: float = 2.0, settle_deg: float = 0.5, settle_hold_s: float = 0.3
    ) -> None:
        """Create empty metrics; see the module docstring for the thresholds."""
        self.jump_deg = jump_deg
        self.settle_deg = settle_deg
        self.settle_hold_s = settle_hold_s
        self._start: float | None = None
        self._last: float | None = None
        self._max_gap = 0.0
        self._errors = array("d")
        self._steady_squared = 0.0
        self._all_squared = 0.0
        self._samples = 0
        self._signs = [0, 0]
        self._reversals = [0, 0]
        self._events: list[_Event] = []
        self._open: _Event | None = None

    def add(self, t: float, error: tuple[float, float], command: tuple[float, float]) -> None:
        """Record one control step at time ``t``: pointing error and the command sent."""
        if self._start is None:
            self._start = t
        if self._last is not None:
            self._max_gap = max(self._max_gap, t - self._last)
        self._last = t
        magnitude = math.hypot(*error)
        self._all_squared += magnitude * magnitude
        self._samples += 1

        for axis, value in enumerate(command):
            sign = (value > 0) - (value < 0)
            if sign and self._signs[axis] and sign != self._signs[axis]:
                self._reversals[axis] += 1
            if sign:
                self._signs[axis] = sign

        event = self._open
        if event is None and magnitude > self.jump_deg:
            direction = (error[0] / magnitude, error[1] / magnitude)
            event = self._open = _Event(t, direction, magnitude)
            self._events.append(event)
        if event is not None:
            event.peak = max(event.peak, magnitude)
            along = error[0] * event.direction[0] + error[1] * event.direction[1]
            event.overshoot = max(event.overshoot, -along)
            if event.half_s is None and magnitude <= event.peak / 2:
                event.half_s = t - event.start
            if magnitude <= self.settle_deg:
                event.settled_since = event.settled_since if event.settled_since is not None else t
                if t - event.settled_since >= self.settle_hold_s:
                    event.settle_s = event.settled_since - event.start
                    self._open = None
            else:
                event.settled_since = None
        else:
            self._errors.append(magnitude)
            self._steady_squared += magnitude * magnitude

    async def summary_async(self) -> dict[str, Any]:
        """Summarize a stopped lock off the event loop; do not add samples concurrently."""
        return await asyncio.to_thread(self.summary)

    def summary(self) -> dict[str, Any]:
        """Numbers for the log; None where there were no samples to judge."""
        duration = 0.0 if self._start is None or self._last is None else self._last - self._start
        errors = sorted(self._errors)

        def median(values: list[float]) -> float | None:
            values = sorted(values)
            return round(values[len(values) // 2], 3) if values else None

        events = self._events
        return {
            "duration_s": round(duration, 2),
            "rms_deg": (
                round(math.sqrt(self._steady_squared / len(errors)), 3) if errors else None
            ),
            "p95_deg": (
                round(errors[min(len(errors) - 1, int(len(errors) * 0.95))], 3) if errors else None
            ),
            # Every sample, jumps included: the score for continuous motion that never settles.
            "rms_all_deg": (
                round(math.sqrt(self._all_squared / self._samples), 3) if self._samples else None
            ),
            "reversals_per_s": (
                [round(r / duration, 2) for r in self._reversals] if duration > 0 else None
            ),
            "max_gap_ms": round(self._max_gap * 1000, 1),
            "events": len(events),
            "half_s": median([e.half_s for e in events if e.half_s is not None]),
            "settle_s": median([e.settle_s for e in events if e.settle_s is not None]),
            "unsettled_events": sum(1 for e in events if e.settle_s is None),
            "overshoot_deg": median([e.overshoot for e in events]),
        }
