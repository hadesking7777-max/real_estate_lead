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

import html
import os
import uuid
from functools import wraps

from flask import Flask, request, jsonify, Response

import lead_store
import qualification
import send
import base_import

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB cap on uploads
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "changeme")
PANEL_USER = os.environ.get("PANEL_USER", "lucas")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "changeme")

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

STAGE_LABELS = {
    "pendente": "Pendentes",
    "contatado": "Contatados",
    "respondeu": "Responderam",
    "qualificando": "Em Qualificacao",
    "quente": "Quentes",
    "morno": "Morno",
    "frio": "Frio",
    "opt_out": "Opt-out",
}

# WhatsApp status event -> our delivery state
_WA_STATUS_MAP = {
    "sent": "enviado",
    "delivered": "entregue",
    "read": "lido",
    "failed": "falhou",
}

DELIVERY_LABELS = {
    "pendente": "Pendente", "enviado": "Enviado", "entregue": "Entregue",
    "lido": "Lido", "respondeu": "Respondeu", "falhou": "Falhou",
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
            for status in value.get("statuses", []):
                _handle_status(status)
    return jsonify({"status": "ok"}), 200


def _handle_status(status):
    """Delivery receipt: sent/delivered/read/failed, keyed by recipient_id."""
    phone = status.get("recipient_id")
    wa_state = status.get("status")
    mapped = _WA_STATUS_MAP.get(wa_state)
    if phone and mapped:
        lead_store.advance_delivery(phone, mapped)


def _handle_message(value, message):
    if message.get("type") != "text":
        return

    phone = message["from"]
    text = message["text"]["body"]

    contacts = value.get("contacts", [])
    nome = contacts[0]["profile"]["name"] if contacts else None

    lead = lead_store.get_or_create_lead(phone, nome=nome)
    lead_store.append_history(phone, "lead", text)
    lead_store.advance_delivery(phone, "respondeu")

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


@app.route("/importar", methods=["GET"])
@_requires_auth
def importar():
    return _render_import_form()


@app.route("/importar/analisar", methods=["POST"])
@_requires_auth
def importar_analisar():
    file = request.files.get("arquivo")
    if not file or not file.filename.lower().endswith(".xlsx"):
        return _render_import_form(erro="Envie um arquivo .xlsx valido."), 400
    token = uuid.uuid4().hex
    path = os.path.join(UPLOAD_DIR, f"{token}.xlsx")
    file.save(path)
    try:
        analysis = base_import.analyze(path)
    except Exception as exc:  # noqa: BLE001 - surface any parse error to the user
        os.remove(path)
        return _render_import_form(erro=f"Nao consegui ler a planilha: {exc}"), 400
    return _render_import_review(token, analysis)


@app.route("/importar/confirmar", methods=["POST"])
@_requires_auth
def importar_confirmar():
    token = request.form.get("token", "")
    include_intl = request.form.get("incluir_intl") == "sim"
    path = os.path.join(UPLOAD_DIR, os.path.basename(token) + ".xlsx")
    if not (token and os.path.exists(path)):
        return _render_import_form(erro="Sessao de upload expirou, envie a planilha de novo."), 400
    analysis = base_import.analyze(path)
    contacts = list(analysis["clean"])
    if include_intl:
        contacts += analysis["internacionais"]
    imported, skipped = lead_store.import_contacts(contacts)
    os.remove(path)
    return _render_import_done(imported, skipped, include_intl,
                               len(analysis["internacionais"]))


FUNNEL_STAGES = [
    ("contatado", "Contatados"),
    ("respondeu", "Responderam"),
    ("qualificando", "Em qualificacao"),
    ("quente", "Quentes"),
]


def _fmt_pct(n, d):
    return f"{(n / d * 100):.0f}%" if d else "0%"


def _e(x):
    return html.escape(str(x if x is not None else ""))


_SEARCH_JS = """
<script>
(function () {
  var PAGE_SIZE = 25;
  var page = 1;
  var query = '';

  function allRows() {
    return Array.prototype.slice.call(document.querySelectorAll('#contacts-body tr'));
  }
  function matches(r) {
    var hay = r.getAttribute('data-search') || '';
    return !query || hay.indexOf(query) !== -1;
  }
  function render() {
    var rows = allRows();
    var visible = rows.filter(matches);
    var totalPages = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
    if (page > totalPages) page = totalPages;
    if (page < 1) page = 1;
    var start = (page - 1) * PAGE_SIZE;
    var end = start + PAGE_SIZE;
    var shown = 0;
    rows.forEach(function (r) { r.style.display = 'none'; });
    visible.forEach(function (r, i) {
      if (i >= start && i < end) { r.style.display = ''; shown++; }
    });
    var info = document.getElementById('pager-info');
    if (info) info.textContent = 'Pagina ' + page + ' de ' + totalPages + ' \\u00b7 ' + visible.length + ' contatos';
    var prev = document.getElementById('pager-prev');
    var next = document.getElementById('pager-next');
    if (prev) prev.disabled = (page <= 1);
    if (next) next.disabled = (page >= totalPages);
  }
  window.filterRows = function (q) { query = (q || '').toLowerCase().trim(); page = 1; render(); };
  window.pagerPrev = function () { if (page > 1) { page--; render(); } };
  window.pagerNext = function () { page++; render(); };
  render();
})();
</script>
"""

_IMPORT_JS = """
<script>
(function () {
  var input = document.getElementById('file-input');
  var dz = document.getElementById('dropzone');
  var card = document.getElementById('file-card');
  var nameEl = document.getElementById('file-name');
  var sizeEl = document.getElementById('file-size');
  var errEl = document.getElementById('file-error');
  var submit = document.getElementById('submit-btn');
  var form = document.getElementById('import-form');
  if (!input) return;

  function fmtSize(b) {
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    return (b / 1048576).toFixed(1) + ' MB';
  }
  function showError(msg) {
    errEl.textContent = msg; errEl.hidden = false;
    card.hidden = true; submit.disabled = true;
  }
  function showFile(f) {
    errEl.hidden = true;
    nameEl.textContent = f.name;
    sizeEl.textContent = fmtSize(f.size);
    card.hidden = false;
    submit.disabled = false;
  }
  function handle(files) {
    if (!files || !files.length) return;
    var f = files[0];
    if (!f.name.toLowerCase().endsWith('.xlsx')) {
      showError('Esse arquivo nao e .xlsx. Envie uma planilha do Excel.');
      input.value = '';
      return;
    }
    showFile(f);
  }
  window.clearFile = function () {
    input.value = ''; card.hidden = true; submit.disabled = true; errEl.hidden = true;
  };
  input.addEventListener('change', function () { handle(input.files); });
  ['dragenter', 'dragover'].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add('drag'); });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove('drag'); });
  });
  dz.addEventListener('drop', function (e) {
    var dt = e.dataTransfer;
    if (dt && dt.files && dt.files.length) { input.files = dt.files; handle(dt.files); }
  });
  form.addEventListener('submit', function () {
    submit.disabled = true;
    submit.textContent = 'Analisando';
    submit.classList.add('loading');
  });
})();
</script>
"""

_SHARED_CSS = """<style>
  :root {
    color-scheme: light;
    --surface-1: #fcfcfb; --page: #f9f9f7;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #898781;
    --border: rgba(11,11,11,0.10);
    --status-good: #0ca30c; --status-good-bg: #e6f6e6;
    --status-info: #2a78d6; --status-info-bg: #e6effb;
    --status-warning: #fab219;
    --status-bad: #d03b3b; --status-bad-bg: #fbe9e9;
    --accent: #2a78d6; --accent-ink: #ffffff;
    --header-bg: #0d366b; --nav-bg: #fcfcfb;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --surface-1: #1a1a19; --page: #0d0d0d;
      --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
      --border: rgba(255,255,255,0.10);
      --status-good: #0ca30c; --status-good-bg: rgba(12,163,12,0.15);
      --status-info: #3987e5; --status-info-bg: rgba(57,135,229,0.15);
      --status-warning: #fab219;
      --status-bad: #e66767; --status-bad-bg: rgba(208,59,59,0.18);
      --accent: #3987e5; --accent-ink: #ffffff;
      --header-bg: #184f95; --nav-bg: #1a1a19;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface-1: #1a1a19; --page: #0d0d0d;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
    --border: rgba(255,255,255,0.10);
    --status-good: #0ca30c; --status-good-bg: rgba(12,163,12,0.15);
    --status-info: #3987e5; --status-info-bg: rgba(57,135,229,0.15);
    --status-warning: #fab219;
    --status-bad: #e66767; --status-bad-bg: rgba(208,59,59,0.18);
    --accent: #3987e5; --accent-ink: #ffffff;
    --header-bg: #184f95; --nav-bg: #1a1a19;
  }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0;
         background: var(--page); color: var(--text-primary); }
  header { background: var(--header-bg); color: white; padding: 20px 24px; }
  header h1 { margin: 0; font-size: 19px; font-weight: 600; }
  header p { margin: 4px 0 0; font-size: 13px; opacity: 0.85; }
  .nav { display: flex; gap: 4px; background: var(--nav-bg); border-bottom: 1px solid var(--border);
         padding: 0 20px; position: sticky; top: 0; z-index: 10; }
  .nav-item { padding: 12px 14px; font-size: 13px; color: var(--text-secondary);
              text-decoration: none; border-bottom: 2px solid transparent; }
  .nav-item.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
  main { padding: 20px; max-width: 960px; margin: 0 auto; }
  section { margin-bottom: 28px; }
  h2 { font-size: 15px; color: var(--text-secondary); font-weight: 600; margin: 0 0 12px; }

  .tiles { display: flex; flex-wrap: wrap; gap: 10px; }
  .tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
          padding: 14px 18px; flex: 1 1 110px; text-align: center; }
  .tile-num { font-size: 26px; font-weight: 600; color: var(--text-primary); }
  .tile-label { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
  .tile-good { border-color: var(--status-good); }
  .tile-good .tile-num { color: var(--status-good); }
  .tile-warning .tile-num { color: var(--status-warning); }
  .tile-muted .tile-num { color: var(--text-muted); }

  .funnel { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; }
  .funnel-row { display: grid; grid-template-columns: 120px 1fr 150px; align-items: center; gap: 12px; padding: 7px 0; }
  .funnel-label { font-size: 13px; color: var(--text-secondary); }
  .funnel-track { background: var(--page); border-radius: 4px; height: 14px; overflow: hidden; }
  .funnel-bar { height: 100%; border-radius: 4px 0 0 4px; min-width: 4px; }
  .funnel-value { font-size: 13px; color: var(--text-primary); font-weight: 600; text-align: right; }
  .funnel-pct { font-size: 12px; color: var(--text-muted); font-weight: 400; }

  .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 16px 20px; margin-bottom: 12px; }
  .card-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 6px; }
  .name { font-weight: 600; font-size: 15px; }
  .badge-row { display: flex; gap: 8px; align-items: center; }
  .badge { background: var(--page); color: var(--text-secondary); font-size: 11px; padding: 3px 8px;
           border-radius: 6px; border: 1px solid var(--border); }
  .phone { color: var(--text-muted); font-size: 12px; margin-bottom: 10px; }
  .signals { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px; font-size: 13px; margin-bottom: 10px; }
  .signal-label { display: block; font-size: 11px; color: var(--text-muted); }
  .last-msg { font-size: 13px; color: var(--text-secondary); border-top: 1px solid var(--border);
              padding-top: 8px; font-style: italic; }

  .chip { font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 6px; white-space: nowrap; }
  .chip-good { color: var(--status-good); background: var(--status-good-bg); }
  .chip-info { color: var(--status-info); background: var(--status-info-bg); }
  .chip-bad { color: var(--status-bad); background: var(--status-bad-bg); }
  .chip-muted { color: var(--text-muted); background: var(--page); }

  .search { width: 100%; padding: 10px 12px; margin-bottom: 12px; border-radius: 8px;
            border: 1px solid var(--border); background: var(--surface-1); color: var(--text-primary); font-size: 14px; }
  .table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; background: var(--surface-1); }
  th { text-align: left; padding: 10px 12px; color: var(--text-muted); font-weight: 600;
       border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); color: var(--text-primary); }
  tr:last-child td { border-bottom: none; }
  td.num { font-variant-numeric: tabular-nums; color: var(--text-secondary); }

  .panel-box { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
  .muted-text { color: var(--text-secondary); font-size: 13px; line-height: 1.5; }
  .file-input { display: block; margin: 14px 0; font-size: 14px; color: var(--text-primary); }
  .checkbox { display: block; margin: 14px 0; font-size: 13px; color: var(--text-secondary); }
  .btn { display: inline-block; padding: 10px 18px; border-radius: 8px; font-size: 14px; font-weight: 600;
         border: 1px solid var(--border); cursor: pointer; text-decoration: none; margin-right: 8px; }
  .btn-primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
  .btn-ghost { background: transparent; color: var(--text-secondary); }
  .alert { padding: 12px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 14px; }
  .alert-good { color: var(--status-good); background: var(--status-good-bg); }
  .alert-bad { color: var(--status-bad); background: var(--status-bad-bg); }
  .empty-state { color: var(--text-muted); font-size: 13px; padding: 20px; text-align: center;
                 border: 1px dashed var(--border); border-radius: 10px; }
  .pager { display: flex; align-items: center; justify-content: center; gap: 14px; margin-top: 14px; }
  .pager-info { font-size: 13px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
  .pager .btn { margin: 0; }
  button.btn:disabled { opacity: 0.4; cursor: default; }

  .dropzone { border: 2px dashed var(--border); border-radius: 12px; padding: 34px 20px; text-align: center;
              cursor: pointer; transition: border-color .15s, background .15s; background: var(--page);
              color: var(--text-muted); }
  .dropzone:hover, .dropzone.drag { border-color: var(--accent); background: var(--status-info-bg); color: var(--accent); }
  .dz-icon { line-height: 1; margin-bottom: 10px; }
  .dz-title { font-size: 14px; color: var(--text-primary); font-weight: 600; }
  .dz-hint { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
  .file-card { display: flex; align-items: center; gap: 12px; margin-top: 14px; padding: 12px 14px;
               border: 1px solid var(--status-good); background: var(--status-good-bg); border-radius: 10px; }
  .file-check { color: var(--status-good); font-weight: 700; font-size: 16px; }
  .file-meta { flex: 1; min-width: 0; }
  .file-name { font-size: 13px; font-weight: 600; color: var(--text-primary);
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-size { font-size: 12px; color: var(--text-muted); }
  .file-remove { background: transparent; border: none; color: var(--text-muted); font-size: 20px;
                 cursor: pointer; line-height: 1; padding: 0 4px; }
  .btn.loading::after { content: ''; display: inline-block; width: 12px; height: 12px; margin-left: 8px;
    border: 2px solid rgba(255,255,255,0.5); border-top-color: #fff; border-radius: 50%;
    vertical-align: middle; animation: spin .7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 520px) {
    .signals { grid-template-columns: 1fr; }
    .funnel-row { grid-template-columns: 90px 1fr; }
    .funnel-value { grid-column: 2; text-align: left; }
  }
</style>"""


_DELIVERY_CHIP = {
    "pendente": "chip-muted", "enviado": "chip-info", "entregue": "chip-info",
    "lido": "chip-good", "respondeu": "chip-good", "falhou": "chip-bad",
}


def _delivery_chip(state):
    cls = _DELIVERY_CHIP.get(state, "chip-muted")
    return f'<span class="chip {cls}">{DELIVERY_LABELS.get(state, state)}</span>'


def _nav(active):
    def item(href, label, key):
        cls = "nav-item active" if key == active else "nav-item"
        return f'<a class="{cls}" href="{href}">{label}</a>'
    return ('<nav class="nav">'
            + item("/painel", "Funil e contatos", "painel")
            + item("/importar", "Importar base", "importar")
            + "</nav>")


def _page(title, subtitle, active, body):
    return (
        '<!doctype html><html lang="pt-br"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)}</title>"
        + _SHARED_CSS
        + "</head><body>"
        f'<header><h1>Guerra Cyrela &middot; Faria Lima</h1><p>{_e(subtitle)}</p></header>'
        + _nav(active)
        + f"<main>{body}</main>"
        + "</body></html>"
    )


def _render_panel_html():
    counts = lead_store.funnel_counts()
    deliv = lead_store.delivery_counts()
    hot = lead_store.hot_leads()
    leads = sorted(lead_store.all_leads(), key=lambda l: l.get("nome") or "")
    total = counts.get("total", 0)

    kpi_tiles = f'<div class="tile"><div class="tile-num">{total}</div><div class="tile-label">Total na base</div></div>'
    for key, label in FUNNEL_STAGES:
        cls = "tile tile-good" if key == "quente" else "tile"
        kpi_tiles += f'<div class="{cls}"><div class="tile-num">{counts.get(key, 0)}</div><div class="tile-label">{label}</div></div>'

    funnel_rows = ""
    prev_count = total
    ramp_steps = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab"]
    for i, (key, label) in enumerate(FUNNEL_STAGES):
        c = counts.get(key, 0)
        width_pct = (c / total * 100) if total else 0
        color = ramp_steps[min(i, len(ramp_steps) - 1)]
        conv = f" &middot; {_fmt_pct(c, prev_count)} da etapa anterior" if i > 0 else ""
        funnel_rows += f"""
        <div class="funnel-row">
          <div class="funnel-label">{label}</div>
          <div class="funnel-track"><div class="funnel-bar" style="width:{max(width_pct, 2):.1f}%; background:{color}"></div></div>
          <div class="funnel-value">{c} <span class="funnel-pct">({_fmt_pct(c, total)}{conv})</span></div>
        </div>"""
        prev_count = c

    deliv_tiles = "".join(
        f'<div class="tile"><div class="tile-num">{deliv.get(k, 0)}</div>'
        f'<div class="tile-label">{DELIVERY_LABELS[k]}</div></div>'
        for k in ["pendente", "enviado", "entregue", "lido", "respondeu", "falhou"]
    )

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
            <span class="name">{_e(lead['nome'] or '(sem nome)')}</span>
            <span class="badge-row">
              <span class="badge">{_e(lead['perfil'])}</span>
              <span class="chip chip-good">&#9679; QUENTE</span>
            </span>
          </div>
          <div class="phone">{_e(lead['phone'])}</div>
          <div class="signals">
            <div><span class="signal-label">Objetivo</span>{_e(s['objetivo'] or '-')}</div>
            <div><span class="signal-label">Experiencia</span>{_e(s['experiencia'] or '-')}</div>
            <div><span class="signal-label">Forma de pagamento</span>{_e(s['forma_pagamento'] or '-')}</div>
            <div><span class="signal-label">Unidades</span>{_e(s['quantidade_unidades'] or '-')}</div>
            <div><span class="signal-label">Timing</span>{_e(s['timing'] or '-')}</div>
          </div>
          {f'<div class="last-msg">&ldquo;{_e(ultima)}&rdquo;</div>' if ultima else ''}
        </div>"""
    if not hot:
        cards = '<div class="empty-state">Nenhum lead quente ainda. Assim que um investidor esquentar, aparece aqui.</div>'

    table_rows = ""
    for lead in leads:
        haystack = _e(f"{lead.get('nome','')} {lead.get('phone','')} {lead.get('pais','')}").lower()
        table_rows += f"""
        <tr data-search="{haystack}">
          <td>{_e(lead.get('nome') or '(sem nome)')}</td>
          <td class="num">{_e(lead.get('phone'))}</td>
          <td>{_e(lead.get('perfil'))}</td>
          <td>{_e(lead.get('pais') or lead.get('origem') or '-')}</td>
          <td>{_e(STAGE_LABELS.get(lead.get('stage'), lead.get('stage')))}</td>
          <td>{_delivery_chip(lead.get('delivery', 'pendente'))}</td>
        </tr>"""
    if not leads:
        table_section = '<div class="empty-state">Nenhum contato ainda. Importe uma planilha para comecar.</div>'
    else:
        table_section = f"""
        <input class="search" type="search" placeholder="Buscar por nome, telefone ou pais..." oninput="filterRows(this.value)">
        <div class="table-wrap"><table>
          <thead><tr><th>Nome</th><th>Telefone</th><th>Perfil</th><th>Origem</th><th>Etapa</th><th>Entrega</th></tr></thead>
          <tbody id="contacts-body">{table_rows}</tbody>
        </table></div>
        <div class="pager">
          <button class="btn btn-ghost" id="pager-prev" onclick="pagerPrev()">Anterior</button>
          <span id="pager-info" class="pager-info"></span>
          <button class="btn btn-ghost" id="pager-next" onclick="pagerNext()">Proximo</button>
        </div>"""

    body = f"""
  <section><div class="tiles">{kpi_tiles}</div></section>
  <section><h2>Funil</h2><div class="funnel">{funnel_rows}</div></section>
  <section><h2>Status de envio</h2><div class="tiles">{deliv_tiles}</div></section>
  <section><h2>Outros estados</h2><div class="tiles">{secondary_tiles}</div></section>
  <section><h2>Leads quentes ({len(hot)})</h2>{cards}</section>
  <section><h2>Contatos ({len(leads)})</h2>{table_section}</section>
  """ + _SEARCH_JS

    return _page("Painel Guerra Cyrela", "Painel do piloto de reativacao", "painel", body)


_UPLOAD_ICON = (
    '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
    '<polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>'
)


def _render_import_form(erro=None):
    alert = f'<div class="alert alert-bad">{_e(erro)}</div>' if erro else ""
    body = f"""
  <section>
    <h2>Importar planilha de contatos</h2>
    {alert}
    <div class="panel-box">
      <p class="muted-text">Envie a planilha (.xlsx) com as colunas nome, telefone e email.
      A gente analisa, remove duplicados e mostra um resumo antes de importar de verdade.</p>
      <form id="import-form" method="post" action="/importar/analisar" enctype="multipart/form-data">
        <input id="file-input" type="file" name="arquivo" accept=".xlsx" hidden>
        <div id="dropzone" class="dropzone" onclick="document.getElementById('file-input').click()">
          <div class="dz-icon">{_UPLOAD_ICON}</div>
          <div class="dz-title">Arraste a planilha aqui ou clique para selecionar</div>
          <div class="dz-hint">Apenas arquivos .xlsx</div>
        </div>
        <div id="file-card" class="file-card" hidden>
          <span class="file-check">&#10003;</span>
          <div class="file-meta">
            <div id="file-name" class="file-name"></div>
            <div id="file-size" class="file-size"></div>
          </div>
          <button type="button" class="file-remove" onclick="clearFile()" title="Remover">&times;</button>
        </div>
        <div id="file-error" class="alert alert-bad" hidden></div>
        <button id="submit-btn" class="btn btn-primary" type="submit" disabled>Analisar planilha</button>
      </form>
    </div>
  </section>""" + _IMPORT_JS
    return _page("Importar base", "Importacao de contatos", "importar", body)


def _render_import_review(token, a):
    n_clean = len(a["clean"])
    n_intl = len(a["internacionais"])
    tiles = "".join(
        f'<div class="tile"><div class="tile-num">{n}</div><div class="tile-label">{lbl}</div></div>'
        for n, lbl in [
            (a["total_rows"], "Linhas na planilha"),
            (n_clean, "Brasil limpos"),
            (n_intl, "Internacionais"),
            (a["removed_duplicates"], "Duplicados removidos"),
            (a["landlines_or_invalid"], "Invalidos/ignorados"),
        ]
    )
    country_rows = "".join(
        f"<tr><td>{_e(pais)}</td><td class='num'>{n}</td></tr>" for pais, n in a["by_country"]
    )
    country_block = (
        f'<div class="table-wrap"><table><thead><tr><th>Pais</th><th>Contatos</th></tr></thead>'
        f"<tbody>{country_rows}</tbody></table></div>" if country_rows else ""
    )
    body = f"""
  <section>
    <h2>Revisao da base</h2>
    <div class="tiles">{tiles}</div>
  </section>
  <section>
    <h2>Confirmar importacao</h2>
    <div class="panel-box">
      <p class="muted-text">Serao importados <b>{n_clean}</b> contatos do Brasil.
      Os {n_intl} internacionais podem entrar junto (investidores de fora que compram em SP).</p>
      {country_block}
      <form method="post" action="/importar/confirmar">
        <input type="hidden" name="token" value="{_e(token)}">
        <label class="checkbox"><input type="checkbox" name="incluir_intl" value="sim" checked> Incluir os {n_intl} contatos internacionais</label>
        <button class="btn btn-primary" type="submit">Confirmar e importar</button>
        <a class="btn btn-ghost" href="/importar">Cancelar</a>
      </form>
    </div>
  </section>"""
    return _page("Revisar importacao", "Importacao de contatos", "importar", body)


def _render_import_done(imported, skipped, include_intl, n_intl):
    intl_note = f" (incluindo {n_intl} internacionais)" if include_intl else ""
    body = f"""
  <section>
    <h2>Importacao concluida</h2>
    <div class="panel-box">
      <div class="alert alert-good">{imported} contatos importados{intl_note}.</div>
      <p class="muted-text">{skipped} ja existiam na base e foram mantidos como estavam, sem sobrescrever conversas em andamento.</p>
      <a class="btn btn-primary" href="/painel">Ver o painel</a>
      <a class="btn btn-ghost" href="/importar">Importar outra planilha</a>
    </div>
  </section>"""
    return _page("Importacao concluida", "Importacao de contatos", "importar", body)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
