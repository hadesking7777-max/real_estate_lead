"""
scheduler.py's background-thread alerting: the WhatsApp account health check
(status-change alerting, not on every poll), the campaign auto-pause alert
after a consecutive failure streak, and the weekly digest email.

Ported from: test_health_alert.py (parts 2-6; part 1 -- send.check_health()
itself -- lives in test_send_and_alerts.py), test_pause_alert.py,
test_weekly_summary.py.
"""

import json

import pytest

import lead_store
import scheduler
import alerts
import send

from conftest import seed_lead

T0 = 1_700_000_000.0


class FakeResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


@pytest.fixture(autouse=True)
def _reset_health_check_state(monkeypatch):
    """_last_health_check is module-global state in scheduler.py, not tied to
    the DB, so it must be reset per test just like the scratch script assumed
    a fresh process (_last_health_check starts at 0.0)."""
    monkeypatch.setattr(scheduler, "_last_health_check", 0.0)


# ---------------------------------------------------------------------------
# WhatsApp account health check (test_health_alert.py, parts 2-6)
# ---------------------------------------------------------------------------

def test_first_healthy_check_just_records_a_baseline_no_alert(monkeypatch):
    alert_calls = []
    monkeypatch.setattr(alerts, "send_health_alert",
                         lambda prev, new, quality, detail=None: alert_calls.append((prev, new, quality, detail)))
    monkeypatch.setattr(send.requests, "get", lambda url, headers, params, timeout: FakeResp(
        200, json.dumps({"status": "CONNECTED", "quality_rating": "GREEN", "verified_name": "Cyrela"})))

    scheduler._check_account_health(T0)
    assert alert_calls == []
    assert lead_store.get_setting("wa_health_status") == "CONNECTED"


def test_a_second_call_within_the_interval_does_not_recheck(monkeypatch):
    monkeypatch.setattr(alerts, "send_health_alert", lambda *a, **kw: None)
    monkeypatch.setattr(send.requests, "get", lambda url, headers, params, timeout: FakeResp(
        200, json.dumps({"status": "CONNECTED", "quality_rating": "GREEN"})))
    scheduler._check_account_health(T0)

    def _boom(*a, **kw):
        raise AssertionError("should not re-check yet")
    monkeypatch.setattr(send.requests, "get", _boom)
    scheduler._check_account_health(T0 + 5)  # well within HEALTH_CHECK_INTERVAL


def test_degrading_from_connected_to_flagged_fires_an_alert(monkeypatch):
    alert_calls = []
    monkeypatch.setattr(alerts, "send_health_alert",
                         lambda prev, new, quality, detail=None: alert_calls.append((prev, new, quality, detail)))
    monkeypatch.setattr(send.requests, "get", lambda url, headers, params, timeout: FakeResp(
        200, json.dumps({"status": "CONNECTED", "quality_rating": "GREEN"})))
    scheduler._check_account_health(T0)

    monkeypatch.setattr(send.requests, "get", lambda url, headers, params, timeout: FakeResp(
        200, json.dumps({"status": "FLAGGED", "quality_rating": "RED"})))
    scheduler._check_account_health(T0 + scheduler.HEALTH_CHECK_INTERVAL + 10)

    assert len(alert_calls) == 1
    assert alert_calls[0][0] == "CONNECTED" and alert_calls[0][1] == "FLAGGED" and alert_calls[0][2] == "RED"


def test_staying_unhealthy_does_not_re_alert(monkeypatch):
    alert_calls = []
    monkeypatch.setattr(alerts, "send_health_alert",
                         lambda prev, new, quality, detail=None: alert_calls.append((prev, new, quality, detail)))
    monkeypatch.setattr(send.requests, "get", lambda url, headers, params, timeout: FakeResp(
        200, json.dumps({"status": "CONNECTED", "quality_rating": "GREEN"})))
    scheduler._check_account_health(T0)

    monkeypatch.setattr(send.requests, "get", lambda url, headers, params, timeout: FakeResp(
        200, json.dumps({"status": "FLAGGED", "quality_rating": "RED"})))
    scheduler._check_account_health(T0 + scheduler.HEALTH_CHECK_INTERVAL + 10)
    scheduler._check_account_health(T0 + 2 * scheduler.HEALTH_CHECK_INTERVAL + 20)
    assert len(alert_calls) == 1, "must not re-alert while status is unchanged"


