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


def send_hot_lead_alert(lead):
    """Send a best-effort email alert for a lead that just reached 'quente'.
    Returns True if actually sent, False otherwise (including "not configured
    yet" -- callers should not treat False as an error worth logging loudly).
    """
    to_addr = _cfg("ALERT_EMAIL_TO")
    smtp_user = _cfg("SMTP_USER")
    smtp_password = _cfg("SMTP_PASSWORD")
    if not (to_addr and smtp_user and smtp_password):
        return False

    nome = lead.get("nome") or lead["phone"]
    signals = lead.get("signals") or {}
    lines = [f"{nome} ({lead['phone']}) acabou de esquentar."]
    for key, label in SIGNAL_LABELS:
        if signals.get(key):
            lines.append(f"{label}: {signals[key]}")
    lines.append("")
    lines.append("Veja a conversa completa no painel.")

    msg = MIMEText("\n".join(lines), _charset="utf-8")
    msg["Subject"] = f"Lead quente: {nome}"
    msg["From"] = smtp_user
    msg["To"] = to_addr

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, [to_addr], msg.as_string())
        return True
    except Exception:  # noqa: BLE001 -- alerting must never break the reply flow
        return False
