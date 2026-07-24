"""
Direct tests of lead_store.py's own API, independent of the scheduler or the
webhook server: the SQLite locked-retry decorator, message-id idempotency,
set_stage's atomicity guarantees, and the hot-since-map used by the panel's
stale-hot-lead badge.

Ported from: test_retry_wrapper.py, test_idempotency_nontext.py (part 1),
test_hardening_1.py (part 7), test_stale_hot.py (part 1).
"""

import sqlite3
import time

import pytest

import lead_store


# ---------- _retry_on_locked ----------

def test_retry_on_locked_succeeds_once_it_clears():
    calls = {"n": 0}

    @lead_store._retry_on_locked(max_attempts=3, base_delay=0.01)
    def flaky_twice_then_ok():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert flaky_twice_then_ok() == "ok"
    assert calls["n"] == 3


def test_retry_on_locked_gives_up_after_max_attempts():
    calls = {"n": 0}

    @lead_store._retry_on_locked(max_attempts=3, base_delay=0.01)
    def always_locked():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        always_locked()
    assert calls["n"] == 3, "should retry exactly max_attempts times, no more"


def test_retry_on_locked_does_not_retry_other_operational_errors():
    calls = {"n": 0}

    @lead_store._retry_on_locked(max_attempts=3, base_delay=0.01)
    def different_error():
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: bogus")

    with pytest.raises(sqlite3.OperationalError):
        different_error()
    assert calls["n"] == 1, "a non-'locked' OperationalError must not be retried"


def test_decorated_functions_work_normally_end_to_end():
    lead_store.get_or_create_lead("5511900000001", nome="Teste")
    lead_store.update_lead("5511900000001", stage="pendente")
    lead_store.append_history("5511900000001", "lead", "oi")
    lead_store.advance_delivery("5511900000001", "enviado", actor="auto", source="test")
    lead_store.record_send("5511900000001", "opener", "reativacao_pf_faria_lima_v1", time.time(), True)
    lead = lead_store.get_lead("5511900000001")
    assert lead["stage"] == "pendente" and lead["delivery"] == "enviado"
    assert lead["history"][0]["text"] == "oi"


# ---------- already_processed (webhook message-id idempotency) ----------

def test_already_processed_dedups_message_ids():
    assert lead_store.already_processed("wamid.ABC123") is False  # first time: not a dup
    assert lead_store.already_processed("wamid.ABC123") is True   # second time: is a dup
    assert lead_store.already_processed("wamid.DEF456") is False  # different id: not a dup


def test_already_processed_none_id_never_blocks_processing():
    assert lead_store.already_processed(None) is False
    assert lead_store.already_processed(None) is False


# ---------- set_stage atomicity ----------

def test_set_stage_unknown_lead_raises_with_no_dangling_audit_entry():
    with pytest.raises(KeyError):
        lead_store.set_stage("5511900000099", "quente")
    log = lead_store.get_state_log("5511900000099")
    assert log == [], "no state_log entry should exist for a lead that was never created"


def test_set_stage_normal_path_updates_and_logs():
    lead_store.get_or_create_lead("5511900000003", nome="Atomic")
    lead_store.update_lead("5511900000003", stage="pendente")
    result = lead_store.set_stage("5511900000003", "contatado", actor="auto", source="campanha")
    assert result["stage"] == "contatado"
    log = lead_store.get_state_log("5511900000003")
    assert any(entry["to_val"] == "contatado" for entry in log)


# ---------- hot_since_map ----------

def test_hot_since_map_returns_last_quente_transition_only_for_leads_with_one():
    now = time.time()
    day = 86400

    lead_store.get_or_create_lead("5511900000001", nome="Recente Quente")
    lead_store.set_stage("5511900000001", "quente", ts=now - 3600)

    lead_store.get_or_create_lead("5511900000002", nome="Velho Quente")
    lead_store.set_stage("5511900000002", "quente", ts=now - 5 * day)

    # marked quente directly (update_lead, not set_stage) -- no state_log entry at all
    lead_store.get_or_create_lead("5511900000003", nome="Sem Log")
    lead_store.update_lead("5511900000003", stage="quente")

    m = lead_store.hot_since_map()
    assert abs(m["5511900000001"] - (now - 3600)) < 1
    assert abs(m["5511900000002"] - (now - 5 * day)) < 1
    assert "5511900000003" not in m