def test_recovering_back_to_connected_fires_a_recovery_alert(monkeypatch):
    alert_calls = []
    monkeypatch.setattr(alerts, "send_health_alert",
                         lambda prev, new, quality, detail=None: alert_calls.append((prev, new, quality, detail)))
    monkeypatch.setattr(send.requests, "get", lambda url, headers, params, timeout: FakeResp(
        200, json.dumps({"status": "CONNECTED", "quality_rating": "GREEN"})))
    scheduler._check_account_health(T0)

    monkeypatch.setattr(send.requests, "get", lambda url, headers, params, timeout: FakeResp(
        200, json.dumps({"status": "FLAGGED", "quality_rating": "RED"})))
    scheduler._check_account_health(T0 + scheduler.HEALTH_CHECK_INTERVAL + 10)

    monkeypatch.setattr(send.requests, "get", lambda url, headers, params, timeout: FakeResp(
        200, json.dumps({"status": "CONNECTED", "quality_rating": "GREEN"})))
    scheduler._check_account_health(T0 + 3 * scheduler.HEALTH_CHECK_INTERVAL + 30)

    assert len(alert_calls) == 2
    assert alert_calls[1][0] == "FLAGGED" and alert_calls[1][1] == "CONNECTED"


def test_non_200_response_also_triggers_an_alert(monkeypatch):
    alert_calls = []
    monkeypatch.setattr(alerts, "send_health_alert",
                         lambda prev, new, quality, detail=None: alert_calls.append((prev, new, quality, detail)))
    monkeypatch.setattr(send.requests, "get", lambda url, headers, params, timeout: FakeResp(
        200, json.dumps({"status": "CONNECTED", "quality_rating": "GREEN"})))
    scheduler._check_account_health(T0)

    monkeypatch.setattr(send.requests, "get", lambda url, headers, params, timeout: FakeResp(
        401, '{"error":{"message":"token expired"}}'))
    scheduler._check_account_health(T0 + scheduler.HEALTH_CHECK_INTERVAL + 10)

    assert len(alert_calls) == 1
    assert alert_calls[0][1] == "ERRO_API"


# ---------------------------------------------------------------------------
# campaign auto-pause alert (test_pause_alert.py)
# ---------------------------------------------------------------------------

def _seed_pending(n, tag):
    for i in range(n):
        seed_lead(f"55119{tag}{i:04d}", stage="pendente", delivery="pendente")


def test_max_fail_streak_pauses_campaign_and_fires_exactly_one_alert(monkeypatch):
    alert_calls = []
    monkeypatch.setattr(alerts, "send_campaign_paused_alert",
                         lambda streak, last_error=None: alert_calls.append((streak, last_error)))

    def failing_sender(phone, template, name):
        return 400, '{"error":{"code":131049,"message":"blocked by healthy ecosystem policy"}}'

    _seed_pending(20, "A")
    scheduler.queue_manual(20)
    t = T0
    for _ in range(scheduler.MAX_FAIL_STREAK):
        res = scheduler.tick(now=t, sender=failing_sender)
        assert res["action"] == "failed", res
        t += scheduler.SEND_GAP_SECONDS + 1

    camp = lead_store.get_campaign()
    assert camp["status"] == "paused", camp
    assert camp["fail_streak"] == scheduler.MAX_FAIL_STREAK
    assert len(alert_calls) == 1, alert_calls
    assert alert_calls[0][0] == scheduler.MAX_FAIL_STREAK
    assert "131049" in (alert_calls[0][1] or "")

    # further ticks while paused don't attempt sends or re-alert
    res = scheduler.tick(now=t + 100, sender=failing_sender)
    assert res is None or res.get("action") != "failed", res
    assert len(alert_calls) == 1, "must not re-alert while already paused"

    # resuming and hitting the streak again fires a fresh, second alert
    scheduler.queue_manual(5)  # resume clears fail_streak and status
    camp = lead_store.get_campaign()
    assert camp["status"] == "running" and camp["fail_streak"] == 0
    t += 200
    for _ in range(scheduler.MAX_FAIL_STREAK):
        scheduler.tick(now=t, sender=failing_sender)
        t += scheduler.SEND_GAP_SECONDS + 1
    assert lead_store.get_campaign()["status"] == "paused"
    assert len(alert_calls) == 2, alert_calls


