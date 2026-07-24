"""
Inbound WhatsApp webhook message handling (_handle_message): redelivery
idempotency, graceful non-text message handling, recording a failed AI-reply
send instead of silently dropping it, and firing the hot-lead email alert
exactly once on the real transition into 'quente'.

Ported from: test_idempotency_nontext.py (parts 2-4), test_hardening_2.py
(part 8), test_hot_alert.py (part 4).
"""

import lead_store
import webhook_server as w
import qualification
import send
import alerts


def test_redelivered_webhook_event_is_only_processed_once(monkeypatch):
    qual_calls = []

    def fake_process_incoming(lead, text):
        qual_calls.append(text)
        return "resposta", {"stage": "qualificando"}

    monkeypatch.setattr(qualification, "process_incoming", fake_process_incoming)
    monkeypatch.setattr(send, "send_text", lambda phone, text: (200, "{}"))

    phone = "5511900000001"
    msg = {"id": "wamid.SAME1", "from": phone, "type": "text", "text": {"body": "oi"}}
    value = {"contacts": [{"profile": {"name": "Fulano"}}], "messages": [msg]}

    w._handle_message(value, msg)
    w._handle_message(value, msg)  # redelivery of the exact same event
    assert qual_calls == ["oi"], qual_calls  # only processed once


def test_non_text_message_is_logged_and_gets_a_polite_reply_no_ai_call(monkeypatch):
    qual_calls = []
    monkeypatch.setattr(qualification, "process_incoming",
                         lambda lead, text: qual_calls.append(text) or ("resposta", {}))
    sent = []
    monkeypatch.setattr(send, "send_text", lambda phone, text: sent.append((phone, text)) or (200, "{}"))

    phone2 = "5511900000002"
    img_msg = {"id": "wamid.IMG1", "from": phone2, "type": "image", "image": {"id": "media123"}}
    value2 = {"contacts": [{"profile": {"name": "Beltrano"}}], "messages": [img_msg]}
    w._handle_message(value2, img_msg)

    assert qual_calls == [], "non-text message must not call the AI qualification engine"
    assert len(sent) == 1 and sent[0][0] == phone2
    assert "texto" in sent[0][1].lower()
    lead2 = lead_store.get_lead(phone2)
    assert lead2 is not None
    history_texts = [h["text"] for h in lead2["history"]]
    assert any("imagem" in t for t in history_texts), history_texts


def test_a_second_different_image_event_still_processes_normally(monkeypatch):
    monkeypatch.setattr(qualification, "process_incoming", lambda lead, text: ("resposta", {}))
    sent = []
    monkeypatch.setattr(send, "send_text", lambda phone, text: sent.append((phone, text)) or (200, "{}"))

    phone2 = "5511900000002"
    img_msg = {"id": "wamid.IMG1", "from": phone2, "type": "image", "image": {"id": "media123"}}
    value2 = {"contacts": [{"profile": {"name": "Beltrano"}}], "messages": [img_msg]}
    w._handle_message(value2, img_msg)

    img_msg2 = {"id": "wamid.IMG2", "from": phone2, "type": "image", "image": {"id": "media456"}}
    w._handle_message(value2, img_msg2)
    assert len(sent) == 2, "a genuinely new event (different id) must still process normally"


def test_failed_ai_reply_send_is_recorded_not_silent(monkeypatch):
    monkeypatch.setattr(qualification, "process_incoming",
                         lambda lead, text: ("resposta", {"stage": "qualificando"}))
    monkeypatch.setattr(send, "send_text",
                         lambda phone, text: (400, '{"error":{"code":131049,"message":"blocked"}}'))

    phone = "5511900000001"
    lead_store.get_or_create_lead(phone, nome="Falha")
    msg = {"id": "wamid.F1", "from": phone, "type": "text", "text": {"body": "oi"}}
    value = {"contacts": [{"profile": {"name": "Falha"}}], "messages": [msg]}
    w._handle_message(value, msg)

    lead = lead_store.get_lead(phone)
    assert lead["delivery"] == "falhou", lead["delivery"]
    assert "131049" in (lead["last_error"] or ""), lead["last_error"]


def test_failed_reply_to_non_text_message_is_also_recorded(monkeypatch):
    monkeypatch.setattr(send, "send_text",
                         lambda phone, text: (400, '{"error":{"code":131049,"message":"blocked"}}'))

    phone2 = "5511900000002"
    lead_store.get_or_create_lead(phone2, nome="Falha2")
    img_msg = {"id": "wamid.F2", "from": phone2, "type": "image", "image": {"id": "m1"}}
    value2 = {"contacts": [{"profile": {"name": "Falha2"}}], "messages": [img_msg]}
    w._handle_message(value2, img_msg)

    lead2 = lead_store.get_lead(phone2)
    assert lead2["delivery"] == "falhou"
    assert "131049" in (lead2["last_error"] or "")


def test_hot_lead_alert_fires_exactly_once_on_the_real_transition(monkeypatch):
    fired = []
    monkeypatch.setattr(alerts, "send_hot_lead_alert", lambda lead: fired.append(lead["phone"]) or True)
    monkeypatch.setattr(qualification, "process_incoming",
                         lambda lead, text: ("resposta da IA", {"stage": "quente"}))
    monkeypatch.setattr(send, "send_text", lambda phone, text: (200, "{}"))

    phone = "5511900000099"
    lead_store.get_or_create_lead(phone, nome="Novo Lead")
    lead_store.update_lead(phone, stage="qualificando")  # not yet quente

    value = {"contacts": [{"profile": {"name": "Novo Lead"}}],
             "messages": [{"from": phone, "type": "text", "text": {"body": "quero avancar"}}]}
    w._handle_message(value, value["messages"][0])
    assert fired == [phone], fired

    # second message while already quente -- must NOT fire again
    w._handle_message(value, value["messages"][0])
    assert fired == [phone], fired
