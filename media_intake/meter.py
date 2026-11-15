"""Counts-only progress for the long venv jobs (semantic.py index, vlm_trash.py). Stdlib only.

A line every MEDIA_INTAKE_PROGRESS_EVERY items or MEDIA_INTAKE_PROGRESS_SECS seconds, whichever
comes first (the same variables as intake.py's Progress; tests drive the lines by count with
SECS=inf). It reads `<label>: done/total <unit> (pct), rate, about <time> left` and never
carries a file name. On a terminal the line is rewritten in place with \\r; in a log (the queue,
nohup) each update is its own line.
"""
import os, sys, time

EVERY, SECS, TTY_SECS = 100, 10.0, 1.0


def duration(s):
    """'45s', '28m 05s', '1h 02m'."""
    s = max(0, int(round(s)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


class Meter:
    def __init__(self, label, total, unit="files", out=None, tty=None, clock=time.monotonic, every=EVERY):
        self.label, self.total, self.unit, self.clock = label, total, unit, clock
        self.out = out or sys.stderr
        if tty is None:
            try:
                tty = self.out.isatty()
            except (AttributeError, ValueError):
                tty = False
        self.tty, self.width, self.done = tty, 0, 0
        self.every = max(1, int(os.environ.get("MEDIA_INTAKE_PROGRESS_EVERY") or every))
        self.secs = float(os.environ.get("MEDIA_INTAKE_PROGRESS_SECS") or (TTY_SECS if tty else SECS))
        self.t0 = self.last = clock()

    def line(self):
        n, tot = self.done, self.total
        pct = f" ({n * 100 // tot}%)" if tot else ""
        out = f"{self.label}: {n:,}/{tot:,} {self.unit}{pct}"
        dt = self.clock() - self.t0
        if n and dt > 0:
            rate = n / dt
            out += f", {rate:.1f} {self.unit}/s" if rate >= 1 else f", {dt / n:.1f} s per {self.unit.rstrip('s')}"
            out += f", about {duration((tot - n) / rate)} left" if tot > n else ""
        return out

    def _emit(self, text, final=False):
        if self.tty:
            self.out.write("\r" + text.ljust(self.width) + ("\n" if final else ""))
            self.width = 0 if final else len(text)
        else:
            self.out.write(text + "\n")
        self.out.flush()

    def update(self, done):
        """done items so far; prints when the count or the clock says so."""
        self.done = done
        now = self.clock()
        if done % self.every == 0 or now - self.last >= self.secs:
            self.last = now
            self._emit(self.line())

    def say(self, text):
        """A line of its own (a start or summary line); ends an in-place progress line first."""
        self._emit(text, final=True)

    def close(self):
        """End an in-place progress line (on an interrupt too)."""
        if self.tty and self.width:
            self.out.write("\n")
            self.out.flush()
            self.width = 0

    def elapsed(self):
        return duration(self.clock() - self.t0)