# ---------------------------------------------------------------------------
# weekly summary digest (test_weekly_summary.py)
# ---------------------------------------------------------------------------

WEEK = scheduler.WEEKLY_SUMMARY_INTERVAL


def test_count_state_transitions_counts_only_in_window_transitions():
    seed_lead("5511900000001", nome="A")
    lead_store.set_stage("5511900000001", "quente", actor="ia", ts=T0 + 10)
    seed_lead("5511900000002", nome="B")
    lead_store.set_stage("5511900000002", "quente", actor="ia", ts=T0 + 20)
    seed_lead("5511900000003", nome="C")
    lead_store.set_stage("5511900000003", "quente", actor="ia", ts=T0 + WEEK + 500)  # outside the window

    c = lead_store.count_state_transitions("stage", "quente", T0, T0 + WEEK)
    assert c == 2, c


def test_weekly_summary_baselines_then_fires_with_correct_counts_and_status(monkeypatch):
    summary_calls = []
    monkeypatch.setattr(alerts, "send_weekly_summary",
                         lambda counts, sent, hot, status: summary_calls.append((counts, sent, hot, status)))

    seed_lead("5511900000001", nome="A")
    lead_store.set_stage("5511900000001", "quente", actor="ia", ts=T0 + 10)
    seed_lead("5511900000002", nome="B")
    lead_store.set_stage("5511900000002", "quente", actor="ia", ts=T0 + 20)
    seed_lead("5511900000003", nome="C")

    # first-ever call just establishes a baseline, no email
    scheduler._maybe_send_weekly_summary(T0)
    assert summary_calls == []
    assert lead_store.get_setting("last_weekly_summary_ts") == str(T0)

    # calling again before the interval elapses does nothing
    scheduler._maybe_send_weekly_summary(T0 + WEEK - 10)
    assert summary_calls == []

    # once the interval elapses, a summary goes out with the right counts
    lead_store.record_send("5511900000001", "opener", "reativacao_pf_faria_lima_v1", T0 + 100, True)
    lead_store.record_send("5511900000002", "opener", "reativacao_pf_faria_lima_v1", T0 + 200, True)
    lead_store.record_send("5511900000002", "opener", "reativacao_pf_faria_lima_v1", T0 + 300, False)  # not counted

    now2 = T0 + WEEK + 5
    scheduler._maybe_send_weekly_summary(now2)
    assert len(summary_calls) == 1, summary_calls
    counts, sent, hot, status = summary_calls[0]
    assert sent == 2, sent  # only the 2 ok=1 sends
    assert hot == 2, hot    # the 2 quente transitions inside [T0, now2)
    assert counts["total"] == 3
    assert status == "parada"  # idle, no manual batch, autopilot off
    assert lead_store.get_setting("last_weekly_summary_ts") == str(now2)

    # the next window starts from THIS send, not the original baseline
    scheduler._maybe_send_weekly_summary(now2 + WEEK - 1)
    assert len(summary_calls) == 1, "must not fire again before a fresh full interval"


def test_campaign_status_label_reflects_paused_autopilot_and_sending_states():
    now2 = T0 + WEEK + 5
    lead_store.set_campaign(status="paused", fail_streak=5)
    assert scheduler._campaign_status_label(now2) == "pausada (streak de falhas: 5)"

    lead_store.set_campaign(status="running", auto_mode=True, fail_streak=0)
    assert scheduler._campaign_status_label(now2) == "piloto automatico ligado"

    lead_store.set_campaign(auto_mode=False, manual_remaining=7, manual_total=10)
    assert "enviando" in scheduler._campaign_status_label(now2)
