"""
The Configuracoes settings-in-DB panel: PHONE_NUMBER_ID/WHATSAPP_TOKEN/
ANTHROPIC_API_KEY (and the alert-email/SMTP fields) can be set from the UI
and are stored in the DB, overriding the environment without a restart --
but secret fields are never re-displayed, and leaving one blank on save must
NOT clear it (unlike plain fields, which blank out on purpose).

Ported from: test_settings.py, test_hot_alert.py (part 5 -- the alert-email/
SMTP fields use the same secret-vs-plain semantics).
"""

import os

import lead_store
import send
import qualification


def test_with_nothing_configured_everything_falls_back_to_env_vars():
    assert send._cfg("PHONE_NUMBER_ID") == "test-phone-id"
    assert send._cfg("WHATSAPP_TOKEN") == "test-whatsapp-token"
    assert send._url() == "https://graph.facebook.com/v20.0/test-phone-id/messages"
    key = lead_store.get_setting("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    assert key == "test-anthropic-key"


def test_get_configuracoes_prefills_phone_id_but_never_redisplays_secrets(authed_client):
    r = authed_client.get("/configuracoes")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'value="test-phone-id"' in body, "phone id should be prefilled from the env fallback"
    assert "Configurado" in body or "Configured" in body
    assert "test-whatsapp-token" not in body, "the actual token value must never be rendered into the page"
    assert "test-anthropic-key" not in body, "the actual claude key value must never be rendered into the page"


def test_saving_with_secret_fields_blank_does_not_clear_them(authed_client):
    r2 = authed_client.post("/configuracoes", data={
        "PHONE_NUMBER_ID": "new-phone-id-from-ui",
        "WHATSAPP_TOKEN": "",
        "ANTHROPIC_API_KEY": "",
    })
    assert r2.status_code == 302 and "saved=1" in r2.headers["Location"]
    assert send._cfg("PHONE_NUMBER_ID") == "new-phone-id-from-ui"
    assert send._cfg("WHATSAPP_TOKEN") == "test-whatsapp-token", \
        "blank field must not clear the existing token"


def test_saving_a_new_secret_value_actually_overrides_the_env_fallback(authed_client):
    r3 = authed_client.post("/configuracoes", data={
        "PHONE_NUMBER_ID": "new-phone-id-from-ui",
        "WHATSAPP_TOKEN": "brand-new-token-from-ui",
        "ANTHROPIC_API_KEY": "brand-new-claude-key-from-ui",
    })
    assert r3.status_code == 302
    assert send._cfg("WHATSAPP_TOKEN") == "brand-new-token-from-ui"
    new_key = lead_store.get_setting("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    assert new_key == "brand-new-claude-key-from-ui"


def test_qualification_client_uses_the_settings_ui_key_over_the_stale_env_var(authed_client, monkeypatch):
    authed_client.post("/configuracoes", data={
        "PHONE_NUMBER_ID": "x", "WHATSAPP_TOKEN": "", "ANTHROPIC_API_KEY": "brand-new-claude-key-from-ui",
    })

    import anthropic
    seen = {}

    class FakeAnthropic:
        def __init__(self, api_key):
            seen["key"] = api_key

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    qualification._client()
    assert seen["key"] == "brand-new-claude-key-from-ui", seen


def test_panel_nav_renders_the_configuracoes_link(authed_client):
    r4 = authed_client.get("/painel")
    assert "/configuracoes" in r4.get_data(as_text=True)


# ---------- alert-email / SMTP fields: secret vs. plain save semantics ----------

def test_settings_page_shows_plain_fields_but_never_the_smtp_password(authed_client):
    lead_store.set_setting("ALERT_EMAIL_TO", "lucas@example.com")
    lead_store.set_setting("SMTP_USER", "bot@gmail.com")
    lead_store.set_setting("SMTP_PASSWORD", "app-password-123")

    r = authed_client.get("/configuracoes")
    body = r.get_data(as_text=True)
    assert 'value="lucas@example.com"' in body
    assert 'value="bot@gmail.com"' in body
    assert "app-password-123" not in body


def test_blank_smtp_password_kept_but_blank_alert_email_clears_it(authed_client):
    lead_store.set_setting("ALERT_EMAIL_TO", "lucas@example.com")
    lead_store.set_setting("SMTP_USER", "bot@gmail.com")
    lead_store.set_setting("SMTP_PASSWORD", "app-password-123")

    r2 = authed_client.post("/configuracoes", data={
        "PHONE_NUMBER_ID": "x", "WHATSAPP_TOKEN": "", "ANTHROPIC_API_KEY": "",
        "ALERT_EMAIL_TO": "", "SMTP_USER": "bot@gmail.com", "SMTP_PASSWORD": "",
    })
    assert r2.status_code == 302
    assert lead_store.get_setting("SMTP_PASSWORD") == "app-password-123", "blank must not clear the secret"
    assert lead_store.get_setting("ALERT_EMAIL_TO") == "", "blank plain field is allowed to clear"
