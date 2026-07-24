"""
Core campaign automation logic in scheduler.py: tick()'s follow-up cadence,
the shared daily send cap, autopilot vs. manual-batch priority, "stop
everything", and a couple of hardening fixes (daily cap counting failed
attempts too, and a race-condition mitigation in _apply_success).

Ported from: test_followup.py, test_daily_cap.py, test_autopilot.py,
test_stop_everything.py, test_hardening_1.py (parts 5 and 6).
"""

import time

import pytest

import lead_store
import scheduler

from conftest import seed_lead

T0 = 1_753_000_000.0  # arbitrary anchor, well clear of epoch/day-boundary edge cases


def _drain(sender, start_ts, max_ticks=500):
    """Run tick() repeatedly (advancing a fake clock by the pacing gap each
    time) until the daily cap is hit or nothing more happens. Returns the
    number of successful sends and the timestamp reached."""
    sent = 0
    t = start_ts
    for _ in range(max_ticks):
        res = scheduler.tick(now=t, sender=sender)
        if res and res.get("action") == "sent":
            sent += 1
            t += scheduler.SEND_GAP_SECONDS + 1
        elif res and res.get("reason") == "daily_cap":
            break
        else:
            t += scheduler.SEND_GAP_SECONDS + 1
    return sent, t


@pytest.fixture(autouse=True)
def _bypass_followup_night_window(monkeypatch):
    """Most of these scenarios don't care about the daytime-only follow-up
    window; individual tests that DO care override this back."""
    monkeypatch.setattr(scheduler, "FOLLOWUP_HOUR_START", 0)
    monkeypatch.setattr(scheduler, "FOLLOWUP_HOUR_END", 24)


# ---------------------------------------------------------------------------
# follow-up cadence (test_followup.py)
# ---------------------------------------------------------------------------

class TestFollowupCadence:
    @pytest.fixture(autouse=True)
    def _seed(self):
        self.D1 = scheduler.FOLLOWUP_DELAY1
        self.D2 = scheduler.FOLLOWUP_DELAY2
        seed_lead("A", stage="contatado", delivery="enviado", followup_count=0, last_send_ts=T0)  # non-responder
        seed_lead("B", stage="contatado", delivery="respondeu", followup_count=0, last_send_ts=T0)  # replied, AI errored
        seed_lead("C", stage="qualificando", delivery="respondeu", followup_count=0, last_send_ts=T0)  # replied+qualified
        seed_lead("D", stage="pendente", delivery="pendente", followup_count=0, last_send_ts=None)  # never contacted

    def test_nothing_due_before_delay1(self, fake_sender):
        scheduler.tick(now=T0 + self.D1 - 10, sender=fake_sender)
        assert not fake_sender.calls, f"sent too early: {fake_sender.calls}"

    def test_followup1_fires_for_the_non_responder_only(self, fake_sender):
        r = scheduler.tick(now=T0 + self.D1 + 1, sender=fake_sender)
        assert r["action"] == "sent" and r["kind"] == "followup1", r
        assert fake_sender.calls[-1][0] == "A", fake_sender.calls
        assert fake_sender.calls[-1][1] in scheduler.FOLLOWUP1, fake_sender.calls
        assert lead_store.get_lead("A")["followup_count"] == 1

    def test_not_due_again_until_delay2_after_followup1(self, fake_sender):
        scheduler.tick(now=T0 + self.D1 + 1, sender=fake_sender)  # followup1 fires
        now3 = T0 + self.D1 + 1
        r = scheduler.tick(now=now3 + self.D2 - 10, sender=fake_sender)
        assert r is None or r.get("action") in ("wait",), r
        assert len(fake_sender.calls) == 1, fake_sender.calls

    def test_followup2_fires_after_delay2(self, fake_sender):
        scheduler.tick(now=T0 + self.D1 + 1, sender=fake_sender)  # followup1
        now3 = T0 + self.D1 + 1
        r = scheduler.tick(now=now3 + self.D2 + 1, sender=fake_sender)
        assert r["action"] == "sent" and r["kind"] == "followup2", r
        assert fake_sender.calls[-1][1] in scheduler.FOLLOWUP2, fake_sender.calls
        assert lead_store.get_lead("A")["followup_count"] == 2

    def test_a_stops_at_two_touches_and_no_one_else_is_ever_eligible(self, fake_sender):
        scheduler.tick(now=T0 + self.D1 + 1, sender=fake_sender)  # followup1
        now3 = T0 + self.D1 + 1
        scheduler.tick(now=now3 + self.D2 + 1, sender=fake_sender)  # followup2
        r = scheduler.tick(now=now3 + self.D2 + 1 + 999999, sender=fake_sender)
        assert len(fake_sender.calls) == 2, f"unexpected extra sends: {fake_sender.calls}"

    def test_due_followup_delivery_filter_excludes_a_replied_lead(self):
        lead_store.update_lead("A", followup_count=0, last_send_ts=T0)  # reset A eligible
        due = lead_store.due_followup(T0 + self.D1 + 1, self.D1, self.D2)
        assert due and due[0]["phone"] == "A", due
        lead_store.update_lead("A", delivery="respondeu")  # A now "replied"
        assert lead_store.due_followup(T0 + self.D1 + 1, self.D1, self.D2) is None, \
            "replied lead still due!"


