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
import run_history

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
    run_history.record(
        "Analise",
        f"{file.filename}: {analysis['total_rows']} linhas, {len(analysis['clean'])} BR, "
        f"{len(analysis['internacionais'])} internacionais, {analysis['removed_duplicates']} duplicados",
    )
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
    run_history.record(
        "Importacao",
        f"{imported} contatos importados, {skipped} ja existiam"
        + (f", incluindo {len(analysis['internacionais'])} internacionais" if include_intl else ""),
    )
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
  var progWrap = document.getElementById('progress-wrap');
  var progBar = document.getElementById('progress-bar');
  var progPct = document.getElementById('progress-pct');
  if (!input) return;

  var selected = null;
  var xhr = null;

  function fmtSize(b) {
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    return (b / 1048576).toFixed(1) + ' MB';
  }
  function reset() {
    if (xhr) { try { xhr.abort(); } catch (e) {} xhr = null; }
    selected = null; input.value = '';
    card.hidden = true; errEl.hidden = true;
    progWrap.hidden = true; progBar.style.width = '0%'; if (progPct) progPct.textContent = '';
    submit.disabled = true; submit.textContent = 'Analisar planilha'; submit.classList.remove('loading');
    dz.hidden = false;
  }
  window.cancelUpload = reset;

  function chooseFile(f) {
    if (!f.name.toLowerCase().endsWith('.xlsx')) {
      errEl.textContent = 'Esse arquivo nao e .xlsx. Envie uma planilha do Excel.';
      errEl.hidden = false; input.value = '';
      return;
    }
    selected = f;
    errEl.hidden = true;
    nameEl.textContent = f.name;
    sizeEl.textContent = fmtSize(f.size);
    card.hidden = false; dz.hidden = true;
    progWrap.hidden = true; progBar.style.width = '0%';
    submit.disabled = false;
  }
  function handle(files) { if (files && files.length) chooseFile(files[0]); }

  input.addEventListener('change', function () { handle(input.files); });
  ['dragenter', 'dragover'].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add('drag'); });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove('drag'); });
  });
  dz.addEventListener('drop', function (e) {
    var dt = e.dataTransfer;
    if (dt && dt.files && dt.files.length) handle(dt.files);
  });

  function failMsg(msg) {
    errEl.textContent = msg; errEl.hidden = false;
    progWrap.hidden = true;
    submit.disabled = false; submit.classList.remove('loading'); submit.textContent = 'Analisar planilha';
    xhr = null;
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (!selected) return;
    var fd = new FormData();
    fd.append('arquivo', selected, selected.name);
    xhr = new XMLHttpRequest();
    xhr.open('POST', '/importar/analisar');
    submit.disabled = true; submit.classList.add('loading'); submit.textContent = 'Enviando';
    progWrap.hidden = false; progBar.style.width = '0%';
    xhr.upload.onprogress = function (ev) {
      if (ev.lengthComputable) {
        var pct = Math.round(ev.loaded / ev.total * 100);
        progBar.style.width = pct + '%';
        if (progPct) progPct.textContent = pct + '%';
        if (pct >= 100) submit.textContent = 'Analisando';
      }
    };
    xhr.onload = function () {
      if (xhr.status === 200) {
        document.open(); document.write(xhr.responseText); document.close();
      } else {
        failMsg('Erro no envio (' + xhr.status + '). Tente de novo.');
      }
    };
    xhr.onerror = function () { failMsg('Falha de conexao no envio. Tente de novo.'); };
    xhr.send(fd);
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
    --shadow: 0 1px 2px rgba(16,24,40,0.04), 0 1px 3px rgba(16,24,40,0.06);
    --shadow-lg: 0 6px 16px rgba(16,24,40,0.10);
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
      --shadow: 0 1px 2px rgba(0,0,0,0.30);
      --shadow-lg: 0 6px 18px rgba(0,0,0,0.45);
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
    --shadow: 0 1px 2px rgba(0,0,0,0.30);
    --shadow-lg: 0 6px 18px rgba(0,0,0,0.45);
  }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  html { scroll-behavior: smooth; }
  body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0;
         background: var(--page); color: var(--text-primary); line-height: 1.45;
         -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }
  header { background-color: var(--header-bg);
           background-image: linear-gradient(135deg, rgba(255,255,255,0.06), rgba(0,0,0,0.14)); color: #fff; }
  .header-inner { max-width: 960px; margin: 0 auto; padding: 18px 20px; display: flex; align-items: center; gap: 14px; }
  .brand { width: 40px; height: 40px; border-radius: 11px; background: rgba(255,255,255,0.14);
           display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 15px;
           letter-spacing: 0.5px; border: 1px solid rgba(255,255,255,0.20); flex-shrink: 0; }
  .header-text h1 { margin: 0; font-size: 18px; font-weight: 600; letter-spacing: -0.2px; }
  .header-text p { margin: 3px 0 0; font-size: 13px; opacity: 0.82; }
  .nav { background: var(--nav-bg); border-bottom: 1px solid var(--border);
         padding: 0 20px; position: sticky; top: 0; z-index: 10; }
  .nav-inner { display: flex; gap: 2px; max-width: 960px; margin: 0 auto; }
  .nav-item { display: inline-flex; align-items: center; gap: 7px; padding: 13px 14px; font-size: 13px;
              color: var(--text-secondary); text-decoration: none; border-bottom: 2px solid transparent;
              transition: color .15s ease, border-color .15s ease; }
  .nav-item:hover { color: var(--text-primary); }
  .nav-item.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
  .nav-ic { display: flex; }
  main { padding: 22px 20px 44px; max-width: 960px; margin: 0 auto; }
  section { margin-bottom: 28px; }
  h2 { font-size: 12px; color: var(--text-muted); font-weight: 700; margin: 0 0 12px;
       text-transform: uppercase; letter-spacing: 0.6px; display: flex; align-items: center; gap: 8px; }
  h2::before { content: ''; width: 3px; height: 13px; background: var(--accent); border-radius: 2px; }

  .tiles { display: flex; flex-wrap: wrap; gap: 10px; }
  .tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
          padding: 16px 18px; flex: 1 1 110px; text-align: center; box-shadow: var(--shadow);
          transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease; }
  .tile:hover { transform: translateY(-2px); box-shadow: var(--shadow-lg); }
  .tile-num { font-size: 27px; font-weight: 700; color: var(--text-primary); letter-spacing: -0.5px; line-height: 1.1; }
  .tile-label { font-size: 11px; color: var(--text-muted); margin-top: 5px; text-transform: uppercase; letter-spacing: 0.4px; font-weight: 600; }
  .tile-good { border-color: var(--status-good); background: linear-gradient(180deg, var(--status-good-bg), var(--surface-1) 60%); }
  .tile-good .tile-num { color: var(--status-good); }
  .tile-warning .tile-num { color: var(--status-warning); }
  .tile-muted .tile-num { color: var(--text-muted); }

  .funnel { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 20px 22px; box-shadow: var(--shadow); }
  .funnel-row { display: grid; grid-template-columns: 130px 1fr 150px; align-items: center; gap: 12px; padding: 8px 0; }
  .funnel-label { font-size: 13px; color: var(--text-secondary); font-weight: 500; }
  .funnel-track { background: var(--page); border-radius: 5px; height: 16px; overflow: hidden; }
  .funnel-bar { height: 100%; border-radius: 5px; min-width: 5px; transition: width .5s cubic-bezier(.4,0,.2,1); }
  .funnel-value { font-size: 13px; color: var(--text-primary); font-weight: 600; text-align: right; }
  .funnel-pct { font-size: 12px; color: var(--text-muted); font-weight: 400; }

  .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 16px 20px; margin-bottom: 12px; box-shadow: var(--shadow); }
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

  .search { width: 100%; padding: 11px 13px; margin-bottom: 12px; border-radius: 9px;
            border: 1px solid var(--border); background: var(--surface-1); color: var(--text-primary);
            font-size: 14px; transition: border-color .15s ease, box-shadow .15s ease; }
  .search:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--status-info-bg); }
  .table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 12px; box-shadow: var(--shadow); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; background: var(--surface-1); }
  th { text-align: left; padding: 11px 13px; color: var(--text-muted); font-weight: 700; font-size: 11px;
       text-transform: uppercase; letter-spacing: 0.4px; background: var(--surface-1);
       border-bottom: 1px solid var(--border); white-space: nowrap; position: sticky; top: 0; }
  td { padding: 11px 13px; border-bottom: 1px solid var(--border); color: var(--text-primary); }
  tbody tr { transition: background .12s ease; }
  tbody tr:hover td { background: var(--page); }
  tr:last-child td { border-bottom: none; }
  td.num { font-variant-numeric: tabular-nums; color: var(--text-secondary); }

  .panel-box { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
               padding: 22px; box-shadow: var(--shadow); }
  .muted-text { color: var(--text-secondary); font-size: 13px; line-height: 1.55; }
  .checkbox { display: block; margin: 14px 0; font-size: 13px; color: var(--text-secondary); }
  .btn { display: inline-flex; align-items: center; justify-content: center; padding: 11px 20px;
         border-radius: 9px; font-size: 14px; font-weight: 600; border: 1px solid var(--border);
         cursor: pointer; text-decoration: none; margin-right: 8px;
         transition: transform .12s ease, box-shadow .15s ease, background .15s ease, filter .15s ease; }
  .btn:active { transform: translateY(1px); }
  .btn-primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent);
                 box-shadow: 0 1px 2px rgba(16,24,40,0.10); }
  .btn-primary:hover:not(:disabled) { filter: brightness(1.07); box-shadow: var(--shadow-lg); }
  .btn-ghost { background: transparent; color: var(--text-secondary); }
  .btn-ghost:hover { background: var(--page); color: var(--text-primary); }
  .form-actions { display: flex; justify-content: flex-end; margin-top: 20px; }
  .form-actions .btn { margin-right: 0; }
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
               border: 1px solid var(--border); background: var(--surface-1); border-radius: 10px; }
  .file-icon { color: var(--status-good); display: flex; align-items: center; }
  .file-meta { flex: 1; min-width: 0; }
  .file-name { font-size: 13px; font-weight: 600; color: var(--text-primary);
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-size { font-size: 12px; color: var(--text-muted); }
  .progress { height: 6px; background: var(--page); border-radius: 3px; overflow: hidden; margin-top: 8px; }
  .progress-bar { height: 100%; width: 0%; background: var(--accent); border-radius: 3px; transition: width .12s linear; }
  .progress-pct { font-size: 12px; color: var(--text-secondary); font-variant-numeric: tabular-nums; min-width: 34px; text-align: right; }
  .file-remove { background: transparent; border: none; color: var(--text-muted); font-size: 20px;
                 cursor: pointer; line-height: 1; padding: 0 4px; }
  .btn.loading::after { content: ''; display: inline-block; width: 12px; height: 12px; margin-left: 8px;
    border: 2px solid rgba(255,255,255,0.5); border-top-color: #fff; border-radius: 50%;
    vertical-align: middle; animation: spin .7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .hist-list { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; background: var(--surface-1); }
  .hist-row { display: flex; align-items: center; gap: 12px; padding: 10px 14px; border-bottom: 1px solid var(--border); }
  .hist-row:last-child { border-bottom: none; }
  .hist-when { font-size: 12px; color: var(--text-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .hist-detail { font-size: 13px; color: var(--text-secondary); flex: 1; min-width: 0; }
  @media (max-width: 520px) {
    .hist-row { flex-wrap: wrap; gap: 6px; }
    .hist-detail { flex-basis: 100%; }
  }

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


_NAV_ICON_FUNNEL = (
    '<svg class="nav-ic" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<line x1="6" y1="20" x2="6" y2="13"/><line x1="12" y1="20" x2="12" y2="8"/>'
    '<line x1="18" y1="20" x2="18" y2="4"/></svg>'
)
_NAV_ICON_IMPORT = (
    '<svg class="nav-ic" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
    '<polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>'
)


def _nav(active):
    def item(href, label, key, icon):
        cls = "nav-item active" if key == active else "nav-item"
        return f'<a class="{cls}" href="{href}">{icon}{label}</a>'
    return ('<nav class="nav"><div class="nav-inner">'
            + item("/painel", "Funil e contatos", "painel", _NAV_ICON_FUNNEL)
            + item("/importar", "Importar base", "importar", _NAV_ICON_IMPORT)
            + "</div></nav>")


def _page(title, subtitle, active, body):
    return (
        '<!doctype html><html lang="pt-br"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)}</title>"
        + _SHARED_CSS
        + "</head><body>"
        '<header><div class="header-inner"><div class="brand">GC</div>'
        '<div class="header-text"><h1>Guerra Cyrela &middot; Faria Lima</h1>'
        f'<p>{_e(subtitle)}</p></div></div></header>'
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

# Spreadsheet (xlsx) file-type icon shown next to the selected file.
_XLSX_ICON = (
    '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
    '<polyline points="14 2 14 8 20 8"/>'
    '<line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="16" y2="17"/></svg>'
)


def _render_history():
    items = run_history.recent(10)
    if not items:
        return '<div class="empty-state">Nenhuma execucao ainda.</div>'
    rows = "".join(
        f"""<div class="hist-row">
          <span class="hist-when">{_e(it.get('quando'))}</span>
          <span class="chip {'chip-good' if it.get('acao') == 'Importacao' else 'chip-info'}">{_e(it.get('acao'))}</span>
          <span class="hist-detail">{_e(it.get('detalhe'))}</span>
        </div>""" for it in items
    )
    return f'<div class="hist-list">{rows}</div>'


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
          <span class="file-icon">{_XLSX_ICON}</span>
          <div class="file-meta">
            <div id="file-name" class="file-name"></div>
            <div id="file-size" class="file-size"></div>
            <div id="progress-wrap" class="progress" hidden>
              <div id="progress-bar" class="progress-bar"></div>
            </div>
          </div>
          <span id="progress-pct" class="progress-pct"></span>
          <button type="button" class="file-remove" onclick="cancelUpload()" title="Cancelar">&times;</button>
        </div>
        <div id="file-error" class="alert alert-bad" hidden></div>
        <div class="form-actions">
          <button id="submit-btn" class="btn btn-primary" type="submit" disabled>Analisar planilha</button>
        </div>
      </form>
    </div>
  </section>
  <section>
    <h2>Historico de execucoes</h2>
    {_render_history()}
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
