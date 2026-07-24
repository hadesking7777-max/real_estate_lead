"""
The /painel dashboard and the pieces of it exercised end to end through the
Flask test client: CSV export (stage filtering, BOM, formula-injection
neutralization), the autopilot on/off toggle, and the stale-hot-lead badge.

Ported from: test_export.py, test_csv_injection.py, test_autopilot_panel.py,
test_stale_hot.py.
"""

import csv
import io
import time

import lead_store
import webhook_server as w


# ---------------------------------------------------------------------------
# CSV export (test_export.py)
# ---------------------------------------------------------------------------

def _seed_export_leads():
    lead_store.get_or_create_lead("5511900000001", nome="Quente Lead")
    lead_store.update_lead("5511900000001", stage="quente", email="quente@example.com",
                            signals={"objetivo": "renda de locacao", "experiencia": "ja investe",
                                     "forma_pagamento": "", "quantidade_unidades": "", "timing": ""})
    lead_store.add_tag("5511900000001", "PF")

    lead_store.get_or_create_lead("5511900000002", nome="Morno Lead")
    lead_store.update_lead("5511900000002", stage="morno")


def test_export_all_returns_both_leads_with_correct_header_and_bom(authed_client):
    _seed_export_leads()
    r = authed_client.get("/contatos/exportar")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    assert "attachment" in r.headers.get("Content-Disposition", "")
    body = r.get_data(as_text=True)
    assert body.startswith("﻿"), "missing BOM"
    rows = list(csv.reader(io.StringIO(body[1:])))
    assert rows[0][0] == "nome"
    names = {row[0] for row in rows[1:]}
    assert names == {"Quente Lead", "Morno Lead"}, names


def test_export_filtered_by_stage_includes_email_tags_and_signals(authed_client):
    _seed_export_leads()
    r2 = authed_client.get("/contatos/exportar?stage=quente")
    body2 = r2.get_data(as_text=True)
    rows2 = list(csv.reader(io.StringIO(body2[1:])))
    data_rows = rows2[1:]
    assert len(data_rows) == 1 and data_rows[0][0] == "Quente Lead", data_rows
    assert data_rows[0][2] == "quente@example.com"  # email column
    assert "PF" in data_rows[0][8]  # tags column
    assert data_rows[0][9] == "renda de locacao"  # objetivo column


def test_panel_renders_both_export_links(authed_client):
    _seed_export_leads()
    r3 = authed_client.get("/painel")
    panel_body = r3.get_data(as_text=True)
    assert "/contatos/exportar" in panel_body
    assert "/contatos/exportar?stage=quente" in panel_body


# ---------------------------------------------------------------------------
# CSV formula-injection neutralization (test_csv_injection.py)
# ---------------------------------------------------------------------------

def test_csv_safe_neutralizes_every_formula_trigger_leaves_normal_text_alone():
    assert w._csv_safe("=cmd|'/c calc'!A1") == "'=cmd|'/c calc'!A1"
    assert w._csv_safe("+1-2") == "'+1-2"
    assert w._csv_safe("-1") == "'-1"
    assert w._csv_safe("@SUM(1,1)") == "'@SUM(1,1)"
    assert w._csv_safe("\tevil") == "'\tevil"
    assert w._csv_safe("Joao Silva") == "Joao Silva"
    assert w._csv_safe(None) == ""
    assert w._csv_safe(0) == "0"


def test_export_neutralizes_a_malicious_lead_display_name_end_to_end(authed_client):
    lead_store.get_or_create_lead("5511900000001", nome='=HYPERLINK("http://evil.com","clique aqui")')
    lead_store.update_lead("5511900000001", stage="pendente")

    resp = authed_client.get("/contatos/exportar")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "'=HYPERLINK" in body, body
    assert "\n=HYPERLINK" not in body and ",=HYPERLINK" not in body, \
        "raw formula leaked into the CSV unescaped"


# ---------------------------------------------------------------------------
# autopilot toggle on the panel (test_autopilot_panel.py)
# ---------------------------------------------------------------------------

def test_panel_renders_autopilot_toggle_off_by_default(authed_client):
    authed_client.set_cookie("ui_lang", "en")
    r = authed_client.get("/painel")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Ligar piloto automatico" in body or "campanha/auto" in body, "auto toggle not rendered"


def test_post_campanha_auto_toggles_autopilot_on_and_off(authed_client):
    authed_client.set_cookie("ui_lang", "en")

    r2 = authed_client.post("/campanha/auto", data={"ligar": "1", "ajax": "1"})
    assert r2.status_code == 204, r2.status_code
    camp = lead_store.get_campaign()
    assert camp["auto_mode"] is True, camp

    r3 = authed_client.get("/painel")
    body3 = r3.get_data(as_text=True)
    # language-independent check: the hidden field should now flip to "0" (turn off next)
    assert 'name="ligar" value="0"' in body3, "toggle did not flip direction after turning on"
    assert "Turn off autopilot" in body3, "EN translation for the on-state label did not render"

    r4 = authed_client.post("/campanha/auto", data={"ligar": "0", "ajax": "1"})
    assert r4.status_code == 204
    assert lead_store.get_campaign()["auto_mode"] is False


# ---------------------------------------------------------------------------
# stale-hot-lead badge (test_stale_hot.py)
# ---------------------------------------------------------------------------

def _card_start(body, idx):
    plain = body.rfind('<div class="card">', 0, idx)
    stale = body.rfind('<div class="card card-stale">', 0, idx)
    return max(plain, stale), (stale > plain)


def test_panel_flags_a_stale_hot_lead_but_not_a_fresh_one(authed_client):
    now = time.time()
    day = 86400

    # a lead that just went hot an hour ago -> should NOT be flagged stale
    lead_store.get_or_create_lead("5511900000001", nome="Recente Quente")
    lead_store.set_stage("5511900000001", "quente", ts=now - 3600)

    # a lead that has been hot for 5 days -> SHOULD be flagged stale (default threshold 3)
    lead_store.get_or_create_lead("5511900000002", nome="Velho Quente")
    lead_store.set_stage("5511900000002", "quente", ts=now - 5 * day)

    # a lead marked quente directly (no set_stage / no state_log entry at all) --
    # must not crash and must not be flagged stale (no data to judge staleness by)
    lead_store.get_or_create_lead("5511900000003", nome="Sem Log")
    lead_store.update_lead("5511900000003", stage="quente")

    resp = authed_client.get("/painel")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    recent_idx = body.find("Recente Quente")
    assert recent_idx != -1
    recent_card_start, recent_is_stale = _card_start(body, recent_idx)
    assert recent_card_start != -1 and not recent_is_stale
    assert "Quente ha" not in body[recent_card_start:recent_idx + 400]

    old_idx = body.find("Velho Quente")
    assert old_idx != -1
    old_card_start, old_is_stale = _card_start(body, old_idx)
    assert old_card_start != -1 and old_is_stale, "expected the card-stale variant of the opening tag"
    assert "Quente ha 5 dias" in body[old_card_start:old_idx + 400]

    nolog_idx = body.find("Sem Log")
    assert nolog_idx != -1
    nolog_card_start, nolog_is_stale = _card_start(body, nolog_idx)
    assert nolog_card_start != -1 and not nolog_is_stale