def test_followup_night_window_guard(monkeypatch):
    monkeypatch.setattr(scheduler, "FOLLOWUP_HOUR_START", 9)
    monkeypatch.setattr(scheduler, "FOLLOWUP_HOUR_END", 20)
    # epoch 0 = 21:00 in Sao Paulo (UTC-3) -> outside window
    assert scheduler._within_followup_window(0) is False
    # 15:00 UTC = 12:00 SP -> inside
    assert scheduler._within_followup_window(15 * 3600) is True


# ---------------------------------------------------------------------------
# shared daily send cap (test_daily_cap.py)
# ---------------------------------------------------------------------------

def _seed_pending(n, prefix="H"):
    for i in range(n):
        phone = f"5511{prefix}{i:08d}"
        seed_lead(phone, stage="pendente", delivery="pendente")


def test_daily_cap_limits_a_large_queued_batch_to_the_day1_ramp_target(fake_sender):
    _seed_pending(200)
    r = scheduler.queue_manual(200)
    assert lead_store.get_campaign()["start_ts"] is None, \
        "start_ts should NOT be set by queue_manual itself"

    sent, t = _drain(fake_sender, T0)
    assert sent == 20, f"expected exactly the day-1 ramp target (20), got {sent}"

    camp = lead_store.get_campaign()
    assert camp["start_ts"] is not None, "start_ts should be lazily stamped once sending begins"

    # further ticks the same day: still capped, nothing more sent
    r2 = scheduler.tick(now=t + 3600, sender=fake_sender)
    assert r2 == {"action": "wait", "reason": "daily_cap"}, r2

    # advance to day 2 (Sao Paulo) -> ramp target 30, cap resets independent of day 1
    t_day2 = camp["start_ts"] + 86400 + 3600
    sent2, t2 = _drain(fake_sender, t_day2)
    assert sent2 == 30, f"expected day-2 ramp target (30), got {sent2}"

    s = scheduler.status_summary(now=t2)
    assert s["daily_target"] == 30 and s["daily_sent"] == 30 and s["daily_remaining"] == 0, s


def test_daily_cap_is_shared_between_openers_and_followups(fake_sender):
    _seed_pending(200)
    scheduler.queue_manual(200)
    sent, t = _drain(fake_sender, T0)
    assert sent == 20

    lead_store.set_campaign(manual_remaining=0, manual_total=0, status="idle")  # stop opener flow
    # a genuine non-responder, due for followup1, well before the cap
    fu_phone = "5511900000001"
    seed_lead(fu_phone, nome="NonResponder", stage="contatado", delivery="enviado",
              followup_count=0, last_send_ts=t - scheduler.FOLLOWUP_DELAY1 - 10)
    r3 = scheduler.tick(now=t + 3600, sender=fake_sender)
    assert r3 == {"action": "wait", "reason": "daily_cap"}, \
        "follow-ups must be blocked once the shared daily cap is hit too"


# ---------------------------------------------------------------------------
# autopilot mode (test_autopilot.py)
# ---------------------------------------------------------------------------

