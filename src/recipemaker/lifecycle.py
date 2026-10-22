"""A recipe command ends with its caller: a signal or an orphaned process unwinds it, so no slot outlives it."""

import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

_POLL_SECONDS = 2.0
_ENDING = (signal.SIGTERM, signal.SIGHUP)


def _stop(number: int, frame: FrameType | None) -> None:
    """Unwind the command (its `finally` blocks kill the children and free the slots) with the shell exit code."""
    raise SystemExit(128 + number)


@contextmanager
def bound(poll: float = _POLL_SECONDS) -> Iterator[None]:
    """For the length of a command: SIGTERM and SIGHUP unwind it, and so does the death of its parent process, seen by
    a timer signal (no thread) that finds init as the new parent."""
    parent = os.getppid()

    def orphaned(number: int, frame: FrameType | None) -> None:
        if os.getppid() != parent:
            _stop(signal.SIGTERM, frame)

    old = {number: signal.signal(number, _stop) for number in _ENDING}
    old[signal.SIGALRM] = signal.signal(signal.SIGALRM, orphaned)
    signal.setitimer(signal.ITIMER_REAL, poll, poll)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for number, handler in old.items():
            signal.signal(number, handler)
