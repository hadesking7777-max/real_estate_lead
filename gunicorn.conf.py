"""
Gunicorn config for production. Flask's own dev server (what webhook_server.py
runs directly) prints its own warning about not being meant for production: no
real process management, single-threaded by default, no protection against a
slow request wedging the whole process. Gunicorn replaces just that part; the
app, nginx, and systemd service stay the same.

Usage (see systemd unit):
    gunicorn -c gunicorn.conf.py webhook_server:app
"""

import scheduler

bind = "127.0.0.1:8000"  # unchanged from webhook_server.py's own default; nginx proxies to this

# One worker process, several threads. The reasons this app wants exactly one
# process rather than gunicorn's usual "2 x cpu + 1" advice:
#   1. lead_store.py is SQLite -- a single writer at a time regardless of how
#      many processes ask, so extra processes buy nothing and only add lock
#      contention on top of what _retry_on_locked already smooths over.
#   2. The traffic here is one broker's dashboard plus WhatsApp webhook
#      events -- nowhere near needing more than one process to keep up.
# Threads (not more processes) give the concurrency that actually matters:
# an open SSE connection on /eventos must not block webhook or panel requests,
# same as threaded=True does today on the Flask dev server.
workers = 1
worker_class = "gthread"
threads = 8

# send.py's WhatsApp calls use a 30s HTTP timeout; give gunicorn's own worker
# watchdog real headroom above that so it never kills a worker mid-request
# right as the slow call was about to finish or fail on its own.
timeout = 60
graceful_timeout = 60

accesslog = "-"   # stdout -> journalctl, matching how the dev server's output was read
errorlog = "-"


def on_starting(server):
    """Runs exactly once in the master process before any worker is forked --
    regardless of the worker count above -- so the campaign scheduler thread
    (follow-ups, autopilot, manual batch pacing) never ends up duplicated per
    worker. It talks to the same SQLite file the workers use and doesn't need
    a Flask request context, so running it here instead of in a request
    worker is safe.
    """
    scheduler.start_background()
