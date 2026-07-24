"""
send_campaign.py is a legacy standalone CLI sender with its own ramp/pacing,
completely uncoordinated with scheduler.py's daily cap, autopilot, and fail-
streak pause. It refuses to run at all if the live webhook_server system
(autopilot or a manual batch) looks active, to avoid two uncoordinated
senders drawing from the same pending pool at once.

Ported from: test_hardening_2.py (part 9).
"""

import pytest

import lead_store
import scheduler
import send_campaign


def test_refuses_to_run_while_autopilot_is_active():
    scheduler.set_auto_mode(True)
    with pytest.raises(SystemExit) as exc_info:
        send_campaign._refuse_if_live_system_active()
    assert exc_info.value.code == 1


def test_refuses_to_run_while_a_manual_batch_is_active():
    scheduler.set_auto_mode(False)
    lead_store.get_or_create_lead("5511900000003", nome="P")
    lead_store.update_lead("5511900000003", stage="pendente", delivery="pendente")
    scheduler.queue_manual(1)
    with pytest.raises(SystemExit) as exc_info:
        send_campaign._refuse_if_live_system_active()
    assert exc_info.value.code == 1


def test_proceeds_normally_when_the_live_system_is_idle():
    scheduler.set_auto_mode(False)
    scheduler.stop_manual()
    send_campaign._refuse_if_live_system_active()  # should NOT raise/exit
