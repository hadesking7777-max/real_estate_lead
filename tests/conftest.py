"""
Shared pytest fixtures for the whole suite.

Several application modules (webhook_server, scheduler, send, alerts) read
environment variables at IMPORT time and are only ever imported ONCE per
process. Since pytest runs the whole suite in a single process, we set a
baseline set of env vars here and import those modules exactly once, before
any test module gets to import them itself (conftest.py is always imported
first). Every test after that just gets the already-imported module back
from sys.modules.

Per-test isolation for the SQLite-backed lead_store is handled separately
(see the `tmp_db` fixture below) since DB_PATH is read dynamically by
lead_store._conn() at call time, not cached anywhere.
"""

import os
import sys

import pytest

BOT = r"F:\Bid_Template - Gabriel\Real_Estate_Lead\bot"
if BOT not in sys.path:
    sys.path.insert(0, BOT)

# Baseline env vars needed at import time by webhook_server / scheduler / send.
# Individual tests that need different values (e.g. the login-hardening tests)
# save/restore os.environ themselves and reload webhook_server locally.
PANEL_USER = "lucas"
PANEL_PASSWORD = "test-panel-password"
VERIFY_TOKEN = "test-verify-token"

os.environ.setdefault("VERIFY_TOKEN", VERIFY_TOKEN)
os.environ.setdefault("PHONE_NUMBER_ID", "test-phone-id")
os.environ.setdefault("WHATSAPP_TOKEN", "test-whatsapp-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("PANEL_USER", PANEL_USER)
os.environ.setdefault("PANEL_PASSWORD", PANEL_PASSWORD)

import lead_store  # noqa: E402
import scheduler  # noqa: E402
import send  # noqa: E402
import alerts  # noqa: E402
import send_campaign  # noqa: E402
import run_history  # noqa: E402
import webhook_server  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Point lead_store at a fresh, empty SQLite file for this test only.

    Mirrors every scratch script's DB-isolation boilerplate:
        lead_store.DB_PATH = <fresh tmp file>
        lead_store._initialized_paths.clear()
    lead_store functions read DB_PATH dynamically via _conn(), so repointing
    it (and clearing the "already initialized this path" cache) is enough --
    no need to re-import lead_store per test. Autouse because every single
    test in this suite touches the DB, directly or indirectly (webhook_server
    routes all go through lead_store too).
    """
    db_path = str(tmp_path / "test_leads.db")
    monkeypatch.setattr(lead_store, "DB_PATH", db_path)
    lead_store._initialized_paths.clear()
    yield db_path
    lead_store._initialized_paths.discard(db_path)


@pytest.fixture
def client():
    """Flask test client for webhook_server.app, unauthenticated."""
    return webhook_server.app.test_client()


@pytest.fixture
def authed_client(client):
    """Flask test client with the panel session auth already set, the same
    session_transaction() bypass every scratch script used."""
    with client.session_transaction() as sess:
        sess["auth"] = True
    return client


@pytest.fixture
def fake_sender():
    """A scheduler sender stub that records every (phone, template) call and
    always reports a successful WhatsApp API send."""
    calls = []

    def _send(phone, template, first_name):
        calls.append((phone, template))
        return 200, '{"messages":[{"id":"wamid.x"}]}'

    _send.calls = calls
    return _send


def seed_lead(phone, nome=None, **fields):
    """Create (or fetch) a lead and apply any extra field overrides in one call --
    the same seed()/get_or_create_lead()+update_lead() pattern every scratch
    scheduler script repeated by hand."""
    lead_store.get_or_create_lead(phone, nome=nome if nome is not None else f"Lead {phone}")
    if fields:
        lead_store.update_lead(phone, **fields)
    return lead_store.get_lead(phone)