def test_manual_only_batch_unchanged_with_autopilot_off_by_default(fake_sender):
    _seed_pending(5, "M")
    r = scheduler.queue_manual(3)
    assert r["status"] == "running" and r["manual_remaining"] == 3, r
    t = T0
    sent = 0
    for _ in range(20):
        res = scheduler.tick(now=t, sender=fake_sender)
        if res and res.get("action") == "sent":
            sent += 1
            t += scheduler.SEND_GAP_SECONDS + 1
        elif res and res.get("action") == "idle":
            break
        else:
            t += scheduler.SEND_GAP_SECONDS + 1
    assert sent == 3, f"manual batch regression: sent={sent}"
    assert lead_store.get_campaign()["status"] == "idle"


def test_autopilot_works_the_base_with_no_manual_queue(fake_sender):
    _seed_pending(50, "P")
    assert lead_store.get_campaign()["auto_mode"] is False
    scheduler.set_auto_mode(True)
    camp = lead_store.get_campaign()
    assert camp["auto_mode"] is True and camp["status"] == "running", camp

    sent, _ = _drain(fake_sender, T0, max_ticks=100)
    assert sent == 20, f"expected day-1 ramp target (20) via autopilot alone, got {sent}"


def test_manual_batch_takes_priority_over_autopilot_then_autopilot_fills_the_rest(fake_sender):
    _seed_pending(50, "Q")
    scheduler.set_auto_mode(True)
    scheduler.queue_manual(5)
    camp = lead_store.get_campaign()
    assert camp["auto_mode"] is True and camp["manual_remaining"] == 5

    t = T0
    res = scheduler.tick(now=t, sender=fake_sender)
    assert res["action"] == "sent" and res.get("remaining") == 4, res  # manual decremented

    sent = 1
    t += scheduler.SEND_GAP_SECONDS + 1
    for _ in range(100):
        res = scheduler.tick(now=t, sender=fake_sender)
        if res and res.get("action") == "sent":
            sent += 1
            t += scheduler.SEND_GAP_SECONDS + 1
        elif res and res.get("reason") == "daily_cap":
            break
        else:
            t += scheduler.SEND_GAP_SECONDS + 1
    assert sent == 20, f"expected total of 20 (manual 5 + autopilot fill to daily cap), got {sent}"
    assert lead_store.get_campaign()["manual_remaining"] == 0


def test_turning_autopilot_off_does_not_clobber_a_concurrent_manual_batch():
    _seed_pending(50, "R")
    scheduler.queue_manual(10)
    scheduler.set_auto_mode(True)
    scheduler.set_auto_mode(False)  # toggle off again
    camp = lead_store.get_campaign()
    assert camp["auto_mode"] is False
    assert camp["status"] == "running" and camp["manual_remaining"] == 10, camp


def test_autopilot_respects_max_fail_streak_auto_pause(fake_sender):
    _seed_pending(50, "S")
    scheduler.set_auto_mode(True)

    def failing_sender(phone, template, first):
        return 400, '{"error":{"code":131049,"message":"nope"}}'

    t = T0
    for _ in range(scheduler.MAX_FAIL_STREAK + 2):
        scheduler.tick(now=t, sender=failing_sender)
        t += scheduler.SEND_GAP_SECONDS + 1
    camp = lead_store.get_campaign()
    assert camp["status"] == "paused", camp
    assert camp["fail_streak"] >= scheduler.MAX_FAIL_STREAK

    r = scheduler.tick(now=t, sender=fake_sender)
    assert r is None, r  # paused: autopilot stands down, no further sends


# ---------------------------------------------------------------------------
# "stop everything" (test_stop_everything.py)
# ---------------------------------------------------------------------------

T_SP_DAY = 1_700_000_000.0  # a Sao Paulo daytime timestamp


def test_stop_manual_also_turns_off_autopilot_and_pauses_outreach():
    _seed_pending(10, "A")
    scheduler.set_auto_mode(True)
    scheduler.queue_manual(5)
    camp = lead_store.get_campaign()
    assert camp["auto_mode"] is True and camp["manual_remaining"] == 5

    scheduler.stop_manual()
    camp = lead_store.get_campaign()
    assert camp["manual_remaining"] == 0 and camp["manual_total"] == 0
    assert camp["status"] == "idle"
    assert camp["auto_mode"] is False, "stop_manual must also turn autopilot off"
    assert camp["outreach_paused"] is True, "stop_manual must set outreach_paused"


