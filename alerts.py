"""
Email alerts for operationally important events (currently: a lead reaching
'quente'). Credentials come from the settings table (Configuracoes page),
same as WhatsApp/Claude credentials. Best-effort by design: a broken or
unconfigured mail setup must never block the WhatsApp reply flow, so every
failure here is swallowed, not raised.
"""

import smtplib
from email.mime.text import MIMEText

import lead_store

SIGNAL_LABELS = [
    ("objetivo", "Objetivo"),
    ("experiencia", "Experiencia"),
    ("forma_pagamento", "Forma de pagamento"),
    ("quantidade_unidades", "Unidades"),
    ("timing", "Timing"),
]


def _cfg(key):
    return (lead_store.get_setting(key) or "").strip()


def configured():
    return bool(_cfg("ALERT_EMAIL_TO") and _cfg("SMTP_USER") and _cfg("SMTP_PASSWORD"))


def _send_email(subject, body):
    """Shared best-effort send used by every alert type below. Returns True if
    actually sent, False otherwise (including "not configured yet" -- callers
    should not treat False as an error worth logging loudly).
    """
    to_addr = _cfg("ALERT_EMAIL_TO")
    smtp_user = _cfg("SMTP_USER")
    smtp_password = _cfg("SMTP_PASSWORD")
    if not (to_addr and smtp_user and smtp_password):
        return False

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, [to_addr], msg.as_string())
        return True
    except Exception:  # noqa: BLE001 -- alerting must never break the reply flow
        return False


def send_hot_lead_alert(lead):
    """Send a best-effort email alert for a lead that just reached 'quente'."""
    nome = lead.get("nome") or lead["phone"]
    signals = lead.get("signals") or {}
    lines = [f"{nome} ({lead['phone']}) acabou de esquentar."]
    for key, label in SIGNAL_LABELS:
        if signals.get(key):
            lines.append(f"{label}: {signals[key]}")
    lines.append("")
    lines.append("Veja a conversa completa no painel.")
    return _send_email(f"Lead quente: {nome}", "\n".join(lines))


def send_health_alert(prev_status, new_status, quality_rating, detail=None):
    """Best-effort alert for a change in the WhatsApp number's connection
    status (Graph API's `status` field on the phone number), checked
    periodically by scheduler.py. Fires on any status change, whether that's
    degrading (CONNECTED -> FLAGGED/RESTRICTED/BANNED/OFFLINE) or recovering
    back to CONNECTED, so a silent ban is caught fast instead of only being
    noticed once every automated send has been failing for a while.
    """
    healthy = new_status == "CONNECTED"
    if healthy:
        lines = [f"O numero do WhatsApp voltou ao status normal (CONNECTED), depois de estar '{prev_status}'."]
    else:
        lines = [
            f"O numero do WhatsApp mudou de status: '{prev_status}' -> '{new_status}'.",
            "Isso normalmente indica qualidade baixa, limite de envio atingido, ou o numero "
            "restringido/banido pela Meta.",
        ]
    if quality_rating:
        lines.append(f"Quality rating atual: {quality_rating}")
    if detail:
        lines.append(f"Detalhe: {detail}")
    lines.append("")
    lines.append("Confira o WhatsApp Manager para mais detalhes.")
    subject = "WhatsApp OK novamente" if healthy else f"Alerta: status do WhatsApp mudou para {new_status}"
    return _send_email(subject, "\n".join(lines))


def send_campaign_paused_alert(fail_streak, last_error=None):
    """Best-effort alert fired the moment scheduler.py auto-pauses the campaign
    after MAX_FAIL_STREAK consecutive send failures -- usually a bad/expired
    token, a newly restricted number, or a template that stopped being approved.
    """
    lines = [f"O envio automatico foi pausado depois de {fail_streak} falhas seguidas."]
    if last_error:
        lines.append(f"Ultimo erro: {last_error}")
    lines.append("")
    lines.append("Confira o token do WhatsApp e os modelos aprovados antes de retomar "
                 "(Enviar agora ou Ligar piloto automatico).")
    return _send_email("Alerta: envio pausado automaticamente", "\n".join(lines))


def send_weekly_summary(counts, sent_count, new_hot_count, status_label):
    """Best-effort digest of what happened since the last summary: sends made,
    new hot leads, and a snapshot of the full funnel plus the campaign's
    current status.
    """
    lines = [
        f"Desde o ultimo resumo: {sent_count} mensagens enviadas, {new_hot_count} novos leads quentes.",
        "",
        "Funil atual:",
        f"Total de contatos: {counts.get('total', 0)}",
        f"Pendentes: {counts.get('pendente', 0)}",
        f"Contatados: {counts.get('contatado', 0)}",
        f"Responderam: {counts.get('respondeu', 0)}",
        f"Em qualificacao: {counts.get('qualificando', 0)}",
        f"Quentes: {counts.get('quente', 0)}",
        f"Mornos: {counts.get('morno', 0)}",
        f"Frios: {counts.get('frio', 0)}",
        f"Opt-out: {counts.get('opt_out', 0)}",
        "",
        f"Status da campanha: {status_label}",
        "",
        "Veja os detalhes completos no painel.",
    ]
    return _send_email("Resumo semanal do painel", "\n".join(lines))
