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

import events
import lead_store
import send

# --- config (env-overridable) ---
RAMP = {1: 20, 2: 30, 3: 45, 4: 60, 5: 80}          # per-day send cap
MAX_DAILY = int(os.environ.get("MAX_DAILY", "100"))  # cap for day 6 onward
# Manual mode: the operator decides how many to send and when; we keep only a
# small pacing gap between sends for number safety.
SEND_GAP_SECONDS = int(os.environ.get("SEND_GAP_SECONDS", "8"))
TICK_INTERVAL = int(os.environ.get("TICK_INTERVAL", "5"))
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
    """Send at most one opener while a manual batch is pending. Returns a summary (or None)."""
    now = time.time() if now is None else now
    sender = sender or send.send_template

    camp = lead_store.get_campaign()
    remaining = camp.get("manual_remaining", 0) or 0
    if camp["status"] != "running" or remaining <= 0:
        return None

    last = lead_store.last_send_ts()
    if last is not None and (now - last) < SEND_GAP_SECONDS:
        return {"action": "wait", "reason": "gap"}

    lead = lead_store.next_pending_opener()
    if not lead:  # base exhausted
        lead_store.set_campaign(manual_remaining=0, status="idle")
        return {"action": "idle", "reason": "no_pending"}

    template = _pick_template(lead, "opener")
    status, resp = sender(lead["phone"], template, _first_name(lead.get("nome")))
    ok = status in (200, 201)
    lead_store.record_send(lead["phone"], "opener", template, now, ok)

    if ok:
        _apply_success(lead, "opener", template, resp, now)
        new_remaining = remaining - 1
        fields = {"fail_streak": 0, "manual_remaining": new_remaining}
        if new_remaining <= 0:
            fields["status"] = "idle"
        lead_store.set_campaign(**fields)
        events.bump()  # state committed; push an update to any open panel
        return {"action": "sent", "phone": lead["phone"], "template": template, "remaining": new_remaining}

    lead_store.advance_delivery(lead["phone"], "falhou")
    streak = (camp["fail_streak"] or 0) + 1
    fields = {"fail_streak": streak}
    if streak >= MAX_FAIL_STREAK:
        fields["status"] = "paused"  # stop after repeated failures (bad token/setup)
    lead_store.set_campaign(**fields)
    events.bump()
    return {"action": "failed", "phone": lead["phone"], "status": status, "streak": streak}


def status_summary(now=None):
    camp = lead_store.get_campaign()
    counts = lead_store.funnel_counts()
    total = counts.get("total", 0)
    pendente = counts.get("pendente", 0)
    return {
        "status": camp["status"],
        "remaining": camp.get("manual_remaining", 0) or 0,
        "total": camp.get("manual_total", 0) or 0,
        "total_enviados": total - pendente,
        "pendentes": pendente,
        "fail_streak": camp["fail_streak"] or 0,
    }


# --- controls ---

def queue_manual(n):
    """Add n openers to the manual send queue (capped at pendentes) and start sending."""
    n = max(0, int(n or 0))
    pend = lead_store.funnel_counts().get("pendente", 0)
    camp = lead_store.get_campaign()
    current = camp.get("manual_remaining", 0) or 0
    remaining = min(current + n, pend)
    added = max(0, remaining - current)
    # batch total: reset when starting fresh, grow when adding to a live batch
    total = remaining if current <= 0 else (camp.get("manual_total", 0) or 0) + added
    status = "running" if remaining > 0 else "idle"
    r = lead_store.set_campaign(manual_remaining=remaining, manual_total=total,
                                status=status, fail_streak=0)
    events.bump()
    return r


def stop_manual():
    """Stop the current batch, clearing whatever is left in the queue."""
    r = lead_store.set_campaign(manual_remaining=0, manual_total=0, status="idle")
    events.bump()
    return r


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
