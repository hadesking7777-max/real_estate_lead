"""
Local JSON-backed lead state store for the pilot. At 295 contacts this is
plenty; swap for a real database only if the base grows past a pilot scale.
"""

import json
import os
import threading

STORE_PATH = os.path.join(os.path.dirname(__file__), "leads.json")
_lock = threading.Lock()

STAGES = ["contatado", "respondeu", "qualificando", "quente", "morno", "frio", "opt_out"]


def _load():
    if not os.path.exists(STORE_PATH):
        return {}
    with open(STORE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_lead(phone):
    with _lock:
        data = _load()
        return data.get(phone)


def get_or_create_lead(phone, nome=None, perfil="PF"):
    with _lock:
        data = _load()
        if phone not in data:
            data[phone] = {
                "phone": phone,
                "nome": nome or "",
                "perfil": perfil,
                "stage": "contatado",
                "history": [],
                "signals": {
                    "objetivo": None,
                    "experiencia": None,
                    "forma_pagamento": None,
                    "quantidade_unidades": None,
                    "timing": None,
                },
                "last_template_used": None,
                "followup_count": 0,
            }
            _save(data)
        return data[phone]


def update_lead(phone, **fields):
    with _lock:
        data = _load()
        if phone not in data:
            raise KeyError(f"Lead {phone} not found, call get_or_create_lead first")
        data[phone].update(fields)
        _save(data)
        return data[phone]


def append_history(phone, role, text):
    with _lock:
        data = _load()
        lead = data[phone]
        lead["history"].append({"role": role, "text": text})
        _save(data)
        return lead


def set_stage(phone, stage):
    if stage not in STAGES:
        raise ValueError(f"Unknown stage: {stage}")
    return update_lead(phone, stage=stage)


def all_leads():
    with _lock:
        return list(_load().values())


def funnel_counts():
    leads = all_leads()
    counts = {stage: 0 for stage in STAGES}
    for lead in leads:
        counts[lead["stage"]] = counts.get(lead["stage"], 0) + 1
    counts["total"] = len(leads)
    return counts


def hot_leads():
    return [l for l in all_leads() if l["stage"] == "quente"]
