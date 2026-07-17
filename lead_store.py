"""
Local JSON-backed lead state store for the pilot. At 295 contacts this is
plenty; swap for a real database only if the base grows past a pilot scale.
"""

import json
import os
import threading

STORE_PATH = os.path.join(os.path.dirname(__file__), "leads.json")
_lock = threading.Lock()

STAGES = ["pendente", "contatado", "respondeu", "qualificando", "quente", "morno", "frio", "opt_out"]

# WhatsApp delivery lifecycle. "pendente" = imported but not yet sent to.
DELIVERY_STATES = ["pendente", "enviado", "entregue", "lido", "respondeu", "falhou"]
_DELIVERY_RANK = {s: i for i, s in enumerate(DELIVERY_STATES)}


def _digits(phone):
    import re
    return re.sub(r"\D", "", str(phone))


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


def _new_lead(phone, nome=None, perfil="PF", origem="Brasil", pais="Brasil",
              stage="contatado", delivery="enviado"):
    return {
        "phone": phone,
        "nome": nome or "",
        "perfil": perfil,
        "origem": origem,
        "pais": pais,
        "stage": stage,
        "delivery": delivery,
        "history": [],
        "signals": {
            "objetivo": None,
            "experiencia": None,
            "forma_pagamento": None,
            "quantidade_unidades": None,
            "timing": None,
        },
        "last_template_used": None,
        "last_wamid": None,
        "followup_count": 0,
    }


def get_or_create_lead(phone, nome=None, perfil="PF"):
    with _lock:
        data = _load()
        if phone not in data:
            data[phone] = _new_lead(phone, nome=nome, perfil=perfil)
            _save(data)
        return data[phone]


def import_contacts(contacts):
    """
    Bulk-import contacts from the analyzed base. Each contact is a dict with
    telefone_e164, nome, perfil, origem, pais. Sets stage/delivery to
    'pendente' (imported, not yet contacted). Skips phones already present so
    re-importing is safe and never overwrites live conversation state.
    Returns (imported, skipped).
    """
    with _lock:
        data = _load()
        imported, skipped = 0, 0
        for c in contacts:
            phone = _digits(c["telefone_e164"])
            if phone in data:
                skipped += 1
                continue
            data[phone] = _new_lead(
                phone,
                nome=c.get("nome", ""),
                perfil=("PJ" if str(c.get("perfil", "")).startswith("PJ") else "PF"),
                origem=c.get("origem", "Brasil"),
                pais=c.get("pais", "Brasil"),
                stage="pendente",
                delivery="pendente",
            )
            imported += 1
        _save(data)
        return imported, skipped


def advance_delivery(phone, new_state):
    """
    Move a lead's delivery status forward only (never regress lido->entregue).
    'respondeu' and 'falhou' are terminal-ish and always win over transit states.
    Silently ignores unknown phones (status webhooks can arrive for numbers not
    in our base, e.g. test sends).
    """
    if new_state not in _DELIVERY_RANK:
        return None
    with _lock:
        data = _load()
        lead = data.get(phone)
        if not lead:
            return None
        current = lead.get("delivery", "pendente")
        if _DELIVERY_RANK.get(new_state, 0) > _DELIVERY_RANK.get(current, 0):
            lead["delivery"] = new_state
            _save(data)
        return lead


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


def delivery_counts():
    leads = all_leads()
    counts = {s: 0 for s in DELIVERY_STATES}
    for lead in leads:
        d = lead.get("delivery", "pendente")
        counts[d] = counts.get(d, 0) + 1
    return counts


def hot_leads():
    return [l for l in all_leads() if l["stage"] == "quente"]
