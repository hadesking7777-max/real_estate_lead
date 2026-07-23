"""
Automation engine for the reactivation campaign.

Runs as a daemon thread inside the web process. Each tick sends AT MOST one
WhatsApp message, gated so the number stays healthy:
  1. Pacing gap      - a minimum interval between any two sends.
  2. Auto follow-ups - non-responders (opener sent, no reply) get a gentle
                       re-engagement touch (follow-up 1), then a soft sign-off
                       (follow-up 2), each after a configurable delay, and only
                       inside daytime Sao Paulo hours.
Priority per tick: follow-up 2 > follow-up 1 > new opener. Openers are queued
by the operator (manual batch); follow-ups fire on their own once due.

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

# Auto re-engagement follow-ups for non-responders (opener sent, no reply yet).
# Delays are measured from the previous send to that lead; both touches are
# approved templates, so they are allowed outside WhatsApp's 24h window.
FOLLOWUP_ENABLED = os.environ.get("FOLLOWUP_ENABLED", "1").strip().lower() not in ("0", "false", "no", "")
FOLLOWUP_DELAY1 = float(os.environ.get("FOLLOWUP_DELAY1_DAYS", "2")) * 86400   # gentle nudge
FOLLOWUP_DELAY2 = float(os.environ.get("FOLLOWUP_DELAY2_DAYS", "3")) * 86400   # soft sign-off
# Only auto-send follow-ups inside daytime Sao Paulo hours, so a reminder never
# lands in the middle of the night. The opener is operator-triggered, so it is
# not windowed here.
FOLLOWUP_HOUR_START = int(os.environ.get("FOLLOWUP_HOUR_START", "9"))
FOLLOWUP_HOUR_END = int(os.environ.get("FOLLOWUP_HOUR_END", "20"))

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
        if lead.get("stage") != "contatado":
            lead_store.log_state(lead["phone"], "stage", lead.get("stage"), "contatado",
                                 actor="auto", source="campanha", ts=now)
        if lead.get("delivery") != "enviado":
            lead_store.log_state(lead["phone"], "delivery", lead.get("delivery"), "enviado",
                                 actor="auto", source="campanha", ts=now)
    else:
        lead_store.update_lead(lead["phone"], last_template_used=template, last_wamid=wamid,
                               last_send_ts=now, followup_count=(1 if kind == "followup1" else 2))
    lead_store.append_history(lead["phone"], "bot", f"[{kind}:{template}]")


def _daily_send_cap_remaining(camp, now):
    """How many more sends (openers and follow-ups share the cap) are allowed today,
    per the warm-up ramp. Lazily stamps the campaign's start_ts on first use, since
    the ramp counts calendar days since the campaign first ran, not messages sent.
    """
    start_ts = camp.get("start_ts")
    if start_ts is None:
        start_ts = now
        lead_store.set_campaign(start_ts=start_ts)
    day = current_day(start_ts, now)
    target = ramp_target(day)
    day_start = _sp_day_start_ts(now)
    sent_today = lead_store.count_ok_sends_between(day_start, day_start + 86400)
    return target - sent_today


def _within_followup_window(now):
    """True if Sao Paulo local time is inside the daytime send window."""
    hour = int(((now - _SP_OFFSET) % 86400) // 3600)
    return FOLLOWUP_HOUR_START <= hour < FOLLOWUP_HOUR_END


def _send_followup(lead, kind, sender, now):
    """Send one re-engagement follow-up to a non-responder and record the outcome."""
    template = _pick_template(lead, kind)
    status, resp = sender(lead["phone"], template, _first_name(lead.get("nome")))
    ok = status in (200, 201)
    lead_store.record_send(lead["phone"], kind, template, now, ok)
    if ok:
        _apply_success(lead, kind, template, resp, now)
        events.bump()
        return {"action": "sent", "kind": kind, "phone": lead["phone"], "template": template}
    # Failure: keep the API reason for the panel and push this lead's next
    # attempt out by the delay (bump last_send_ts) so one bad number can't
    # retry-storm every tick. followup_count stays put, so it is tried again
    # after the delay rather than marked done.
    fields = {"last_send_ts": now}
    try:
        import json
        err = (json.loads(resp) or {}).get("error", {})
        detail = (err.get("error_data") or {}).get("details") or err.get("message") or ""
        fields["last_error"] = f"{err.get('code')}: {detail}"[:400]
    except Exception:  # noqa: BLE001
        pass
    lead_store.update_lead(lead["phone"], **fields)
    events.bump()
    return {"action": "failed", "kind": kind, "phone": lead["phone"], "status": status}


def tick(now=None, sender=None):
    """Send at most one message per tick: a due follow-up first, else a queued opener.

    Total daily volume (openers and follow-ups together) is capped by the warm-up
    ramp, so a large queued batch is worked gradually across days instead of all
    at once -- this is what keeps the number healthy at scale.
    """
    now = time.time() if now is None else now
    sender = sender or send.send_template

    last = lead_store.last_send_ts()
    if last is not None and (now - last) < SEND_GAP_SECONDS:
        return {"action": "wait", "reason": "gap"}

    camp = lead_store.get_campaign()

    if _daily_send_cap_remaining(camp, now) <= 0:
        return {"action": "wait", "reason": "daily_cap"}

    # 1) Auto follow-ups to non-responders. Independent of the manual opener
    #    batch, but stands down if the campaign was paused (bad token/setup) or
    #    it is outside the daytime send window.
    if FOLLOWUP_ENABLED and camp["status"] != "paused" and _within_followup_window(now):
        due = lead_store.due_followup(now, FOLLOWUP_DELAY1, FOLLOWUP_DELAY2)
        if due:
            fu_lead, kind = due
            return _send_followup(fu_lead, kind, sender, now)

    # 2) Manual opener batch takes priority whenever the operator has one queued.
    remaining = camp.get("manual_remaining", 0) or 0
    if camp["status"] == "running" and remaining > 0:
        lead = lead_store.next_pending_opener()
        if not lead:  # base exhausted
            lead_store.set_campaign(manual_remaining=0, status="idle")
            return {"action": "idle", "reason": "no_pending"}
        return _send_opener(lead, sender, now, manual_remaining=remaining)

    # 3) Autopilot: works the pending base on its own, day after day, with no
    #    operator batch needed -- bounded only by the daily cap already checked
    #    above. Stands down if paused, same as follow-ups do.
    if camp.get("auto_mode") and camp["status"] != "paused":
        lead = lead_store.next_pending_opener()
        if lead:
            return _send_opener(lead, sender, now, manual_remaining=None)

    return None


def _send_opener(lead, sender, now, manual_remaining=None):
    """Send one opener and record the outcome.

    manual_remaining, when given, means this send counts against an operator-
    queued batch (decremented, batch finished at zero); when None, it's an
    autopilot pull from the pending base with no counter to manage, gated only
    by the daily cap the caller already checked.
    """
    template = _pick_template(lead, "opener")
    status, resp = sender(lead["phone"], template, _first_name(lead.get("nome")))
    ok = status in (200, 201)
    lead_store.record_send(lead["phone"], "opener", template, now, ok)

    if ok:
        _apply_success(lead, "opener", template, resp, now)
        fields = {"fail_streak": 0}
        result = {"action": "sent", "phone": lead["phone"], "template": template}
        if manual_remaining is not None:
            new_remaining = manual_remaining - 1
            fields["manual_remaining"] = new_remaining
            if new_remaining <= 0:
                fields["status"] = "idle"
            result["remaining"] = new_remaining
        lead_store.set_campaign(**fields)
        events.bump()  # state committed; push an update to any open panel
        return result

    lead_store.advance_delivery(lead["phone"], "falhou", actor="auto", source="campanha")
    try:  # keep the API error reason so the panel can show WHY it failed
        import json
        err = (json.loads(resp) or {}).get("error", {})
        detail = (err.get("error_data") or {}).get("details") or err.get("message") or ""
        lead_store.update_lead(lead["phone"], last_error=f"{err.get('code')}: {detail}"[:400])
    except Exception:  # noqa: BLE001
        pass
    streak = (lead_store.get_campaign()["fail_streak"] or 0) + 1
    fields = {"fail_streak": streak}
    if streak >= MAX_FAIL_STREAK:
        fields["status"] = "paused"  # stop after repeated failures (bad token/setup)
    lead_store.set_campaign(**fields)
    events.bump()
    return {"action": "failed", "phone": lead["phone"], "status": status, "streak": streak}


def status_summary(now=None):
    now = time.time() if now is None else now
    camp = lead_store.get_campaign()
    counts = lead_store.funnel_counts()
    total = counts.get("total", 0)
    pendente = counts.get("pendente", 0)
    day_start = _sp_day_start_ts(now)
    daily_sent = lead_store.count_ok_sends_between(day_start, day_start + 86400)
    daily_target = ramp_target(current_day(camp["start_ts"], now)) if camp.get("start_ts") else ramp_target(1)
    return {
        "status": camp["status"],
        "remaining": camp.get("manual_remaining", 0) or 0,
        "total": camp.get("manual_total", 0) or 0,
        "total_enviados": total - pendente,
        "pendentes": pendente,
        "fail_streak": camp["fail_streak"] or 0,
        "daily_sent": daily_sent,
        "daily_target": daily_target,
        "daily_remaining": max(0, daily_target - daily_sent),
        "auto_mode": camp.get("auto_mode", False),
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


def set_auto_mode(on):
    """Turn autopilot on/off: when on, tick() works the pending base on its own
    every day (bounded by the daily ramp cap) with no manual batch needed.
    Turning on clears any prior pause (same resume convention as queue_manual);
    turning off only stops autopilot, it does not touch a manual batch that
    might be running independently.
    """
    if on:
        r = lead_store.set_campaign(auto_mode=True, status="running", fail_streak=0)
    else:
        r = lead_store.set_campaign(auto_mode=False)
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
