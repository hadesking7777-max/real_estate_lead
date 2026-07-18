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

import hashlib
import html
import json
import os
import time
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify, redirect, session, Response, stream_with_context, g
from werkzeug.security import generate_password_hash, check_password_hash

import lead_store
import qualification
import send
import base_import
import run_history
import scheduler
import events
import i18n
from i18n import T
import logo_assets

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB cap on uploads
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "changeme")
PANEL_USER = os.environ.get("PANEL_USER", "lucas")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "changeme")
# Stable session-signing secret derived from the panel credentials (survives
# restarts without a separate env var; changes only if the password changes).
app.secret_key = hashlib.sha256(
    (PANEL_USER + PANEL_PASSWORD + "bidcyrela-session-v1").encode()
).hexdigest()
app.permanent_session_lifetime = timedelta(days=7)

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


@app.before_request
def _set_ui_lang():
    lang = request.cookies.get("ui_lang")
    g.ui_lang = lang if lang in i18n.LANGS else i18n.DEFAULT


@app.route("/idioma/<lang>")
def set_idioma(lang):
    # switch the UI language (stored in a cookie); works before login too, so the
    # login screen can be switched. Redirects back to wherever the user came from.
    nxt = _safe_next(request.args.get("next") or request.referrer or "/painel")
    resp = redirect(nxt)
    if lang in i18n.LANGS:
        resp.set_cookie("ui_lang", lang, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp

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


def _creds_ok(user, pw):
    if user != PANEL_USER:
        return False
    stored = lead_store.get_setting("panel_password_hash")
    if stored:
        return check_password_hash(stored, pw)
    return pw == PANEL_PASSWORD


def _requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("auth"):
            return f(*args, **kwargs)
        # Programmatic clients (and tests) may still use HTTP Basic auth.
        auth = request.authorization
        if auth and _creds_ok(auth.username, auth.password):
            return f(*args, **kwargs)
        return redirect("/login?next=" + request.path)

    return wrapper


def _safe_next(nxt):
    # prevent open-redirect: only same-site absolute paths
    return nxt if (nxt.startswith("/") and not nxt.startswith("//")) else "/painel"


@app.route("/login", methods=["GET"])
def login():
    if session.get("auth"):
        return redirect("/painel")
    return _render_login(next_url=_safe_next(request.args.get("next", "/painel")))


@app.route("/login", methods=["POST"])
def login_post():
    nxt = _safe_next(request.form.get("next", "/painel"))
    if _creds_ok(request.form.get("usuario", ""), request.form.get("senha", "")):
        session.permanent = True
        session["auth"] = True
        return redirect(nxt)
    return _render_login(error=T("Usuario ou senha invalidos."), next_url=nxt), 401


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/conta/senha", methods=["POST"])
@_requires_auth
def conta_senha():
    atual = request.form.get("atual", "")
    nova = request.form.get("nova", "")
    confirma = request.form.get("confirma", "")
    if not _creds_ok(PANEL_USER, atual):
        return redirect("/painel?conta=atual")
    if len(nova) < 6:
        return redirect("/painel?conta=curta")
    if nova != confirma:
        return redirect("/painel?conta=match")
    lead_store.set_setting("panel_password_hash", generate_password_hash(nova))
    return redirect("/painel?conta=ok")


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
        if wa_state == "failed":
            # keep WhatsApp's own error reason so the panel can show WHY it failed
            errs = status.get("errors") or []
            if errs:
                e = errs[0]
                detail = (e.get("error_data") or {}).get("details") or ""
                reason = f"{e.get('code')}: {e.get('title') or ''}"
                if detail:
                    reason += f" - {detail}"
                try:
                    lead_store.update_lead(phone, last_error=reason[:400])
                except Exception:  # noqa: BLE001 - never let logging break the webhook
                    pass
        events.bump()  # delivery status changed; push to open panels


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
    events.bump()  # reply + qualification updated the contact; push to open panels

    status, resp_text = send.send_text(phone, reply_text)
    if status not in (200, 201):
        app.logger.error("Send failed for %s: %s %s", phone, status, resp_text)


@app.route("/painel")
@_requires_auth
def painel():
    return _render_panel_html()


@app.route("/painel/fragmento")
@_requires_auth
def painel_fragmento():
    # inner content only, for the live refresh to swap into <main>
    return _panel_sections()


@app.route("/eventos")
@_requires_auth
def eventos():
    # Server-Sent Events: pushes "change" the instant state changes (send,
    # webhook, import), so pages update immediately instead of on a timer.
    def gen():
        last = events.current_version()
        yield "retry: 3000\n\n"
        while True:
            v = events.wait_for_change(last, timeout=25)
            if v != last:
                last = v
                yield "data: change\n\n"
            else:
                yield ": keepalive\n\n"
    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                   headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                            "Connection": "keep-alive"})


@app.route("/resultados")
def resultados():
    # merged into /painel; keep the route as a redirect for old bookmarks
    return redirect("/painel")


@app.route("/importar", methods=["GET"])
@_requires_auth
def importar():
    return _render_import_form()


@app.route("/importar/historico")
@_requires_auth
def importar_historico():
    # execution-history fragment for the import page's live refresh
    return _render_history()


@app.route("/importar/analisar", methods=["POST"])
@_requires_auth
def importar_analisar():
    file = request.files.get("arquivo")
    if not file or not file.filename.lower().endswith(".xlsx"):
        return _render_import_form(erro=T("Envie um arquivo .xlsx valido.")), 400
    token = uuid.uuid4().hex
    path = os.path.join(UPLOAD_DIR, f"{token}.xlsx")
    file.save(path)
    try:
        analysis = base_import.analyze(path)
    except Exception as exc:  # noqa: BLE001 - surface any parse error to the user
        os.remove(path)
        return _render_import_form(erro=T("Nao consegui ler a planilha: {exc}", exc=exc)), 400
    run_history.record(
        "Analise",
        f"{file.filename}: {analysis['total_rows']} linhas, {len(analysis['clean'])} BR, "
        f"{len(analysis['internacionais'])} internacionais, {analysis['removed_duplicates']} duplicados",
    )
    events.bump()  # new analysis in the history; push to open pages
    return _render_import_review(token, analysis)


@app.route("/campanha/enviar", methods=["POST"])
@_requires_auth
def campanha_enviar():
    try:
        qty = int(request.form.get("quantidade", "0"))
    except ValueError:
        qty = 0
    if qty > 0:
        scheduler.queue_manual(qty)
    return redirect("/painel")


@app.route("/campanha/parar", methods=["POST"])
@_requires_auth
def campanha_parar():
    scheduler.stop_manual()
    return redirect("/painel")


@app.route("/campanha/status")
@_requires_auth
def campanha_status():
    return jsonify(scheduler.status_summary())


@app.route("/contato/<phone>")
@_requires_auth
def contato(phone):
    lead = lead_store.get_lead(phone)
    if not lead:
        return _page(T("Contato"), T("Contato nao encontrado"), "painel",
                     f'<section><div class="empty-state">{T("Contato nao encontrado.")}</div>'
                     f'<p><a class="btn btn-ghost" href="/painel">{T("Voltar")}</a></p></section>'), 404
    return _render_contact(lead)


@app.route("/contato/<phone>/etapa", methods=["POST"])
@_requires_auth
def contato_etapa(phone):
    stage = request.form.get("stage", "")
    if stage in lead_store.STAGES and lead_store.get_lead(phone):
        lead_store.set_stage(phone, stage)
    return redirect(f"/contato/{phone}")


def _delivery_for_stage(stage, current):
    """Delivery state that keeps the card chip consistent with a manual stage move.
    Returns None to leave delivery untouched (e.g. not to downgrade a real receipt)."""
    if stage == "pendente":
        return "pendente"
    if stage == "contatado":
        # at least "sent", but never downgrade a real delivered/read receipt
        return "enviado" if current in ("pendente", "falhou") else None
    # every later stage (replied, qualifying, hot, warm, cold, opt-out) means they replied
    return "respondeu"


# Delivery tags the operator can choose when moving a card into Contacted.
CONTACTED_DELIVERY_CHOICES = ["enviado", "entregue", "lido", "falhou"]


