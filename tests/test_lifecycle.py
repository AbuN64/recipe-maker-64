"""A command ends with its caller: signals unwind it and the death of its parent stops it."""

import os
import signal
import time

import pytest

from recipemaker import lifecycle


def test_sigterm_and_sighup_unwind_the_command_and_the_old_handlers_come_back():
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as stopped, lifecycle.bound():
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)
    assert stopped.value.code == 128 + signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) == before
    with pytest.raises(SystemExit) as hung, lifecycle.bound():
        os.kill(os.getpid(), signal.SIGHUP)
        time.sleep(1)
    assert hung.value.code == 128 + signal.SIGHUP


def test_an_orphaned_command_unwinds_itself_with_the_sigterm_code(monkeypatch):
    parents = iter([100])
    monkeypatch.setattr(os, "getppid", lambda: next(parents, 1))
    with pytest.raises(SystemExit) as stopped, lifecycle.bound(poll=0.01):
        time.sleep(5)
    assert stopped.value.code == 128 + signal.SIGTERM
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
