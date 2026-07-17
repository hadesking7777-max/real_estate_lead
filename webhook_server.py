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


FUNNEL_STAGES = [
    ("contatado", "Contatados"),
    ("respondeu", "Responderam"),
    ("qualificando", "Em qualificacao"),
    ("quente", "Quentes"),
]


def _fmt_pct(n, d):
    return f"{(n / d * 100):.0f}%" if d else "0%"


def _render_panel_html():
    counts = lead_store.funnel_counts()
    hot = lead_store.hot_leads()
    total = counts.get("total", 0)

    kpi_tiles = f'<div class="tile"><div class="tile-num">{total}</div><div class="tile-label">Total na base</div></div>'
    for key, label in FUNNEL_STAGES:
        c = counts.get(key, 0)
        is_quente = key == "quente"
        cls = "tile tile-good" if is_quente else "tile"
        kpi_tiles += f'<div class="{cls}"><div class="tile-num">{c}</div><div class="tile-label">{label}</div></div>'

    funnel_rows = ""
    prev_count = total
    for i, (key, label) in enumerate(FUNNEL_STAGES):
        c = counts.get(key, 0)
        width_pct = (c / total * 100) if total else 0
        # ordinal ramp floor (light mode): no lighter than step 250 (#86b6ef)
        ramp_steps = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab"]
        color = ramp_steps[min(i, len(ramp_steps) - 1)]
        conv = f' &middot; {_fmt_pct(c, prev_count)} da etapa anterior' if i > 0 else ""
        funnel_rows += f"""
        <div class="funnel-row">
          <div class="funnel-label">{label}</div>
          <div class="funnel-track">
            <div class="funnel-bar" style="width:{max(width_pct, 2):.1f}%; background:{color}"></div>
          </div>
          <div class="funnel-value">{c} <span class="funnel-pct">({_fmt_pct(c, total)}{conv})</span></div>
        </div>
        """
        prev_count = c

    secondary_tiles = "".join(
        f'<div class="tile tile-{cls}"><div class="tile-num">{counts.get(stage, 0)}</div>'
        f'<div class="tile-label">{STAGE_LABELS[stage]}</div></div>'
        for stage, cls in [("morno", "warning"), ("frio", "muted"), ("opt_out", "muted")]
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
            <span class="badge-row">
              <span class="badge">{lead['perfil']}</span>
              <span class="status-chip status-good">&#9679; QUENTE</span>
            </span>
          </div>
          <div class="phone">{lead['phone']}</div>
          <div class="signals">
            <div><span class="signal-label">Objetivo</span>{s['objetivo'] or '-'}</div>
            <div><span class="signal-label">Experiencia</span>{s['experiencia'] or '-'}</div>
            <div><span class="signal-label">Forma de pagamento</span>{s['forma_pagamento'] or '-'}</div>
            <div><span class="signal-label">Unidades</span>{s['quantidade_unidades'] or '-'}</div>
            <div><span class="signal-label">Timing</span>{s['timing'] or '-'}</div>
          </div>
          {f'<div class="last-msg">&ldquo;{ultima}&rdquo;</div>' if ultima else ''}
        </div>
        """

    if not hot:
        cards = '<div class="empty-state">Nenhum lead quente ainda. Assim que um investidor esquentar, aparece aqui.</div>'

    return f"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Painel Guerra Cyrela</title>
<style>
  :root {{
    color-scheme: light;
    --surface-1:    #fcfcfb;
    --page:         #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:   #898781;
    --border:       rgba(11,11,11,0.10);
    --status-good:  #0ca30c;
    --status-good-bg: #e6f6e6;
    --status-warning: #fab219;
    --status-warning-bg: #fff6e0;
    --header-bg:    #0d366b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) {{
      color-scheme: dark;
      --surface-1:    #1a1a19;
      --page:         #0d0d0d;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:   #898781;
      --border:       rgba(255,255,255,0.10);
      --status-good:  #0ca30c;
      --status-good-bg: rgba(12,163,12,0.15);
      --status-warning: #fab219;
      --status-warning-bg: rgba(250,178,25,0.15);
      --header-bg:    #184f95;
    }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --surface-1:    #1a1a19;
    --page:         #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:   #898781;
    --border:       rgba(255,255,255,0.10);
    --status-good:  #0ca30c;
    --status-good-bg: rgba(12,163,12,0.15);
    --status-warning: #fab219;
    --status-warning-bg: rgba(250,178,25,0.15);
    --header-bg:    #184f95;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0;
         background: var(--page); color: var(--text-primary); }}
  header {{ background: var(--header-bg); color: white; padding: 20px 24px; }}
  header h1 {{ margin: 0; font-size: 19px; font-weight: 600; }}
  header p {{ margin: 4px 0 0; font-size: 13px; opacity: 0.85; }}
  main {{ padding: 20px; max-width: 920px; margin: 0 auto; }}
  section {{ margin-bottom: 28px; }}
  h2 {{ font-size: 15px; color: var(--text-secondary); font-weight: 600;
       margin: 0 0 12px; }}

  .tiles {{ display: flex; flex-wrap: wrap; gap: 10px; }}
  .tile {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
           padding: 14px 18px; flex: 1 1 120px; text-align: center; }}
  .tile-num {{ font-size: 26px; font-weight: 600; color: var(--text-primary); }}
  .tile-label {{ font-size: 12px; color: var(--text-secondary); margin-top: 4px; }}
  .tile-good {{ border-color: var(--status-good); }}
  .tile-good .tile-num {{ color: var(--status-good); }}
  .tile-warning .tile-num {{ color: var(--status-warning); }}
  .tile-muted .tile-num {{ color: var(--text-muted); }}

  .funnel {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
             padding: 18px 20px; }}
  .funnel-row {{ display: grid; grid-template-columns: 120px 1fr 140px; align-items: center;
                 gap: 12px; padding: 7px 0; }}
  .funnel-label {{ font-size: 13px; color: var(--text-secondary); }}
  .funnel-track {{ background: var(--page); border-radius: 4px; height: 14px; overflow: hidden; }}
  .funnel-bar {{ height: 100%; border-radius: 4px 0 0 4px; min-width: 4px; }}
  .funnel-value {{ font-size: 13px; color: var(--text-primary); font-weight: 600; text-align: right; }}
  .funnel-pct {{ font-size: 12px; color: var(--text-muted); font-weight: 400; }}

  .card {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
           padding: 16px 20px; margin-bottom: 12px; }}
  .card-head {{ display: flex; justify-content: space-between; align-items: center;
                margin-bottom: 8px; flex-wrap: wrap; gap: 6px; }}
  .name {{ font-weight: 600; font-size: 15px; }}
  .badge-row {{ display: flex; gap: 8px; align-items: center; }}
  .badge {{ background: var(--page); color: var(--text-secondary); font-size: 11px;
            padding: 3px 8px; border-radius: 6px; border: 1px solid var(--border); }}
  .status-chip {{ font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 6px; }}
  .status-good {{ color: var(--status-good); background: var(--status-good-bg); }}
  .phone {{ color: var(--text-muted); font-size: 12px; margin-bottom: 10px; }}
  .signals {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px; font-size: 13px;
              margin-bottom: 10px; }}
  .signal-label {{ display: block; font-size: 11px; color: var(--text-muted); }}
  .last-msg {{ font-size: 13px; color: var(--text-secondary); border-top: 1px solid var(--border);
               padding-top: 8px; font-style: italic; }}
  .empty-state {{ color: var(--text-muted); font-size: 13px; padding: 20px; text-align: center;
                  border: 1px dashed var(--border); border-radius: 10px; }}

  @media (max-width: 520px) {{
    .signals {{ grid-template-columns: 1fr; }}
    .funnel-row {{ grid-template-columns: 90px 1fr; }}
    .funnel-value {{ grid-column: 2; text-align: left; }}
  }}
</style>
</head>
<body>
<header>
  <h1>Guerra Cyrela &middot; Faria Lima</h1>
  <p>Painel do piloto de reativacao</p>
</header>
<main>
  <section>
    <div class="tiles">{kpi_tiles}</div>
  </section>

  <section>
    <h2>Funil</h2>
    <div class="funnel">{funnel_rows}</div>
  </section>

  <section>
    <h2>Outros estados</h2>
    <div class="tiles">{secondary_tiles}</div>
  </section>

  <section>
    <h2>Leads quentes ({len(hot)})</h2>
    {cards}
  </section>
</main>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
