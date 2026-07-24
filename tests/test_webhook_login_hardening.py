"""
Login hardening that depends on webhook_server's IMPORT-TIME logic: the
random one-time password generated when no PANEL_PASSWORD is configured
anywhere (env or DB), and DB-backed login-attempt throttling.

webhook_server is only imported ONCE per process (see conftest.py), but
PANEL_USER/PANEL_PASSWORD/VERIFY_TOKEN are computed at module import time
from os.environ. To exercise that logic under different env configurations,
each test here temporarily changes os.environ and calls
importlib.reload(webhook_server) -- then, in a finally block, restores the
original env and reloads again so the shared webhook_server module (and any
other test file's fixtures built on top of it) is left exactly as every
other test file expects it. Isolated in its own file/module so this reload
dance can't accidentally leak into unrelated tests.

Ported from: test_login_hardening.py.
"""

import contextlib
import importlib
import os

import lead_store
import webhook_server as w


@contextlib.contextmanager
def _temp_env(**changes):
    """changes: env var name -> new value, or None to unset it for the duration."""
    previous = {k: os.environ.get(k) for k in changes}
    try:
        for k, v in changes.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_default_password_generated_when_unset_changeme_no_longer_works():
    with _temp_env(PANEL_USER=None, PANEL_PASSWORD=None):
        importlib.reload(w)
        assert w.PANEL_PASSWORD != "changeme"
        assert len(w.PANEL_PASSWORD) >= 12
        client = w.app.test_client()

        resp = client.post("/login", data={"usuario": "lucas", "senha": "changeme", "next": "/painel"})
        assert resp.status_code == 401, resp.status_code

        resp = client.post("/login", data={"usuario": "lucas", "senha": w.PANEL_PASSWORD, "next": "/painel"})
        assert resp.status_code == 302, resp.status_code
    importlib.reload(w)  # restore the shared module to the baseline env for other test files


def test_login_lockout_after_max_attempts_and_unlock_after_window():
    with _temp_env(PANEL_PASSWORD="correcthorsebatterystaple"):
        importlib.reload(w)
        client = w.app.test_client()
        for i in range(w.MAX_LOGIN_ATTEMPTS):
            resp = client.post("/login", data={"usuario": "lucas", "senha": "wrong", "next": "/painel"})
            assert resp.status_code == 401, (i, resp.status_code)

        # the next attempt, even with the CORRECT password, is locked out
        resp = client.post("/login", data={"usuario": "lucas", "senha": "correcthorsebatterystaple",
                                            "next": "/painel"})
        assert resp.status_code == 429, resp.status_code

        # simulate the lockout window elapsing
        lead_store.set_setting("login_locked_until", "0")
        resp = client.post("/login", data={"usuario": "lucas", "senha": "correcthorsebatterystaple",
                                            "next": "/painel"})
        assert resp.status_code == 302, resp.status_code
    importlib.reload(w)


def test_successful_login_resets_the_failure_counter():
    with _temp_env(PANEL_PASSWORD="correcthorsebatterystaple"):
        importlib.reload(w)
        client = w.app.test_client()
        client.post("/login", data={"usuario": "lucas", "senha": "wrong", "next": "/painel"})
        client.post("/login", data={"usuario": "lucas", "senha": "wrong", "next": "/painel"})
        resp = client.post("/login", data={"usuario": "lucas", "senha": "correcthorsebatterystaple",
                                            "next": "/painel"})
        assert resp.status_code == 302, resp.status_code
        assert lead_store.get_setting("login_fail_count") in (None, "0")
    importlib.reload(w)