@app.route("/board/etapa", methods=["POST"])
@_requires_auth
def board_etapa():
    # drag-and-drop stage change on the board; returns no body (AJAX)
    phone = request.form.get("phone", "")
    stage = request.form.get("stage", "")
    chosen = request.form.get("delivery", "")
    lead = lead_store.get_lead(phone) if phone else None
    if stage in lead_store.STAGES and lead:
        lead_store.set_stage(phone, stage)
        if chosen in DELIVERY_LABELS:
            # explicit tag picked in the modal (used when dropping into Contacted)
            lead_store.update_lead(phone, delivery=chosen)
        else:
            deliv = _delivery_for_stage(stage, lead.get("delivery") or "pendente")
            if deliv and deliv != lead.get("delivery"):
                lead_store.update_lead(phone, delivery=deliv)
        events.bump()  # push the move to every open panel
        return ("", 204)
    return ("invalid", 400)


@app.route("/contato/<phone>/tag", methods=["POST"])
@_requires_auth
def contato_tag_add(phone):
    lead_store.add_tag(phone, request.form.get("tag", ""))
    return redirect(f"/contato/{phone}")


@app.route("/contato/<phone>/tag/remover", methods=["POST"])
@_requires_auth
def contato_tag_remove(phone):
    lead_store.remove_tag(phone, request.form.get("tag", ""))
    return redirect(f"/contato/{phone}")


@app.route("/contato/<phone>/nota", methods=["POST"])
@_requires_auth
def contato_nota(phone):
    lead_store.add_note(phone, request.form.get("nota", ""), time.time())
    return redirect(f"/contato/{phone}")


@app.route("/importar/confirmar", methods=["POST"])
@_requires_auth
def importar_confirmar():
    token = request.form.get("token", "")
    include_intl = request.form.get("incluir_intl") == "sim"
    path = os.path.join(UPLOAD_DIR, os.path.basename(token) + ".xlsx")
    if not (token and os.path.exists(path)):
        return _render_import_form(erro=T("Sessao de upload expirou, envie a planilha de novo.")), 400
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
    events.bump()  # contacts imported; push to open pages
    return _render_import_done(imported, skipped, include_intl,
                               len(analysis["internacionais"]))


FUNNEL_STAGES = [
    ("contatado", "Contatados"),
    ("respondeu", "Responderam"),
    ("qualificando", "Em qualificacao"),
    ("quente", "Quentes"),
]

# Kanban board columns (status pipeline), in flow order. (stage_key, label, header css class)
BOARD_COLUMNS = [
    ("pendente", "Pendentes", ""),
    ("contatado", "Contatados", "col-info"),
    ("respondeu", "Responderam", "col-info"),
    ("qualificando", "Em qualificacao", "col-info"),
    ("quente", "Quentes", "col-good"),
    ("morno", "Morno", "col-warning"),
    ("frio", "Frio", ""),
    ("opt_out", "Opt-out", "col-bad"),
]


def _fmt_pct(n, d):
    return f"{(n / d * 100):.0f}%" if d else "0%"


def _e(x):
    return html.escape(str(x if x is not None else ""))


def _info_btn(key):
    return f'<button class="info-btn" type="button" onclick="showState(\'{key}\')" title="O que significa?">?</button>'


_INFO_MODAL_HTML = """
  <div id="state-modal" class="modal-overlay" hidden onclick="if(event.target===this)hideState()">
    <div class="modal-card">
      <div class="modal-head"><span id="modal-title" class="modal-title"></span>
        <button class="modal-close" type="button" onclick="hideState()">&times;</button></div>
      <p id="modal-text" class="modal-text"></p>
    </div>
  </div>"""


def _deliv_modal_html():
    # asked when a card is dropped into Contacted, which has four possible tags
    opts = "".join(
        f'<button class="deliv-opt" type="button" data-deliv="{k}">'
        f'<span class="chip {_DELIVERY_CHIP.get(k, "chip-muted")}">{T(DELIVERY_LABELS[k])}</span></button>'
        for k in CONTACTED_DELIVERY_CHOICES
    )
    return f"""
  <div id="deliv-modal" class="modal-overlay" hidden onclick="if(event.target===this)closeDeliv()">
    <div class="modal-card">
      <div class="modal-head"><span class="modal-title">{T("Qual o status do envio?")}</span>
        <button class="modal-close" type="button" onclick="closeDeliv()">&times;</button></div>
      <p class="modal-text">{T("Escolha como marcar este contato na coluna Contatados.")}</p>
      <div class="deliv-opts">{opts}</div>
    </div>
  </div>"""

# Live panel refresh: re-fetches the panel content every few seconds and swaps
# it in place, so every section (campaign progress, funnel, rates, chart, hot
# leads, board) reflects the campaign in real time. Preserves the search text
# and scroll, and skips a cycle while the user is typing or a modal is open.
_LIVE_JS = """
<script>
(function () {
  var main = document.querySelector('main');
  if (!main) return;
  var pending = false, timer = null;
  function busy() {
    var ae = document.activeElement;
    if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT')) return true;
    var sm = document.getElementById('state-modal'); if (sm && !sm.hidden) return true;
    var pm = document.getElementById('pwd-modal'); if (pm && !pm.hidden) return true;
    var dm = document.getElementById('deliv-modal'); if (dm && !dm.hidden) return true;
    return false;
  }
  function apply() {
    if (busy()) { pending = true; return; }
    pending = false;
    var s0 = document.querySelector('.search');
    var q = s0 ? s0.value : '';
    var sy = window.scrollY;
    fetch('/painel/fragmento', {credentials: 'same-origin'})
      .then(function (r) { if (!r.ok) throw 0; return r.text(); })
      .then(function (html) {
        main.innerHTML = html;
        var s1 = document.querySelector('.search');
        if (s1) s1.value = q;
        if (window.filterRows) window.filterRows(q);
        window.scrollTo(0, sy);
      })
      .catch(function () {});
  }
  function nudge() { clearTimeout(timer); timer = setTimeout(apply, 150); }
  document.addEventListener('focusout', function () { if (pending) nudge(); });
  document.addEventListener('click', function () { if (pending) nudge(); });
  // instant push via Server-Sent Events; falls back to slow polling if unsupported
  if (window.EventSource) {
    try {
      var es = new EventSource('/eventos');
      es.onmessage = function () { nudge(); };
    } catch (e) { setInterval(nudge, 6000); }
  } else {
    setInterval(nudge, 6000);
  }
})();
</script>"""


# State/rate explanation popups. Titles reuse the shared labels; descriptions are
# translated per current language. Built per request so it follows the UI language.
_STATE_INFO_SRC = {
    "campanha": ("Campanha", "Controle manual do envio. Voce escolhe quantos contatos disparar agora e clica em Enviar agora. Pode repetir quantas vezes quiser. Os envios saem espacados automaticamente para proteger o numero."),
    "pendente": ("Pendentes", "Contatos que ainda nao receberam nenhuma mensagem. Estao na fila para o primeiro contato quando a campanha rodar."),
    "contatado": ("Contatados", "Ja receberam a mensagem de abertura, mas ainda nao responderam. Aguardando resposta, ou entrando na cadencia de follow-up."),
    "respondeu": ("Responderam", "Responderam a primeira mensagem. A IA comeca a qualificacao a partir daqui."),
    "qualificando": ("Em qualificacao", "Estao conversando com a IA agora, que mede intencao, capital, forma de pagamento e timing."),
    "quente": ("Quentes", "Leads qualificados, com alta intencao de compra. Ja entregues no seu WhatsApp para fechar."),
    "morno": ("Morno", "Tem interesse, mas com horizonte mais longo ou capital ainda indefinido. Ficam sendo nutridos pela IA."),
    "frio": ("Frio", "Sem interesse real no momento. Saem do fluxo ativo da campanha."),
    "opt_out": ("Opt-out", "Pediram para nao receber mais mensagens. Removidos na hora e nunca mais contatados."),
    "taxa_entrega": ("Taxa de entrega", "Percentual das mensagens enviadas que chegaram no aparelho do contato. Mede a qualidade da base e a saude do numero."),
    "taxa_leitura": ("Taxa de leitura", "Percentual das mensagens enviadas que foram lidas (abertas) pelo contato."),
    "taxa_resposta": ("Taxa de resposta", "Percentual das mensagens enviadas que geraram uma resposta. E a principal metrica de reativacao."),
    "taxa_qualificacao": ("Taxa de qualificacao", "Dos que responderam, quantos viraram leads quentes (investidores prontos para fechar)."),
}


