"""
Tiny in-process change signal for push-based UI updates.

Anything that changes state (a send, an inbound/status webhook, an import)
calls bump(). The SSE endpoint blocks in wait_for_change() and wakes the
instant bump() is called, so the panel updates immediately, not on a timer.
"""

import threading

_cond = threading.Condition()
_version = 0


def bump():
    global _version
    with _cond:
        _version += 1
        _cond.notify_all()


def current_version():
    with _cond:
        return _version


def wait_for_change(last_version, timeout):
    """Block until the version differs from last_version (or timeout). Returns the version."""
    with _cond:
        if _version != last_version:
            return _version
        _cond.wait(timeout)
        return _version
