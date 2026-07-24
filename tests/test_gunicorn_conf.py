"""
gunicorn.conf.py has exactly one piece of real logic: on_starting(), which
must start the campaign scheduler's background thread exactly once, in the
master process before any worker forks. Gunicorn itself can't run on this
Windows box (no fork/fcntl), so this loads the config file as a plain module
and calls on_starting() directly.

NOTE on side effects: on_starting() starts a REAL daemon thread that lives
for the rest of this test process (there is no stop_background()). Its loop
also does a health check and a weekly-summary check on its very first pass,
before its first sleep. We neutralize both so this test doesn't fire a real
network request or depend on the current wall-clock day:
  - scheduler._last_health_check is set to "now" so the health-check branch's
    interval guard skips a live HTTP call this pass.
  - the weekly-summary check is a safe no-op on a fresh DB regardless (it
    just records a baseline timestamp the first time it's ever called).
We also bump scheduler.TICK_INTERVAL up to a large number BEFORE starting
the thread (a plain, non-reverting assignment, on purpose: the thread keeps
running after this test function returns, so a monkeypatch revert at test
teardown would not help -- we want it to stay large for the rest of the
whole test session, so the leaked thread never ticks again against whatever
tmp DB a later test happens to be using).

Ported from: test_gunicorn_conf.py.
"""

import importlib.util
import os
import time

import scheduler

BOT = r"F:\Bid_Template - Gabriel\Real_Estate_Lead\bot"


def test_on_starting_starts_the_scheduler_background_thread_exactly_once(monkeypatch):
    assert scheduler._thread is None, "sanity: no background thread yet"

    spec = importlib.util.spec_from_file_location(
        "gunicorn_conf", os.path.join(BOT, "gunicorn.conf.py"))
    conf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conf)

    assert conf.workers == 1, "must stay at 1 worker -- see the comment for why"
    assert conf.worker_class == "gthread"
    assert conf.threads >= 2, "need more than 1 thread so an open SSE connection can't block everything else"
    assert conf.timeout > 30, "must clear send.py's 30s HTTP timeout with headroom"

    # neutralize the background loop's first-pass health check (real HTTP call)
    monkeypatch.setattr(scheduler, "_last_health_check", time.time())
    # this assignment is intentionally NOT via monkeypatch -- see module docstring
    scheduler.TICK_INTERVAL = 10 ** 6

    conf.on_starting(server=None)
    time.sleep(0.2)
    assert scheduler._thread is not None and scheduler._thread.is_alive()

    # calling it again (as could happen with --preload + multiple lifecycle calls)
    # must not spin up a second thread -- start_background() already guards this
    before = scheduler._thread
    conf.on_starting(server=None)
    assert scheduler._thread is before