def _board_js():
    entries = ",\n".join(
        f"  {k}: {{t: {json.dumps(T(t))}, d: {json.dumps(T(d))}}}"
        for k, (t, d) in _STATE_INFO_SRC.items()
    )
    return """
<script>
window.filterRows = function (q) {
  q = (q || '').toLowerCase().trim();
  document.querySelectorAll('.kanban-card').forEach(function (c) {
    var hay = c.getAttribute('data-search') || '';
    c.style.display = (!q || hay.indexOf(q) !== -1) ? '' : 'none';
  });
};
var STATE_INFO = {
""" + entries + """
};
window.showState = function (k) {
  var info = STATE_INFO[k]; if (!info) return;
  document.getElementById('modal-title').textContent = info.t;
  document.getElementById('modal-text').textContent = info.d;
  document.getElementById('state-modal').hidden = false;
};
window.hideState = function () {
  var m = document.getElementById('state-modal'); if (m) m.hidden = true;
};
document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { window.hideState(); if (window.closeDeliv) window.closeDeliv(); } });

// Drag a card between columns to change its stage. Delegated on document so it
// keeps working after the live refresh swaps the board's HTML.
(function () {
  var dragPhone = null;
  document.addEventListener('dragstart', function (e) {
    var card = e.target.closest && e.target.closest('.kanban-card');
    if (!card) return;
    dragPhone = card.getAttribute('data-phone');
    card.classList.add('dragging');
    try { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', dragPhone); } catch (err) {}
  });
  document.addEventListener('dragend', function (e) {
    var card = e.target.closest && e.target.closest('.kanban-card');
    if (card) card.classList.remove('dragging');
    document.querySelectorAll('.board-col-body.drop-hover').forEach(function (b) { b.classList.remove('drop-hover'); });
  });
  document.addEventListener('dragover', function (e) {
    var body = e.target.closest && e.target.closest('.board-col-body');
    if (!body) return;
    e.preventDefault();
    try { e.dataTransfer.dropEffect = 'move'; } catch (err) {}
    body.classList.add('drop-hover');
  });
  document.addEventListener('dragleave', function (e) {
    var body = e.target.closest && e.target.closest('.board-col-body');
    if (body && !body.contains(e.relatedTarget)) body.classList.remove('drop-hover');
  });
  function moveCard(phone, body) {
    var card = document.querySelector('.kanban-card[data-phone="' + phone + '"]');
    if (card) {
      var empty = body.querySelector('.board-empty'); if (empty) empty.remove();
      body.appendChild(card);  // optimistic move; the push refresh reconciles counts
    }
  }
  function postMove(phone, stage, deliv) {
    var fd = new FormData(); fd.append('phone', phone); fd.append('stage', stage);
    if (deliv) fd.append('delivery', deliv);
    fetch('/board/etapa', { method: 'POST', body: fd, credentials: 'same-origin' }).catch(function () {});
  }

  // Contacted has four possible tags, so ask which one via the modal.
  var pendingDrop = null;
  window.closeDeliv = function () {
    var m = document.getElementById('deliv-modal'); if (m) m.hidden = true;
    pendingDrop = null;
  };
  document.addEventListener('click', function (e) {
    var opt = e.target.closest && e.target.closest('.deliv-opt');
    if (!opt || !pendingDrop) return;
    var d = pendingDrop; pendingDrop = null;
    var m = document.getElementById('deliv-modal'); if (m) m.hidden = true;
    moveCard(d.phone, d.body);
    postMove(d.phone, 'contatado', opt.getAttribute('data-deliv'));
  });

  document.addEventListener('drop', function (e) {
    var body = e.target.closest && e.target.closest('.board-col-body');
    if (!body) return;
    e.preventDefault();
    body.classList.remove('drop-hover');
    var phone = dragPhone || (e.dataTransfer && e.dataTransfer.getData('text/plain'));
    dragPhone = null;
    var stage = body.getAttribute('data-stage');
    if (!phone || !stage) return;
    var card = document.querySelector('.kanban-card[data-phone="' + phone + '"]');
    if (card && card.parentNode === body) return;  // same column, nothing to do
    if (stage === 'contatado') {
      // pick the delivery tag before moving
      pendingDrop = { phone: phone, body: body };
      var m = document.getElementById('deliv-modal'); if (m) m.hidden = false;
      return;
    }
    moveCard(phone, body);
    postMove(phone, stage, null);
  });
})();
</script>
"""

_IMPORT_LIVE_JS = """
<script>
(function () {
  var timer = null;
  function apply() {
    fetch('/importar/historico', {credentials: 'same-origin'})
      .then(function (r) { if (!r.ok) throw 0; return r.text(); })
      .then(function (html) {
        var el = document.getElementById('history-live');
        if (el) el.innerHTML = html;
      })
      .catch(function () {});
  }
  function nudge() { clearTimeout(timer); timer = setTimeout(apply, 150); }
  if (window.EventSource) {
    try { var es = new EventSource('/eventos'); es.onmessage = function () { nudge(); }; }
    catch (e) { setInterval(nudge, 6000); }
  } else { setInterval(nudge, 6000); }
})();
</script>"""

def _import_js():
    L = {
        "analyze": T("Analisar planilha"),
        "uploading": T("Enviando"),
        "analyzing": T("Analisando"),
        "notXlsx": T("Esse arquivo nao e .xlsx. Envie uma planilha do Excel."),
        "uploadErr": T("Erro no envio ({s}). Tente de novo."),
        "connErr": T("Falha de conexao no envio. Tente de novo."),
    }
    return "<script>\nvar IMPL = " + json.dumps(L) + ";\n" + _IMPORT_JS_BODY + "\n</script>"


