"""Precursor of the Xid 62 (PMU halted) on card 25a0c115: the GPU reports SW power cap while drawing well
below its siblings.

Found on 2026-11-18 (NOTAS.md, "Análisis 2026-11-18"): 10-50 min before each of the three Xid 62 of that day,
41:00 had clocks_event bit 0x4 (SW power cap) set at 150-190 W under a 220 W limit, while the other three GPUs,
doing the same work, drew 210-220 W. In 8 days of 1 Hz telemetry (training and inference) it never showed
up otherwise.

A sample of one GPU counts when every GPU is busy (util >= BUSY_UTIL): it is "suspect" if that GPU has the
SW power cap bit and draws less than RATIO x the mean of the others. Over a rolling WINDOW_S window, an
alert fires when at least MIN_SAMPLES samples count and SUSPECT_SHARE of them are suspect; it clears below
CLEAR_SHARE. Pure logic: the caller feeds lines and acts on the returned events.
"""
from __future__ import annotations

import collections
import dataclasses
import datetime as dt

BUSY_UTIL = 90
RATIO = 0.9
WINDOW_S = 600
MIN_SAMPLES = 120
SUSPECT_SHARE = 0.30
CLEAR_SHARE = 0.10
SW_POWER_CAP = 0x4


@dataclasses.dataclass(frozen=True)
class Sample:
    bus: str
    power_w: float
    util: int
    reasons: int


@dataclasses.dataclass(frozen=True)
class Event:
    kind: str  # "alert" or "clear"
    at: dt.datetime
    bus: str
    share: float  # share of suspect samples in the window
    ratio: float  # mean power of this GPU / mean of the others, over the window


def parse(line: str) -> tuple[dt.datetime, Sample] | None:
    """One CSV line of gpu_telemetry (FIELDS order); None if it cannot be read (ERR!, [N/A], header)."""
    f = [x.strip() for x in line.split(",")]
    if len(f) < 15:
        return None
    try:
        t = dt.datetime.strptime(f[0][:19], "%Y/%m/%d %H:%M:%S")
        return t, Sample(bus=f[2][-12:-2], power_w=float(f[8].split()[0]),
                         util=int(f[13].split()[0]), reasons=int(f[7], 16))
    except (ValueError, IndexError):
        return None


class Detector:
    def __init__(self) -> None:
        self._tick: tuple[str, dict[str, Sample]] | None = None  # timestamp text, samples of that tick
        self._win: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._alerted: set[str] = set()

    def feed(self, line: str) -> list[Event]:
        """Feed one telemetry line; returns the events decided when a tick (one line per GPU) completes."""
        parsed = parse(line)
        if parsed is None:
            return []
        t, s = parsed
        events: list[Event] = []
        # One tick = one line per GPU; their milliseconds differ, so a tick ends when a GPU repeats.
        if self._tick is not None and s.bus in self._tick[1]:
            events = self._close(self._tick[1])
            self._tick = None
        if self._tick is None:
            self._tick = ("", {"_t": t})  # type: ignore[dict-item]
        self._tick[1][s.bus] = s
        return events

    def _close(self, tick: dict) -> list[Event]:
        t = tick.pop("_t")
        if len(tick) < 2 or any(s.util < BUSY_UTIL for s in tick.values()):
            return self._expire(t)
        for bus, s in tick.items():
            others = [o.power_w for b, o in tick.items() if b != bus]
            mean_others = sum(others) / len(others)
            suspect = bool(s.reasons & SW_POWER_CAP) and s.power_w < RATIO * mean_others
            self._win[bus].append((t, suspect, s.power_w / mean_others if mean_others else 1.0))
        return self._expire(t)

    def _expire(self, now: dt.datetime) -> list[Event]:
        events = []
        for bus, win in self._win.items():
            while win and (now - win[0][0]).total_seconds() > WINDOW_S:
                win.popleft()
            if len(win) < MIN_SAMPLES:
                continue
            share = sum(1 for x in win if x[1]) / len(win)
            ratio = sum(x[2] for x in win) / len(win)
            if bus not in self._alerted and share >= SUSPECT_SHARE:
                self._alerted.add(bus)
                events.append(Event("alert", now, bus, share, ratio))
            elif bus in self._alerted and share < CLEAR_SHARE:
                self._alerted.discard(bus)
                events.append(Event("clear", now, bus, share, ratio))
        return events


def replay(paths: list[str]) -> None:
    """Run the detector over saved telemetry (.csv or .csv.gz) and print its events."""
    import gzip
    det = Detector()
    for p in paths:
        op = gzip.open if p.endswith(".gz") else open
        with op(p, "rt") as fh:
            for line in fh:
                for e in det.feed(line):
                    print(f"{e.at}  {e.kind:5}  {e.bus}  suspect={e.share:.0%}  ratio={e.ratio:.2f}")


if __name__ == "__main__":
    import sys
    replay(sys.argv[1:])
