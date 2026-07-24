"""
Graph API send helpers, shared by the webhook server and manual scripts.
Reads PHONE_NUMBER_ID and WHATSAPP_TOKEN from the settings UI (Configuracoes,
stored in the DB) when set there, otherwise falls back to the environment --
never hardcode.
"""

import os
import requests

import lead_store

GRAPH_VERSION = "v20.0"


def _cfg(key):
    val = lead_store.get_setting(key)
    return val if val else os.environ.get(key, "")


def _url():
    phone_number_id = _cfg("PHONE_NUMBER_ID")
    return f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}/messages"


def _headers():
    return {
        "Authorization": f"Bearer {_cfg('WHATSAPP_TOKEN')}",
        "Content-Type": "application/json",
    }


def send_text(to, body):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    resp = requests.post(_url(), headers=_headers(), json=payload, timeout=30)
    return resp.status_code, resp.text


def check_health():
    """GET the phone number's own status + quality rating (not a message send).
    Used by scheduler.py's periodic health check so a degrading/banned number
    can be caught even when nothing is actively being sent to trigger a failure.
    Returns (status_code, response_text); status_code is 0 on a network-level
    failure (no response at all), so callers can treat that the same way.
    """
    phone_number_id = _cfg("PHONE_NUMBER_ID")
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}"
    try:
        resp = requests.get(url, headers=_headers(),
                            params={"fields": "status,quality_rating,verified_name"}, timeout=15)
        return resp.status_code, resp.text
    except requests.RequestException as e:
        return 0, str(e)


def send_template(to, template_name, name_param, language="pt_BR"):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": name_param}],
                }
            ],
        },
    }
    resp = requests.post(_url(), headers=_headers(), json=payload, timeout=30)
    return resp.status_code, resp.text
