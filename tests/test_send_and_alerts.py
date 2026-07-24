"""
Direct tests of send.py (the WhatsApp Graph API client) and alerts.py (best-
effort SMTP email alerts), independent of the scheduler or the webhook server.

Ported from: test_health_alert.py (part 1 -- send.check_health() itself),
test_hot_alert.py (parts 1-3 -- alerts.send_hot_lead_alert content/failure
handling).
"""

import json

import lead_store
import send
import alerts


class FakeResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


# ---------- send.check_health() ----------

def test_check_health_hits_the_phone_number_endpoint_and_parses_the_body(monkeypatch):
    monkeypatch.setattr(send.requests, "get", lambda url, headers, params, timeout: FakeResp(
        200, json.dumps({"status": "CONNECTED", "quality_rating": "GREEN", "verified_name": "Cyrela"})))
    status_code, body = send.check_health()
    assert status_code == 200
    assert json.loads(body)["status"] == "CONNECTED"


# ---------- alerts.send_hot_lead_alert ----------

def test_unconfigured_alert_is_a_safe_no_op():
    lead = {"phone": "5511900000001", "nome": "Fulano", "signals": {"objetivo": "renda"}}
    assert alerts.configured() is False
    assert alerts.send_hot_lead_alert(lead) is False


def test_configured_alert_sends_via_gmail_smtp_with_right_recipient_and_content(monkeypatch):
    lead = {"phone": "5511900000001", "nome": "Fulano", "signals": {"objetivo": "renda"}}
    lead_store.set_setting("ALERT_EMAIL_TO", "lucas@example.com")
    lead_store.set_setting("SMTP_USER", "bot@gmail.com")
    lead_store.set_setting("SMTP_PASSWORD", "app-password-123")
    assert alerts.configured() is True

    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls["host"] = host
            calls["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, pw):
            calls["login"] = (user, pw)

        def sendmail(self, frm, to, msg):
            calls["sendmail"] = (frm, to, msg)

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)

    ok = alerts.send_hot_lead_alert(lead)

    assert ok is True
    assert calls["login"] == ("bot@gmail.com", "app-password-123")
    assert calls["sendmail"][0] == "bot@gmail.com"
    assert calls["sendmail"][1] == ["lucas@example.com"]
    # body is base64/quoted-printable encoded per the utf-8 charset -- decode properly
    import email
    parsed = email.message_from_string(calls["sendmail"][2])
    decoded_body = parsed.get_payload(decode=True).decode(parsed.get_content_charset() or "utf-8")
    assert "Fulano" in decoded_body, decoded_body
    assert "renda" in decoded_body, decoded_body


def test_smtp_failure_is_swallowed_never_raises(monkeypatch):
    lead = {"phone": "5511900000001", "nome": "Fulano", "signals": {"objetivo": "renda"}}
    lead_store.set_setting("ALERT_EMAIL_TO", "lucas@example.com")
    lead_store.set_setting("SMTP_USER", "bot@gmail.com")
    lead_store.set_setting("SMTP_PASSWORD", "app-password-123")

    class BoomSMTP:
        def __init__(self, *a, **k):
            raise RuntimeError("network is down")

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP_SSL", BoomSMTP)

    ok = alerts.send_hot_lead_alert(lead)
    assert ok is False
