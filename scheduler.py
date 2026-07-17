"""
Automation engine for the reactivation campaign.

Runs as a daemon thread inside the web process. Each tick sends AT MOST one
WhatsApp message, gated by three rules so the number stays healthy:
  1. Warming ramp    - a per-day cap on total sends (day 1: 20, 2: 30, ... ).
  2. Pacing gap      - a minimum interval between any two sends.
  3. Auto follow-ups - non-responders get follow-up 1, then 2, after delays.
Openers and follow-ups share the daily cap (total volume is what matters for
blocks). Priority per tick: follow-up 2 > follow-up 1 > new opener.

The core is tick(now, sender): a pure function that reads state, performs one
action, and writes state, so it can be unit-tested with an injected clock and
a fake sender. The background thread just calls tick() on an interval.
"""

import logging
import os
import threading
import time

import lead_store
import send

# --- config (env-overridable) ---
RAMP = {1: 20, 2: 30, 3: 45, 4: 60, 5: 80}          # per-day send cap
MAX_DAILY = int(os.environ.get("MAX_DAILY", "100"))  # cap for day 6 onward
SEND_GAP_SECONDS = int(os.environ.get("SEND_GAP_SECONDS", "120"))
FOLLOWUP1_DELAY = int(os.environ.get("FOLLOWUP1_DELAY", str(2 * 86400)))
FOLLOWUP2_DELAY = int(os.environ.get("FOLLOWUP2_DELAY", str(4 * 86400)))
TICK_INTERVAL = int(os.environ.get("TICK_INTERVAL", "30"))
MAX_FAIL_STREAK = int(os.environ.get("MAX_FAIL_STREAK", "5"))

PF_OPENERS = [f"reativacao_pf_faria_lima_v{i}" for i in range(1, 7)]
PJ_OPENERS = ["reativacao_pj_faria_lima_v1", "reativacao_pj_faria_lima_v2"]
FOLLOWUP1 = ["followup1_faria_lima_a", "followup1_faria_lima_b"]
FOLLOWUP2 = ["followup2_faria_lima_a", "followup2_faria_lima_b"]

_SP_OFFSET = 3 * 3600  # Sao Paulo = UTC-3, no DST


def _sp_day_start_ts(now_ts):
    """Epoch of the most recent Sao Paulo midnight at or before now_ts."""
    shifted = now_ts - _SP_OFFSET
    return (shifted - (shifted % 86400)) + _SP_OFFSET


def current_day(start_ts, now_ts):
    days = round((_sp_day_start_ts(now_ts) - _sp_day_start_ts(start_ts)) / 86400)
    return int(days) + 1


def ramp_target(day):
    return RAMP.get(day, MAX_DAILY)


def _first_name(nome):
    nome = (nome or "").strip()
    return nome.split(" ")[0] if nome else nome


def _pick_template(lead, kind):
    phone = lead["phone"]
    idx = int(phone) if phone.isdigit() else sum(map(ord, phone))
    if kind == "opener":
        pool = PJ_OPENERS if str(lead.get("perfil", "")).startswith("PJ") else PF_OPENERS
    elif kind == "followup1":
        pool = FOLLOWUP1
    else:
        pool = FOLLOWUP2
    return pool[idx % len(pool)]


def _wamid(resp_text):
    import json
    try:
        return json.loads(resp_text)["messages"][0]["id"]
    except Exception:  # noqa: BLE001
        return None


def _apply_success(lead, kind, template, resp_text, now):
    wamid = _wamid(resp_text)
    if kind == "opener":
        lead_store.update_lead(lead["phone"], stage="contatado", delivery="enviado",
                               last_template_used=template, last_wamid=wamid, last_send_ts=now)
    else:
        lead_store.update_lead(lead["phone"], last_template_used=template, last_wamid=wamid,
                               last_send_ts=now, followup_count=(1 if kind == "followup1" else 2))
    lead_store.append_history(lead["phone"], "bot", f"[{kind}:{template}]")


def tick(now=None, sender=None):
    """Perform at most one send. Returns a summary dict (or None if idle/paused)."""
    now = time.time() if now is None else now
    sender = sender or send.send_template

    camp = lead_store.get_campaign()
    if camp["status"] != "running" or camp["start_ts"] is None:
        return None

    last = lead_store.last_send_ts()
    if last is not None and (now - last) < SEND_GAP_SECONDS:
        return {"action": "wait", "reason": "gap"}

    day = current_day(camp["start_ts"], now)
    target = ramp_target(day)
    sent_today = lead_store.count_ok_sends_between(_sp_day_start_ts(now), now + 1)
    if sent_today >= target:
        return {"action": "wait", "reason": "daily_cap", "sent_today": sent_today, "target": target}

    fu = lead_store.due_followup(now, FOLLOWUP1_DELAY, FOLLOWUP2_DELAY)
    if fu:
        lead, kind = fu
    else:
        lead, kind = lead_store.next_pending_opener(), "opener"
    if not lead:
        return {"action": "idle", "reason": "nothing_due"}

    template = _pick_template(lead, kind)
    status, resp = sender(lead["phone"], template, _first_name(lead.get("nome")))
    ok = status in (200, 201)
    lead_store.record_send(lead["phone"], kind, template, now, ok)

    if ok:
        _apply_success(lead, kind, template, resp, now)
        lead_store.set_campaign(fail_streak=0)
        return {"action": "sent", "kind": kind, "phone": lead["phone"], "template": template}

    lead_store.advance_delivery(lead["phone"], "falhou")
    streak = (camp["fail_streak"] or 0) + 1
    fields = {"fail_streak": streak}
    if streak >= MAX_FAIL_STREAK:
        fields["status"] = "paused"
    lead_store.set_campaign(**fields)
    return {"action": "failed", "kind": kind, "phone": lead["phone"], "status": status, "streak": streak}


def status_summary(now=None):
    now = time.time() if now is None else now
    camp = lead_store.get_campaign()
    counts = lead_store.funnel_counts()
    total = counts.get("total", 0)
    pendente = counts.get("pendente", 0)
    day = current_day(camp["start_ts"], now) if camp["start_ts"] else 0
    sent_today = (lead_store.count_ok_sends_between(_sp_day_start_ts(now), now + 1)
                  if camp["start_ts"] else 0)
    return {
        "status": camp["status"],
        "day": day,
        "target": ramp_target(day) if day else 0,
        "sent_today": sent_today,
        "total_enviados": total - pendente,
        "pendentes": pendente,
        "fail_streak": camp["fail_streak"] or 0,
    }


# --- controls ---

def start_campaign():
    camp = lead_store.get_campaign()
    if camp["status"] == "idle" or camp["start_ts"] is None:
        return lead_store.set_campaign(status="running", start_ts=time.time(), fail_streak=0)
    return lead_store.set_campaign(status="running", fail_streak=0)  # resume


def pause_campaign():
    return lead_store.set_campaign(status="paused")


# --- background thread ---

_thread = None


def _run_loop():
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001
            logging.exception("scheduler tick failed")
        time.sleep(TICK_INTERVAL)


def start_background():
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_run_loop, name="campaign-scheduler", daemon=True)
    _thread.start()