_IMPORT_JS_BODY = """
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
    submit.disabled = true; submit.textContent = IMPL.analyze; submit.classList.remove('loading');
    dz.hidden = false;
  }
  window.cancelUpload = reset;

  function chooseFile(f) {
    if (!f.name.toLowerCase().endsWith('.xlsx')) {
      errEl.textContent = IMPL.notXlsx;
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
    submit.disabled = false; submit.classList.remove('loading'); submit.textContent = IMPL.analyze;
    xhr = null;
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (!selected) return;
    var fd = new FormData();
    fd.append('arquivo', selected, selected.name);
    xhr = new XMLHttpRequest();
    xhr.open('POST', '/importar/analisar');
    submit.disabled = true; submit.classList.add('loading'); submit.textContent = IMPL.uploading;
    progWrap.hidden = false; progBar.style.width = '0%';
    xhr.upload.onprogress = function (ev) {
      if (ev.lengthComputable) {
        var pct = Math.round(ev.loaded / ev.total * 100);
        progBar.style.width = pct + '%';
        if (progPct) progPct.textContent = pct + '%';
        if (pct >= 100) submit.textContent = IMPL.analyzing;
      }
    };
    xhr.onload = function () {
      if (xhr.status === 200) {
        document.open(); document.write(xhr.responseText); document.close();
      } else {
        failMsg(IMPL.uploadErr.replace('{s}', xhr.status));
      }
    };
    xhr.onerror = function () { failMsg(IMPL.connErr); };
    xhr.send(fd);
  });
})();
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
  .header-inner { max-width: 1280px; margin: 0 auto; padding: 18px 20px; display: flex; align-items: center; gap: 14px; }
  .brand { width: 40px; height: 40px; border-radius: 11px; background: rgba(255,255,255,0.14);
           display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 15px;
           letter-spacing: 0.5px; border: 1px solid rgba(255,255,255,0.20); flex-shrink: 0; }
  .brand-logo { height: 48px; width: auto; border-radius: 10px; display: block; flex-shrink: 0;
                border: 1px solid rgba(255,255,255,0.16); }
  .header-text h1 { margin: 0; font-size: 18px; font-weight: 600; letter-spacing: -0.2px; }
  .header-text p { margin: 3px 0 0; font-size: 13px; opacity: 0.82; }
  .nav { background: var(--nav-bg); border-bottom: 1px solid var(--border);
         padding: 0 20px; position: sticky; top: 0; z-index: 10; }
  .nav-inner { display: flex; gap: 2px; max-width: 1280px; margin: 0 auto; }
  .nav-item { display: inline-flex; align-items: center; gap: 7px; padding: 13px 14px; font-size: 13px;
              color: var(--text-secondary); text-decoration: none; border-bottom: 2px solid transparent;
              transition: color .15s ease, border-color .15s ease; }
  .nav-item:hover { color: var(--text-primary); }
  .nav-item.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
  .nav-ic { display: flex; }
  main { padding: 22px 20px 44px; max-width: 1280px; margin: 0 auto; }
  section { margin-bottom: 28px; }
  .contact-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; align-items: start; }
  .contact-grid > section { margin-bottom: 0; }
  .contact-grid > section.grid-full { grid-column: 1 / -1; }
  @media (max-width: 760px) { .contact-grid { grid-template-columns: 1fr; } }
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
  .tile-sub { font-size: 11px; color: var(--text-muted); margin-top: 3px; font-variant-numeric: tabular-nums; }

  .colchart-wrap { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
                   padding: 20px 22px; box-shadow: var(--shadow); overflow-x: auto; }
  .colchart { display: flex; align-items: flex-end; gap: 16px; height: 180px; min-width: min-content; }
  .col { display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; gap: 6px; flex: 0 0 auto; }
  .col-bar { width: 26px; background: var(--accent); border-radius: 4px 4px 0 0; min-height: 4px;
             transition: height .5s cubic-bezier(.4,0,.2,1); }
  .col-val { font-size: 12px; font-weight: 600; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
  .col-x { font-size: 11px; color: var(--text-muted); white-space: nowrap; }

  .funnel { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 20px 22px; box-shadow: var(--shadow); }
  .funnel-row { display: grid; grid-template-columns: 130px 1fr 240px; align-items: center; gap: 12px; padding: 8px 0; }
  .funnel-label { font-size: 13px; color: var(--text-secondary); font-weight: 500; }
  .funnel-track { background: var(--page); border-radius: 5px; height: 16px; overflow: hidden; }
  .funnel-bar { height: 100%; border-radius: 5px; min-width: 5px; transition: width .5s cubic-bezier(.4,0,.2,1); }
  .funnel-value { font-size: 13px; color: var(--text-primary); font-weight: 600; text-align: right; white-space: nowrap; }
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

  .nav-spacer { flex: 1; }
  .nav-logout { color: var(--text-muted); }
  .nav-logout:hover { color: var(--status-bad); }

  .login-body { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px;
                background:
                  radial-gradient(1200px 500px at 50% -10%, var(--status-info-bg), transparent 70%),
                  var(--page); }
  .login-card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 16px;
                padding: 32px 28px; width: 100%; max-width: 360px; box-shadow: var(--shadow-lg); text-align: center; }
  .login-brand { width: 48px; height: 48px; border-radius: 13px; background: var(--accent); color: #fff;
                 display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 18px;
                 letter-spacing: 0.5px; margin: 0 auto 14px; }
  .login-logo { height: 84px; width: auto; display: block; margin: 0 auto 16px; border-radius: 12px; }
  .login-title { margin: 0; font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }
  .login-sub { margin: 4px 0 22px; font-size: 13px; color: var(--text-muted); }
  .login-label { display: block; text-align: left; font-size: 12px; color: var(--text-secondary);
                 font-weight: 600; margin: 12px 0 6px; }
  .login-input { width: 100%; padding: 11px 13px; border-radius: 9px; border: 1px solid var(--border);
                 background: var(--page); color: var(--text-primary); font-size: 14px;
                 transition: border-color .15s ease, box-shadow .15s ease; }
  .login-input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--status-info-bg); }
  .login-btn { width: 100%; margin-top: 18px; margin-right: 0; }
  .login-error { background: var(--status-bad-bg); color: var(--status-bad); font-size: 13px;
                 padding: 10px 12px; border-radius: 8px; margin-bottom: 6px; text-align: left; }
  .campaign-box { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
                  padding: 16px 20px; box-shadow: var(--shadow); display: flex; flex-direction: column; gap: 14px; }
  .campaign-top { display: flex; align-items: center; justify-content: space-between; gap: 14px; flex-wrap: wrap; }
  .campaign-info { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .campaign-metrics { font-size: 13px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
  .campaign-box form { margin: 0; }
  .campaign-box .btn { margin-right: 0; }
  .camp-progress { height: 8px; background: var(--page); border-radius: 5px; overflow: hidden; }
  .camp-progress-bar { height: 100%; background: var(--status-good); border-radius: 5px;
                       min-width: 4px; transition: width .4s ease; }
  .send-form { display: flex; align-items: center; gap: 8px; }
  .qty-input { width: 84px; padding: 10px 12px; border-radius: 9px; border: 1px solid var(--border);
               background: var(--page); color: var(--text-primary); font-size: 14px; text-align: center;
               font-variant-numeric: tabular-nums; }
  .qty-input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--status-info-bg); }

  .contact-link { color: var(--accent); text-decoration: none; font-weight: 600; }
  .contact-link:hover { text-decoration: underline; }
  .row-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 5px; }
  .chip.mini { font-size: 10px; padding: 1px 6px; }
  .tag-more { font-size: 10px; color: var(--text-muted); align-self: center; }
  .back-link { display: inline-block; color: var(--text-secondary); text-decoration: none; font-size: 13px; margin-bottom: 14px; }
  .back-link:hover { color: var(--accent); }
  .contact-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 14px; flex-wrap: wrap;
                    background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; box-shadow: var(--shadow); }
  .contact-name { font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }
  .contact-sub { font-size: 13px; color: var(--text-muted); margin-top: 4px; font-variant-numeric: tabular-nums; }
  .contact-chips { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .contact-chips .btn { margin: 0; padding: 8px 14px; font-size: 13px; }
  .tag-group { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
  .tag-group-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.4px; color: var(--text-muted); font-weight: 700; min-width: 90px; }
  .tag-chip { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; font-weight: 600;
              padding: 3px 6px 3px 9px; border-radius: 6px; background: var(--status-info-bg); color: var(--status-info); }
  .tag-x { display: inline; margin: 0; }
  .tag-x button { background: transparent; border: none; color: var(--status-info); cursor: pointer; font-size: 15px; line-height: 1; padding: 0 2px; }
  .inline-form { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 8px; }
  .inline-input { width: auto; flex: 1 1 200px; margin-bottom: 0; }
  .inline-form .btn { margin: 0; }
  .select { padding: 10px 12px; border-radius: 9px; border: 1px solid var(--border);
            background: var(--surface-1); color: var(--text-primary); font-size: 14px; }
  .notes-list { margin-top: 14px; }
  .note-item { display: flex; gap: 12px; padding: 8px 0; border-top: 1px solid var(--border); font-size: 13px; }
  .note-when { color: var(--text-muted); font-size: 12px; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .timeline { display: flex; flex-direction: column; gap: 10px; }
  .tl-system { text-align: center; font-size: 12px; color: var(--text-muted); padding: 4px 0; }
  .tl-tmpl { display: inline-block; margin-left: 6px; font-family: ui-monospace, monospace; font-size: 11px; opacity: 0.8; }
  .tl-msg { max-width: 78%; }
  .tl-in { align-self: flex-start; }
  .tl-out { align-self: flex-end; text-align: right; }
  .tl-who { font-size: 11px; color: var(--text-muted); margin-bottom: 3px; }
  .tl-bubble { display: inline-block; padding: 10px 13px; border-radius: 12px; font-size: 13px; line-height: 1.4;
               border: 1px solid var(--border); background: var(--surface-1); text-align: left; }
  .tl-out .tl-bubble { background: var(--status-info-bg); border-color: transparent; }
  .alert { padding: 12px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 14px; }
  .alert-good { color: var(--status-good); background: var(--status-good-bg); }
  .alert-bad { color: var(--status-bad); background: var(--status-bad-bg); }
  .empty-state { color: var(--text-muted); font-size: 13px; padding: 20px; text-align: center;
                 border: 1px dashed var(--border); border-radius: 10px; }

  .board { display: flex; gap: 12px; overflow-x: auto; padding-bottom: 10px; align-items: stretch; }
  .board-col { flex: 0 0 250px; background: var(--page); border: 1px solid var(--border); border-radius: 12px;
               display: flex; flex-direction: column; height: 560px; }
  .board-col-head { padding: 12px 14px; border-bottom: 1px solid var(--border); display: flex;
                    align-items: center; justify-content: space-between; border-top: 3px solid var(--text-muted);
                    border-radius: 12px 12px 0 0; }
  .board-col.col-info .board-col-head { border-top-color: var(--accent); }
  .board-col.col-good .board-col-head { border-top-color: var(--status-good); }
  .board-col.col-warning .board-col-head { border-top-color: var(--status-warning); }
  .board-col.col-bad .board-col-head { border-top-color: var(--status-bad); }
  .board-col-title { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.4px;
                     color: var(--text-secondary); }
  .board-col-count { font-size: 12px; color: var(--text-muted); background: var(--surface-1);
                     border: 1px solid var(--border); border-radius: 20px; padding: 1px 9px; font-variant-numeric: tabular-nums; }
  .board-col-body { flex: 1; min-height: 0; padding: 10px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
  .kanban-card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
                 padding: 12px; box-shadow: var(--shadow); transition: border-color .12s ease, transform .12s ease; }
  .kanban-card:hover { border-color: var(--accent); transform: translateY(-1px); }
  .kanban-card { cursor: grab; }
  .kanban-card:active { cursor: grabbing; }
  .kanban-card.dragging { opacity: 0.45; }
  .board-col-body.drop-hover { outline: 2px dashed var(--accent); outline-offset: -4px; border-radius: 8px;
                               background: rgba(42,120,214,0.08); }
  .deliv-opts { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 14px; }
  .deliv-opt { display: flex; align-items: center; justify-content: center; padding: 14px 10px;
               background: var(--page); border: 1px solid var(--border); border-radius: 10px;
               cursor: pointer; transition: border-color .12s ease, transform .12s ease; }
  .deliv-opt:hover { border-color: var(--accent); transform: translateY(-1px); }
  .kanban-phone { font-size: 12px; color: var(--text-muted); font-variant-numeric: tabular-nums; margin-top: 4px; }
  .kanban-email { font-size: 12px; color: var(--text-muted); margin-top: 2px; word-break: break-all; }
  .kanban-foot { margin-top: 8px; }
  .kanban-error { margin-top: 5px; font-size: 11px; line-height: 1.35; color: var(--status-bad);
                  word-break: break-word; }
  .board-empty { color: var(--text-muted); font-size: 12px; text-align: center; padding: 16px 8px; }

  .info-btn { width: 16px; height: 16px; border-radius: 50%; border: 1px solid var(--border);
              background: transparent; color: var(--text-muted); font-size: 10px; font-weight: 700;
              cursor: pointer; display: inline-flex; align-items: center; justify-content: center;
              line-height: 1; margin-left: 6px; vertical-align: middle; padding: 0;
              transition: color .12s ease, border-color .12s ease; }
  .info-btn:hover { color: var(--accent); border-color: var(--accent); }
  .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: flex; align-items: center;
                   justify-content: center; padding: 20px; z-index: 100; animation: fadein .15s ease; }
  .modal-card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 14px;
                padding: 22px 24px; max-width: 400px; width: 100%; box-shadow: var(--shadow-lg); animation: pop .16s ease; }
  .modal-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
  .modal-title { font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: var(--accent); }
  .modal-close { background: transparent; border: none; color: var(--text-muted); font-size: 22px;
                 cursor: pointer; line-height: 1; padding: 0; }
  .modal-close:hover { color: var(--text-primary); }
  .modal-text { font-size: 14px; color: var(--text-secondary); line-height: 1.55; margin: 0; }
  .modal-label { display: block; text-align: left; font-size: 12px; color: var(--text-secondary);
                 font-weight: 600; margin: 12px 0 6px; }
  .modal-input { width: 100%; padding: 10px 12px; border-radius: 9px; border: 1px solid var(--border);
                 background: var(--page); color: var(--text-primary); font-size: 14px; }
  .modal-input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--status-info-bg); }
  @keyframes fadein { from { opacity: 0; } to { opacity: 1; } }
  @keyframes pop { from { opacity: 0; transform: translateY(6px) scale(0.98); } to { opacity: 1; transform: none; } }

  .account { position: relative; }
  .account-btn { display: inline-flex; align-items: center; gap: 8px; background: transparent; border: none;
                 color: var(--text-secondary); font-size: 13px; cursor: pointer; padding: 10px 4px; }
  .account-btn:hover { color: var(--text-primary); }
  .account-avatar { width: 26px; height: 26px; border-radius: 50%; background: var(--accent); color: #fff;
                    display: inline-flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; }
  .account-name { font-weight: 600; }
  .account-caret { font-size: 10px; color: var(--text-muted); }
  .account-menu { position: absolute; right: 0; top: calc(100% + 4px); background: var(--surface-1);
                  border: 1px solid var(--border); border-radius: 10px; box-shadow: var(--shadow-lg);
                  min-width: 170px; padding: 6px; z-index: 50; }
  .account-item { display: block; width: 100%; text-align: left; background: transparent; border: none;
                  color: var(--text-primary); font-size: 13px; padding: 9px 10px; border-radius: 7px;
                  cursor: pointer; text-decoration: none; }
  .account-item:hover { background: var(--page); }
  .account-divider { height: 1px; background: var(--border); margin: 6px 4px; }
  .account-sub-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px;
                       color: var(--text-muted); padding: 8px 10px 3px; }
  .lang-item { display: flex; justify-content: space-between; align-items: center; }
  .lang-item.lang-active { color: var(--accent); font-weight: 600; }
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


def _delivery_chip(state, title=None):
    cls = _DELIVERY_CHIP.get(state, "chip-muted")
    tip = f' title="{_e(title)}"' if title else ""
    return f'<span class="chip {cls}"{tip}>{T(DELIVERY_LABELS.get(state, state))}</span>'


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
def _lang_menu():
    cur = i18n.current()
    nxt = _e(request.path if request else "/painel")
    rows = ""
    for code in i18n.LANGS:
        mark = ' &#10003;' if code == cur else ''
        active = ' lang-active' if code == cur else ''
        rows += (f'<a class="account-item lang-item{active}" href="/idioma/{code}?next={nxt}">'
                 f'{_e(i18n.LANG_NAMES[code])}{mark}</a>')
    return (f'<div class="account-sub-label">{T("Idioma")}</div>{rows}')


def _nav(active):
    def item(href, label, key, icon):
        cls = "nav-item active" if key == active else "nav-item"
        return f'<a class="{cls}" href="{href}">{icon}{label}</a>'
    initials = (PANEL_USER[:2] or "U").upper()
    account = (
        '<div class="account">'
        '<button class="account-btn" type="button" onclick="toggleAccount(event)">'
        f'<span class="account-avatar">{_e(initials)}</span>'
        f'<span class="account-name">{_e(PANEL_USER)}</span>'
        '<span class="account-caret">&#9662;</span></button>'
        '<div class="account-menu" id="account-menu" hidden>'
        f'<button class="account-item" type="button" onclick="openPwd()">{T("Trocar senha")}</button>'
        + _lang_menu()
        + '<div class="account-divider"></div>'
        f'<a class="account-item" href="/logout">{T("Sair")}</a>'
        '</div></div>'
    )
    return ('<nav class="nav"><div class="nav-inner">'
            + item("/painel", T("Painel"), "painel", _NAV_ICON_FUNNEL)
            + item("/importar", T("Importar base"), "importar", _NAV_ICON_IMPORT)
            + '<span class="nav-spacer"></span>'
            + account
            + "</div></nav>")


def _account_html():
    return f"""
  <div id="pwd-modal" class="modal-overlay" hidden onclick="if(event.target===this)closePwd()">
    <div class="modal-card">
      <div class="modal-head"><span class="modal-title">{T("Trocar senha")}</span>
        <button class="modal-close" type="button" onclick="closePwd()">&times;</button></div>
      <form method="post" action="/conta/senha">
        <label class="modal-label">{T("Senha atual")}</label>
        <input class="modal-input" type="password" name="atual" required autocomplete="current-password">
        <label class="modal-label">{T("Nova senha (minimo 6 caracteres)")}</label>
        <input class="modal-input" type="password" name="nova" required minlength="6" autocomplete="new-password">
        <label class="modal-label">{T("Confirmar nova senha")}</label>
        <input class="modal-input" type="password" name="confirma" required minlength="6" autocomplete="new-password">
        <div class="form-actions"><button class="btn btn-primary" type="submit">{T("Salvar")}</button></div>
      </form>
    </div>
  </div>