def test_outreach_paused_withholds_a_due_followup(fake_sender):
    seed_lead("B0", stage="pendente", delivery="pendente")
    phone = "B0"
    lead_store.update_lead(phone, stage="contatado", delivery="enviado",
                            followup_count=0, last_send_ts=T_SP_DAY - scheduler.FOLLOWUP_DELAY1 - 10)

    # sanity: without outreach_paused, the due follow-up fires
    res = scheduler.tick(now=T_SP_DAY, sender=fake_sender)
    assert res and res.get("action") == "sent" and res.get("kind") == "followup1", res

    # re-arm the same lead as due again, but pause outreach first
    lead_store.update_lead(phone, followup_count=0, last_send_ts=T_SP_DAY - scheduler.FOLLOWUP_DELAY1 - 10)
    lead_store.set_campaign(outreach_paused=True)
    res = scheduler.tick(now=T_SP_DAY + scheduler.SEND_GAP_SECONDS + 1, sender=fake_sender)
    assert res is None or res.get("kind") != "followup1", res


def test_outreach_paused_withholds_autopilot_sends_even_if_auto_mode_true(fake_sender):
    _seed_pending(5, "C")
    lead_store.set_campaign(auto_mode=True, status="running", outreach_paused=True)
    res = scheduler.tick(now=T_SP_DAY, sender=fake_sender)
    assert res is None, res


def test_queue_manual_and_set_auto_mode_clear_outreach_paused_on_resume():
    _seed_pending(10, "D")
    lead_store.set_campaign(outreach_paused=True)
    scheduler.queue_manual(3)
    assert lead_store.get_campaign()["outreach_paused"] is False

    lead_store.set_campaign(outreach_paused=True)
    scheduler.set_auto_mode(True)
    assert lead_store.get_campaign()["outreach_paused"] is False


# ---------------------------------------------------------------------------
# hardening: daily cap counts ALL attempts + race-condition mitigation
# (test_hardening_1.py, parts 5 and 6)
# ---------------------------------------------------------------------------

def test_daily_cap_counts_failed_attempts_too():
    _seed_pending(30, "H")
    attempt_n = [0]

    def flaky_sender(phone, template, first):
        attempt_n[0] += 1
        # every other attempt fails -- scattered, not consecutive, so fail_streak
        # keeps resetting and would never trip MAX_FAIL_STREAK
        if attempt_n[0] % 2 == 0:
            return 400, '{"error":{"code":131049,"message":"nope"}}'
        return 200, '{"messages":[{"id":"wamid.x"}]}'

    scheduler.queue_manual(30)
    t = T0
    total_attempts = 0
    for _ in range(100):
        res = scheduler.tick(now=t, sender=flaky_sender)
        if res and res.get("action") in ("sent", "failed"):
            total_attempts += 1
            t += scheduler.SEND_GAP_SECONDS + 1
        elif res and res.get("reason") == "daily_cap":
            break
        else:
            t += scheduler.SEND_GAP_SECONDS + 1
    # day-1 ramp target is 20 -- with scattered failures counting too, total
    # attempts (success+fail combined) must stop at exactly 20
    assert total_attempts == 20, f"expected cap to include failures, got {total_attempts} attempts"


def test_apply_success_does_not_clobber_a_concurrent_stage_change():
    phone = "5511900000001"
    seed_lead(phone, nome="Racer", stage="pendente", delivery="pendente")
    lead_snapshot = lead_store.get_lead(phone)  # what tick() would have read before sending

    # simulate a concurrent opt-out landing WHILE the (slow) network call is "in flight"
    lead_store.set_stage(phone, "opt_out", actor="auto", source="ia")

    scheduler._apply_success(lead_snapshot, "opener", "reativacao_pf_faria_lima_v1",
                              '{"messages":[{"id":"wamid.race"}]}', T0)

    final = lead_store.get_lead(phone)
    assert final["stage"] == "opt_out", \
        f"race condition clobbered a concurrent opt-out: {final['stage']}"
    assert final["delivery"] == "enviado", \
        "delivery should still be recorded even when stage isn't overwritten"


def test_apply_success_normal_case_still_moves_stage_to_contatado():
    phone2 = "5511900000002"
    seed_lead(phone2, nome="Normal", stage="pendente", delivery="pendente")
    snap2 = lead_store.get_lead(phone2)
    scheduler._apply_success(snap2, "opener", "reativacao_pf_faria_lima_v1",
                              '{"messages":[{"id":"wamid.n"}]}', T0)
    final2 = lead_store.get_lead(phone2)
    assert final2["stage"] == "contatado" and final2["delivery"] == "enviado"
