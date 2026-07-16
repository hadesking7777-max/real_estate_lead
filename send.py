"""
Graph API send helpers, shared by the webhook server and manual scripts.
Reads PHONE_NUMBER_ID and WHATSAPP_TOKEN from the environment, never hardcode.
"""

import os
import requests

GRAPH_VERSION = "v20.0"


def _url():
    phone_number_id = os.environ["PHONE_NUMBER_ID"]
    return f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}/messages"


def _headers():
    return {
        "Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN']}",
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