""" + _ACCOUNT_JS


_ACCOUNT_JS = """
<script>
window.toggleAccount = function (e) { if (e) e.stopPropagation();
  var m = document.getElementById('account-menu'); if (m) m.hidden = !m.hidden; };
document.addEventListener('click', function () {
  var m = document.getElementById('account-menu'); if (m) m.hidden = true; });
window.openPwd = function () {
  var p = document.getElementById('pwd-modal'); if (p) p.hidden = false;
  var m = document.getElementById('account-menu'); if (m) m.hidden = true; };
window.closePwd = function () {
  var p = document.getElementById('pwd-modal'); if (p) p.hidden = true; };
document.addEventListener('keydown', function (e) { if (e.key === 'Escape') window.closePwd(); });
</script>"""


_FAVICON_TAG = f'<link rel="icon" type="image/png" href="{logo_assets.FAVICON_URI}">'


def _render_login(error=None, next_url="/painel"):
    err = f'<div class="login-error">{_e(error)}</div>' if error else ""
    return (
        f'<!doctype html><html lang="{i18n.html_lang()}"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{T("Entrar")} &middot; Guerra Cyrela</title>'
        + _FAVICON_TAG
        + _SHARED_CSS
        + '</head><body class="login-body"><div class="login-card">'
        f'<img class="login-logo" src="{logo_assets.LOGO_URI}" alt="Guerra Cyrela">'
        '<h1 class="login-title">Guerra Cyrela</h1>'
        f'<p class="login-sub">{T("Painel de reativacao")}</p>'
        + err
        + '<form method="post" action="/login">'
        f'<input type="hidden" name="next" value="{_e(next_url)}">'
        f'<label class="login-label">{T("Usuario")}</label>'
        '<input class="login-input" type="text" name="usuario" autocomplete="username" autofocus required>'
        f'<label class="login-label">{T("Senha")}</label>'
        '<input class="login-input" type="password" name="senha" autocomplete="current-password" required>'
        f'<button class="btn btn-primary login-btn" type="submit">{T("Entrar")}</button>'
        '</form></div></body></html>'
    )


def _page(title, subtitle, active, body):
    return (
        f'<!doctype html><html lang="{i18n.html_lang()}"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)}</title>"
        + _FAVICON_TAG
        + _SHARED_CSS
        + "</head><body>"
        f'<header><div class="header-inner"><img class="brand-logo" src="{logo_assets.LOGO_URI}" alt="Guerra Cyrela">'
        '<div class="header-text"><h1>Guerra Cyrela &middot; Faria Lima</h1>'
        f'<p>{_e(subtitle)}</p></div></div></header>'
        + _nav(active)
        + f"<main>{body}</main>"
        + _account_html()
        + "</body></html>"
    )


def _fmt_ts(ts):
    if not ts:
        return ""
    return (datetime.utcfromtimestamp(ts) - timedelta(hours=3)).strftime("%d/%m/%Y %H:%M")


def _auto_tags(lead):
    tags = ["PJ" if str(lead.get("perfil", "")).startswith("PJ") else "PF"]
    if lead.get("origem") == "Internacional":
        tags.append("Internacional")
        if lead.get("pais"):
            tags.append(lead["pais"])
    else:
        tags.append("SP capital" if str(lead.get("phone", "")).startswith("5511") else "Interior BR")
    s = lead.get("signals", {}) or {}
    fp = (s.get("forma_pagamento") or "").lower()
    if "vista" in fp:
        tags.append("A vista")
    elif "financ" in fp:
        tags.append("Financiado")
    tm = (s.get("timing") or "").lower()
    if "agora" in tm or "curto" in tm:
        tags.append("Timing curto")
    ex = (s.get("experiencia") or "").lower()
    if "investe" in ex or "recorrente" in ex or "portfolio" in ex or "portfólio" in ex:
        tags.append("Investidor recorrente")
    ob = (s.get("objetivo") or "").lower()
    if "renda" in ob or "aluguel" in ob or "locac" in ob or "locaç" in ob:
        tags.append("Renda")
    if "valoriz" in ob:
        tags.append("Valorizacao")
    return tags


def _timeline_html(lead):
    items = ""
    kind_labels = {"opener": T("Abertura enviada"), "followup1": T("Follow-up 1 enviado"),
                   "followup2": T("Follow-up 2 enviado"), "template": T("Mensagem enviada")}
    for h in lead["history"]:
        role, text = h["role"], (h["text"] or "")
        if role == "bot" and text.startswith("["):
            inner = text.strip("[]")
            kind = inner.split(":")[0]
            tmpl = inner.split(":", 1)[1] if ":" in inner else ""
            items += (f'<div class="tl-system">{kind_labels.get(kind, T("Mensagem enviada"))}'
                      f'<span class="tl-tmpl">{_e(tmpl)}</span></div>')
        elif role == "lead":
            items += (f'<div class="tl-msg tl-in"><div class="tl-who">{T("Investidor")}</div>'
                      f'<div class="tl-bubble">{_e(text)}</div></div>')
        else:
            items += (f'<div class="tl-msg tl-out"><div class="tl-who">{T("Assistente")}</div>'
                      f'<div class="tl-bubble">{_e(text)}</div></div>')
    return items or f'<div class="empty-state">{T("Nenhuma mensagem ainda.")}</div>'


def _render_contact(lead):
    phone = lead["phone"]
    auto = _auto_tags(lead)
    manual = lead_store.get_tags(phone)
    notes = lead_store.get_notes(phone)
    s = lead["signals"]

    auto_chips = "".join(f'<span class="chip chip-muted">{_e(t)}</span>' for t in auto) or '<span class="muted-text">-</span>'
    manual_chips = "".join(
        f'<span class="tag-chip">{_e(t)}'
        f'<form method="post" action="/contato/{_e(phone)}/tag/remover" class="tag-x">'
        f'<input type="hidden" name="tag" value="{_e(t)}"><button type="submit" title="{T("Remover")}">&times;</button></form>'
        f"</span>" for t in manual
    ) or f'<span class="muted-text">{T("Nenhuma tag manual.")}</span>'

    signals_html = "".join(
        f'<div><span class="signal-label">{T(lbl)}</span>{_e(s.get(k) or "-")}</div>'
        for k, lbl in [("objetivo", "Objetivo"), ("experiencia", "Experiencia"),
                       ("forma_pagamento", "Forma de pagamento"),
                       ("quantidade_unidades", "Unidades"), ("timing", "Timing")]
    )
    notes_list = "".join(
        f'<div class="note-item"><span class="note-when">{_fmt_ts(n["ts"])}</span>'
        f'<span>{_e(n["text"])}</span></div>' for n in notes
    ) or f'<div class="empty-state">{T("Nenhuma nota ainda.")}</div>'

    body = f"""
  <a class="back-link" href="/painel">&larr; {T("Voltar ao painel")}</a>
  <div class="contact-grid">
  <section>
    <div class="contact-header">
      <div>
        <div class="contact-name">{_e(lead["nome"] or T("(sem nome)"))}</div>
        <div class="contact-sub">{_e(phone)} &middot; {_e(lead["pais"] or lead["origem"])}{f' &middot; {_e(lead["email"])}' if lead.get("email") else ""}</div>
      </div>
      <div class="contact-chips">
        <span class="chip chip-muted">{_e(lead["perfil"])}</span>
        <span class="chip chip-info">{_e(T(STAGE_LABELS.get(lead["stage"], lead["stage"])))}</span>
        {_delivery_chip(lead.get("delivery", "pendente"), lead.get("last_error"))}
        <a class="btn btn-primary" href="https://wa.me/{_e(phone)}" target="_blank" rel="noopener">{T("Abrir no WhatsApp")}</a>
      </div>
    </div>
    {f'<div class="alert alert-bad">{T("Motivo da falha")}: {_e(lead.get("last_error"))}</div>' if lead.get("delivery") == "falhou" and lead.get("last_error") else ""}
  </section>

  <section>
    <h2>{T("Tags")}</h2>
    <div class="panel-box">
      <div class="tag-group"><span class="tag-group-label">{T("Automaticas")}</span>{auto_chips}</div>
      <div class="tag-group"><span class="tag-group-label">{T("Manuais")}</span>{manual_chips}</div>
      <form method="post" action="/contato/{_e(phone)}/tag" class="inline-form">
        <input class="search inline-input" type="text" name="tag" placeholder="{T("Nova tag...")}" maxlength="40" required>
        <button class="btn btn-ghost" type="submit">{T("Adicionar")}</button>
      </form>
    </div>
  </section>

  <section>
    <h2>{T("Sinais de qualificacao")}</h2>
    <div class="panel-box"><div class="signals">{signals_html}</div></div>
  </section>

  <section>
    <h2>{T("Notas")}</h2>
    <div class="panel-box">
      <form method="post" action="/contato/{_e(phone)}/nota" class="inline-form">
        <input class="search inline-input" type="text" name="nota" placeholder="{T("Escrever uma nota...")}" maxlength="300" required>
        <button class="btn btn-ghost" type="submit">{T("Adicionar nota")}</button>
      </form>
      <div class="notes-list">{notes_list}</div>
    </div>
  </section>

  <section class="grid-full">
    <h2>{T("Conversa")}</h2>
    <div class="timeline">{_timeline_html(lead)}</div>
  </section>
  </div>"""
    return _page(lead["nome"] or phone, T("Detalhe do contato"), "painel", body)


def _fmt_day(day_ts):
    return (datetime.utcfromtimestamp(day_ts) - timedelta(hours=3)).strftime("%d/%m")


def _daily_sends_chart():
    data = lead_store.sends_by_day()
    if not data:
        return ('<div class="empty-state">'
                + T("Nenhum envio ainda. O grafico aparece aqui quando a campanha comecar a enviar.")
                + '</div>')
    maxv = max(v for _, v in data) or 1
    cols = ""
    for day_ts, v in data:
        h = max(4, round(v / maxv * 100))
        cols += (f'<div class="col"><span class="col-val">{v}</span>'
                 f'<div class="col-bar" style="height:{h}%"></div>'
                 f'<span class="col-x">{_fmt_day(day_ts)}</span></div>')
    return f'<div class="colchart-wrap"><div class="colchart">{cols}</div></div>'


def _render_campaign():
    s = scheduler.status_summary()
    sending = s["status"] == "running" and s["remaining"] > 0
    if sending:
        chip, label = "chip-good", T("Enviando &middot; faltam {n}", n=s["remaining"])
    elif s["status"] == "paused":
        chip, label = "chip-info", T("Pausada")
    else:
        chip, label = "chip-muted", T("Parada")
    warn = ""
    if s["status"] == "paused" and s["fail_streak"] >= scheduler.MAX_FAIL_STREAK:
        warn = ('<div class="alert alert-bad">'
                + T("Envio pausado automaticamente apos varias falhas seguidas. Confirme os modelos aprovados e o token antes de enviar de novo.")
                + '</div>')
    metrics = T("Total enviados {a} &middot; Pendentes {b}", a=s["total_enviados"], b=s["pendentes"])
    # while sending, show a stop button; otherwise the manual "send N" form
    if sending:
        control = ('<form method="post" action="/campanha/parar">'
                   f'<button class="btn btn-ghost" type="submit">{T("Parar envio")}</button></form>')
    else:
        maxq = max(s["pendentes"], 1)
        default_q = min(20, maxq)
        control = (
            '<form method="post" action="/campanha/enviar" class="send-form">'
            f'<input class="qty-input" type="number" name="quantidade" min="1" max="{maxq}" '
            f'value="{default_q}" title="{T("Quantos contatos enviar agora")}">'
            f'<button class="btn btn-primary" type="submit">{T("Enviar agora")}</button>'
            '</form>')
    progress = ""
    if sending and s["total"]:
        pct = int((s["total"] - s["remaining"]) / s["total"] * 100)
        progress = (f'<div class="camp-progress"><div class="camp-progress-bar" id="camp-progress-bar" '
                    f'style="width:{pct}%"></div></div>')
    return f"""
  <section>
    <h2>{T("Campanha")}{_info_btn('campanha')}</h2>
    {warn}
    <div class="campaign-box" id="campaign-box" data-sending="{'1' if sending else '0'}">
      <div class="campaign-top">
        <div class="campaign-info">
          <span class="chip {chip}" id="camp-chip">{label}</span>
          <span class="campaign-metrics" id="camp-metrics">{metrics}</span>
        </div>
        {control}
      </div>
      {progress}
    </div>
  </section>"""


def _panel_sections():
    counts = lead_store.funnel_counts()
    deliv = lead_store.delivery_counts()
    hot = lead_store.hot_leads()
    leads = sorted(lead_store.all_leads(), key=lambda l: l.get("nome") or "")
    total = counts.get("total", 0)

    funnel_rows = ""
    prev_count = total
    ramp_steps = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab"]
    for i, (key, label) in enumerate(FUNNEL_STAGES):
        c = counts.get(key, 0)
        width_pct = (c / total * 100) if total else 0
        color = ramp_steps[min(i, len(ramp_steps) - 1)]
        conv = T(" &middot; {p} da etapa anterior", p=_fmt_pct(c, prev_count)) if i > 0 else ""
        funnel_rows += f"""
        <div class="funnel-row">
          <div class="funnel-label">{T(label)}{_info_btn(key)}</div>
          <div class="funnel-track"><div class="funnel-bar" style="width:{max(width_pct, 2):.1f}%; background:{color}"></div></div>
          <div class="funnel-value">{c} <span class="funnel-pct">({_fmt_pct(c, total)}{conv})</span></div>
        </div>"""
        prev_count = c

    # conversion rates
    sent = deliv["enviado"] + deliv["entregue"] + deliv["lido"] + deliv["respondeu"]
    delivered = deliv["entregue"] + deliv["lido"] + deliv["respondeu"]
    read = deliv["lido"] + deliv["respondeu"]
    responded = deliv["respondeu"]
    quente_n = counts.get("quente", 0)
    rate_tiles = "".join(
        f'<div class="tile"><div class="tile-num">{_fmt_pct(n, d)}</div>'
        f'<div class="tile-label">{T(lbl)}{_info_btn(key)}</div><div class="tile-sub">{T("{n} de {d}", n=n, d=d)}</div></div>'
        for n, d, lbl, key in [
            (delivered, sent, "Taxa de entrega", "taxa_entrega"),
            (read, sent, "Taxa de leitura", "taxa_leitura"),
            (responded, sent, "Taxa de resposta", "taxa_resposta"),
            (quente_n, responded, "Taxa de qualificacao", "taxa_qualificacao")]
    )

    cards = ""
    for lead in hot:
        s = lead["signals"]
        lead_messages = [h for h in lead["history"] if h["role"] == "lead"]
        ultima = lead_messages[-1]["text"] if lead_messages else ""
        cards += f"""
        <div class="card">
          <div class="card-head">
            <span class="name">{_e(lead['nome'] or T('(sem nome)'))}</span>
            <span class="badge-row">
              <span class="badge">{_e(lead['perfil'])}</span>
              <span class="chip chip-good">&#9679; {T("QUENTE")}</span>
            </span>
          </div>
          <div class="phone">{_e(lead['phone'])}{f" &middot; {_e(lead['email'])}" if lead.get('email') else ""}</div>
          <div class="signals">
            <div><span class="signal-label">{T("Objetivo")}</span>{_e(s['objetivo'] or '-')}</div>
            <div><span class="signal-label">{T("Experiencia")}</span>{_e(s['experiencia'] or '-')}</div>
            <div><span class="signal-label">{T("Forma de pagamento")}</span>{_e(s['forma_pagamento'] or '-')}</div>
            <div><span class="signal-label">{T("Unidades")}</span>{_e(s['quantidade_unidades'] or '-')}</div>
            <div><span class="signal-label">{T("Timing")}</span>{_e(s['timing'] or '-')}</div>
          </div>
          {f'<div class="last-msg">&ldquo;{_e(ultima)}&rdquo;</div>' if ultima else ''}
        </div>"""
    if not hot:
        cards = ('<div class="empty-state">'
                 + T("Nenhum lead quente ainda. Assim que um investidor esquentar, aparece aqui.")
                 + '</div>')

    tmap = lead_store.tags_map()

    def _kanban_card(lead):
        phone = lead["phone"]
        email = lead.get("email") or ""
        alltags = _auto_tags(lead) + tmap.get(phone, [])
        haystack = _e(f"{lead.get('nome','')} {phone} {email} {lead.get('pais','')} {' '.join(alltags)}").lower()
        chips = "".join(f'<span class="chip chip-muted mini">{_e(t)}</span>' for t in alltags[:3])
        if len(alltags) > 3:
            chips += f'<span class="tag-more">+{len(alltags) - 3}</span>'
        email_line = f'<div class="kanban-email">{_e(email)}</div>' if email else ""
        # The delivery chip is only worth showing once something has been sent.
        # A "pendente" delivery just repeats the Pendentes column header, so skip it.
        deliv = lead.get("delivery", "pendente")
        err = lead.get("last_error") or ""
        foot = ""
        if deliv != "pendente":
            reason = f'<div class="kanban-error">{_e(err)}</div>' if (deliv == "falhou" and err) else ""
            foot = f'<div class="kanban-foot">{_delivery_chip(deliv, err if deliv == "falhou" else None)}</div>{reason}'
        return f"""
        <div class="kanban-card" draggable="true" data-phone="{_e(phone)}" data-search="{haystack}">
          <a class="contact-link" draggable="false" href="/contato/{_e(phone)}">{_e(lead.get('nome') or T('(sem nome)'))}</a>
          <div class="row-tags">{chips}</div>
          <div class="kanban-phone">{_e(phone)}</div>
          {email_line}
          {foot}
        </div>"""

    by_stage = {}
    for lead in leads:
        by_stage.setdefault(lead.get("stage"), []).append(lead)

    cols_html = ""
    for key, label, cls in BOARD_COLUMNS:
        col_leads = by_stage.get(key, [])
        inner = "".join(_kanban_card(l) for l in col_leads) or f'<div class="board-empty">{T("Vazio")}</div>'
        cols_html += f"""
        <div class="board-col {cls}">
          <div class="board-col-head"><span class="board-col-title">{T(label)}{_info_btn(key)}</span><span class="board-col-count">{len(col_leads)}</span></div>
          <div class="board-col-body" data-stage="{key}">{inner}</div>
        </div>"""

    if not leads:
        board_section = ('<div class="empty-state">'
                         + T("Nenhum contato ainda. Importe uma planilha para comecar.")
                         + '</div>')
    else:
        board_section = f"""
        <input class="search" type="search" placeholder="{T("Buscar por nome, telefone, email ou tag...")}" oninput="filterRows(this.value)">
        <div class="board">{cols_html}</div>"""

    conta_map = {
        "ok": ("good", "Senha alterada com sucesso."),
        "atual": ("bad", "Senha atual incorreta."),
        "curta": ("bad", "A nova senha precisa ter ao menos 6 caracteres."),
        "match": ("bad", "A nova senha e a confirmacao nao conferem."),
    }
    cm = conta_map.get(request.args.get("conta"))
    toast = f'<div class="alert alert-{cm[0]}">{T(cm[1])}</div>' if cm else ""

    return toast + _render_campaign() + f"""
  <section><h2>{T("Funil")}</h2><div class="funnel">{funnel_rows}</div></section>
  <section><h2>{T("Taxas de conversao")}</h2><div class="tiles">{rate_tiles}</div></section>
  <section><h2>{T("Envios por dia")}</h2>{_daily_sends_chart()}</section>
  <section><h2>{T("Leads quentes ({n})", n=len(hot))}</h2>{cards}</section>
  <section><h2>{T("Contatos por status ({n})", n=len(leads))}</h2>{board_section}</section>
  """ + _INFO_MODAL_HTML + _deliv_modal_html()


def _render_panel_html():
    return _page("Painel Guerra Cyrela", T("Painel do piloto de reativacao"), "painel",
                 _panel_sections() + _board_js() + _LIVE_JS)


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
        return f'<div class="empty-state">{T("Nenhuma execucao ainda.")}</div>'
    rows = "".join(
        f"""<div class="hist-row">
          <span class="hist-when">{_e(it.get('quando'))}</span>
          <span class="chip {'chip-good' if it.get('acao') == 'Importacao' else 'chip-info'}">{_e(T(it.get('acao') or ''))}</span>
          <span class="hist-detail">{_e(it.get('detalhe'))}</span>
        </div>""" for it in items
    )
    return f'<div class="hist-list">{rows}</div>'


def _render_import_form(erro=None):
    alert = f'<div class="alert alert-bad">{_e(erro)}</div>' if erro else ""
    body = f"""
  <section>
    <h2>{T("Importar planilha de contatos")}</h2>
    {alert}
    <div class="panel-box">
      <p class="muted-text">{T("Envie a planilha (.xlsx) com as colunas nome, telefone e email. A gente analisa, remove duplicados e mostra um resumo antes de importar de verdade.")}</p>
      <form id="import-form" method="post" action="/importar/analisar" enctype="multipart/form-data">
        <input id="file-input" type="file" name="arquivo" accept=".xlsx" hidden>
        <div id="dropzone" class="dropzone" onclick="document.getElementById('file-input').click()">
          <div class="dz-icon">{_UPLOAD_ICON}</div>
          <div class="dz-title">{T("Arraste a planilha aqui ou clique para selecionar")}</div>
          <div class="dz-hint">{T("Apenas arquivos .xlsx")}</div>
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
          <button type="button" class="file-remove" onclick="cancelUpload()" title="{T("Cancelar")}">&times;</button>
        </div>
        <div id="file-error" class="alert alert-bad" hidden></div>
        <div class="form-actions">
          <button id="submit-btn" class="btn btn-primary" type="submit" disabled>{T("Analisar planilha")}</button>
        </div>
      </form>
    </div>
  </section>
  <section>
    <h2>{T("Historico de execucoes")}</h2>
    <div id="history-live">{_render_history()}</div>
  </section>""" + _import_js() + _IMPORT_LIVE_JS
    return _page(T("Importar base"), T("Importacao de contatos"), "importar", body)


def _render_import_review(token, a):
    n_clean = len(a["clean"])
    n_intl = len(a["internacionais"])
    tiles = "".join(
        f'<div class="tile"><div class="tile-num">{n}</div><div class="tile-label">{T(lbl)}</div></div>'
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
        f'<div class="table-wrap"><table><thead><tr><th>{T("Pais")}</th><th>{T("Contatos")}</th></tr></thead>'
        f"<tbody>{country_rows}</tbody></table></div>" if country_rows else ""
    )
    body = f"""
  <section>
    <h2>{T("Revisao da base")}</h2>
    <div class="tiles">{tiles}</div>
  </section>
  <section>
    <h2>{T("Confirmar importacao")}</h2>
    <div class="panel-box">
      <p class="muted-text">{T("Serao importados <b>{n}</b> contatos do Brasil. Os {i} internacionais podem entrar junto (investidores de fora que compram em SP).", n=n_clean, i=n_intl)}</p>
      {country_block}
      <form method="post" action="/importar/confirmar">
        <input type="hidden" name="token" value="{_e(token)}">
        <label class="checkbox"><input type="checkbox" name="incluir_intl" value="sim" checked> {T("Incluir os {i} contatos internacionais", i=n_intl)}</label>
        <button class="btn btn-primary" type="submit">{T("Confirmar e importar")}</button>
        <a class="btn btn-ghost" href="/importar">{T("Cancelar")}</a>
      </form>
    </div>
  </section>"""
    return _page(T("Revisao da base"), T("Importacao de contatos"), "importar", body)


def _render_import_done(imported, skipped, include_intl, n_intl):
    intl_note = T(" (incluindo {i} internacionais)", i=n_intl) if include_intl else ""
    body = f"""
  <section>
    <h2>{T("Importacao concluida")}</h2>
    <div class="panel-box">
      <div class="alert alert-good">{T("{n} contatos importados{note}.", n=imported, note=intl_note)}</div>
      <p class="muted-text">{T("{s} ja existiam na base e foram mantidos como estavam, sem sobrescrever conversas em andamento.", s=skipped)}</p>
      <a class="btn btn-primary" href="/painel">{T("Ver o painel")}</a>
      <a class="btn btn-ghost" href="/importar">{T("Importar outra planilha")}</a>
    </div>
  </section>"""
    return _page(T("Importacao concluida"), T("Importacao de contatos"), "importar", body)


if __name__ == "__main__":
    scheduler.start_background()
    # Bind to localhost so the app is reachable only through nginx (the HTTPS
    # domain), not directly on the raw IP:8000. Override with BIND_HOST if needed.
    # threaded=True so a held-open SSE stream never blocks other requests.
    app.run(host=os.environ.get("BIND_HOST", "127.0.0.1"), port=8000, threaded=True)
