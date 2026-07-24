"""
Panel login/session hardening that doesn't require reloading webhook_server:
session cookie attributes, and the constant-time _consteq comparison used by
both the panel login and the Meta webhook verify-token handshake.

(The login-lockout and default-password-generation tests need to reload
webhook_server itself, since PANEL_PASSWORD/PANEL_USER are computed at import
time -- those live in test_webhook_login_hardening.py instead, isolated so a
reload can't leak module state into the rest of this suite.)

Ported from: test_cookie_hardening.py, test_consteq.py.
"""

import webhook_server as w


def test_login_sets_session_cookie_with_hardening_attributes(client):
    resp = client.post("/login", data={"usuario": "lucas", "senha": "test-panel-password", "next": "/painel"})
    assert resp.status_code == 302, resp.status_code

    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "session=" in set_cookie, set_cookie
    assert "HttpOnly" in set_cookie, f"missing HttpOnly: {set_cookie}"
    assert "SameSite=Lax" in set_cookie, f"missing SameSite=Lax: {set_cookie}"
    assert "Secure" in set_cookie, f"missing Secure (default should be on): {set_cookie}"


def test_protected_route_requires_auth(client):
    resp2 = client.get("/painel")
    assert resp2.status_code in (302, 401), resp2.status_code


# ---------- _consteq ----------

def test_consteq_matches_equality_semantics():
    assert w._consteq("abc", "abc") is True
    assert w._consteq("abc", "abd") is False
    assert w._consteq("abc", "ab") is False
    assert w._consteq("", "") is True
    assert w._consteq(None, None) is True
    assert w._consteq(None, "x") is False


def test_login_still_works_end_to_end_via_consteq(client):
    resp = client.post("/login", data={"usuario": "lucas", "senha": "test-panel-password", "next": "/painel"})
    assert resp.status_code == 302, resp.status_code

    resp = client.post("/login", data={"usuario": "lucas", "senha": "wrong", "next": "/painel"})
    assert resp.status_code == 401, resp.status_code


def test_webhook_verify_handshake_via_consteq(client):
    resp = client.get(
        "/webhook", query_string={"hub.mode": "subscribe", "hub.verify_token": "test-verify-token",
                                   "hub.challenge": "echo123"})
    assert resp.status_code == 200 and resp.get_data(as_text=True) == "echo123", \
        (resp.status_code, resp.get_data(as_text=True))

    resp = client.get(
        "/webhook", query_string={"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                   "hub.challenge": "echo123"})
    assert resp.status_code == 403, resp.status_code
