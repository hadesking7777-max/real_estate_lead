"""
Webhook server: receives WhatsApp Cloud API events, runs the qualification
engine, sends the reply, and persists lead state. Also serves the client-
facing panel at /painel (the web dashboard promised to Lucas), protected by
a login prompt.

Usage:
    $env:VERIFY_TOKEN = "escolha-uma-string-qualquer"
    $env:PHONE_NUMBER_ID = "1078739001993229"
    $env:WHATSAPP_TOKEN = "EAA..."
    $env:ANTHROPIC_API_KEY = "sk-ant-..."
    $env:PANEL_USER = "lucas"
    $env:PANEL_PASSWORD = "escolha-uma-senha"
    python webhook_server.py

Then expose this publicly (ngrok, a small VPS, etc.) and subscribe the URL
+ VERIFY_TOKEN in Meta's App Dashboard > WhatsApp > Configuration > Webhook.
Give Lucas the same public URL + /painel, plus PANEL_USER/PANEL_PASSWORD.
"""

import os
from functools import wraps

from flask import Flask, request, jsonify, Response

import lead_store
import qualification
import send

app = Flask(__name__)
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "changeme")
PANEL_USER = os.environ.get("PANEL_USER", "lucas")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "changeme")

STAGE_LABELS = {
    "contatado": "Contatados",
    "respondeu": "Responderam",
    "qualificando": "Em Qualificacao",
    "quente": "Quentes",
    "morno": "Morno",
    "frio": "Frio",
    "opt_out": "Opt-out",
}


def _requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != PANEL_USER or auth.password != PANEL_PASSWORD:
            return Response(
                "Login necessario", 401, {"WWW-Authenticate": 'Basic realm="Painel"'}
            )
        return f(*args, **kwargs)

    return wrapper


@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def receive():
    payload = request.get_json(force=True, silent=True) or {}
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                _handle_message(value, message)
    return jsonify({"status": "ok"}), 200


def _handle_message(value, message):
    if message.get("type") != "text":
        return

    phone = message["from"]
    text = message["text"]["body"]

    contacts = value.get("contacts", [])
    nome = contacts[0]["profile"]["name"] if contacts else None

    lead = lead_store.get_or_create_lead(phone, nome=nome)
    lead_store.append_history(phone, "lead", text)

    reply_text, updates = qualification.process_incoming(lead, text)

    lead_store.update_lead(phone, **updates)
    lead_store.append_history(phone, "bot", reply_text)

    status, resp_text = send.send_text(phone, reply_text)
    if status not in (200, 201):
        app.logger.error("Send failed for %s: %s %s", phone, status, resp_text)


@app.route("/painel")
@_requires_auth
def painel():
    return _render_panel_html()


def _render_panel_html():
    counts = lead_store.funnel_counts()
    hot = lead_store.hot_leads()

    total_tile = (
        f'<div class="tile"><div class="tile-num">{counts.get("total", 0)}</div>'
        f'<div class="tile-label">Total na base</div></div>'
    )
    stage_tiles = "".join(
        f'<div class="tile"><div class="tile-num">{counts.get(stage, 0)}</div>'
        f'<div class="tile-label">{STAGE_LABELS[stage]}</div></div>'
        for stage in ["contatado", "respondeu", "qualificando", "quente"]
    )

    cards = ""
    for lead in hot:
        s = lead["signals"]
        lead_messages = [h for h in lead["history"] if h["role"] == "lead"]
        ultima = lead_messages[-1]["text"] if lead_messages else ""
        cards += f"""
        <div class="card">
          <div class="card-head">
            <span class="name">{lead['nome'] or '(sem nome)'}</span>
            <span class="badge">{lead['perfil']}</span>
          </div>
          <div class="phone">{lead['phone']}</div>
          <div class="signals">
            <div><b>Objetivo:</b> {s['objetivo'] or '-'}</div>
            <div><b>Experiencia:</b> {s['experiencia'] or '-'}</div>
            <div><b>Forma de pagamento:</b> {s['forma_pagamento'] or '-'}</div>
            <div><b>Unidades:</b> {s['quantidade_unidades'] or '-'}</div>
            <div><b>Timing:</b> {s['timing'] or '-'}</div>
          </div>
          <div class="last-msg">{ultima}</div>
        </div>
        """

    if not hot:
        cards = '<p class="empty">Nenhum lead quente ainda.</p>'

    return f"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Painel Guerra Cyrela</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0;
         background: #f5f6f8; color: #1a1a1a; }}
  header {{ background: #1f2b3a; color: white; padding: 20px 24px; }}
  header h1 {{ margin: 0; font-size: 20px; }}
  main {{ padding: 20px; max-width: 900px; margin: 0 auto; }}
  .tiles {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 28px; }}
  .tile {{ background: white; border-radius: 10px; padding: 16px 20px;
           box-shadow: 0 1px 3px rgba(0,0,0,0.08); flex: 1 1 120px; text-align: center; }}
  .tile-num {{ font-size: 28px; font-weight: 700; color: #1f2b3a; }}
  .tile-label {{ font-size: 13px; color: #666; margin-top: 4px; }}
  h2 {{ font-size: 16px; color: #333; }}
  .card {{ background: white; border-radius: 10px; padding: 16px 20px; margin-bottom: 14px;
           box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .card-head {{ display: flex; justify-content: space-between; align-items: center;
                margin-bottom: 8px; }}
  .name {{ font-weight: 700; font-size: 16px; }}
  .badge {{ background: #e8f0fe; color: #1a56db; font-size: 12px; padding: 3px 8px;
            border-radius: 6px; }}
  .phone {{ color: #666; font-size: 13px; margin-bottom: 10px; }}
  .signals {{ display: grid; grid-template-columns: 1fr 1fr; gap: 4px; font-size: 13px;
              margin-bottom: 10px; }}
  .last-msg {{ font-size: 13px; color: #444; border-top: 1px solid #eee; padding-top: 8px; }}
  .empty {{ color: #888; }}
  @media (max-width: 480px) {{ .signals {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<header><h1>Guerra Cyrela, Faria Lima, painel do piloto</h1></header>
<main>
  <div class="tiles">{total_tile}{stage_tiles}</div>
  <h2>Leads quentes ({len(hot)})</h2>
  {cards}
</main>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
