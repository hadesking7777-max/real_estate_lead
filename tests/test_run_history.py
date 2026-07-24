"""
run_history.py's atomic write (temp file + os.replace): normal round trip,
no leftover temp files, tolerating a corrupted existing file, and cleaning up
its temp file if the write itself fails partway.

run_history.py doesn't touch lead_store at all, so isolation here is just
pointing HISTORY_PATH at a fresh tmp file per test (independent of the
lead_store tmp_db fixture, though that still runs harmlessly alongside it).

Ported from: test_run_history_atomic.py.
"""

import glob
import json
import os

import pytest

import run_history as rh


@pytest.fixture(autouse=True)
def tmp_history(tmp_path, monkeypatch):
    monkeypatch.setattr(rh, "HISTORY_PATH", os.path.join(str(tmp_path), "history.json"))
    yield tmp_path


def test_record_and_recent_round_trip():
    rh.record("importar", "10 contatos")
    rh.record("analisar", "5 contatos novos")
    items = rh.recent(10)
    assert len(items) == 2
    assert items[0]["acao"] == "analisar"  # most recent first
    assert items[1]["acao"] == "importar"


def test_no_leftover_temp_files_after_a_normal_write(tmp_history):
    rh.record("importar", "10 contatos")
    leftovers = glob.glob(os.path.join(str(tmp_history), "*.tmp"))
    assert leftovers == [], leftovers


def test_recent_and_record_tolerate_a_corrupted_existing_file():
    with open(rh.HISTORY_PATH, "w", encoding="utf-8") as f:
        f.write('{"items": [truncated garbage')  # deliberately invalid JSON

    assert rh.recent(10) == []

    rh.record("importar", "recovered after corruption")
    items = rh.recent(10)
    assert len(items) == 1 and items[0]["detalhe"] == "recovered after corruption"


def test_a_failed_write_cleans_up_its_temp_file_and_leaves_original_untouched(monkeypatch, tmp_history):
    rh.record("importar", "10 contatos")  # establish a real file to check "untouched" against
    before = rh._load()

    def _boom(*a, **kw):
        raise RuntimeError("disk full (simulated)")

    monkeypatch.setattr(json, "dump", _boom)
    with pytest.raises(RuntimeError):
        rh.record("importar", "should not land")

    assert rh._load() == before, "original file must be untouched after a failed write"
    assert glob.glob(os.path.join(str(tmp_history), "*.tmp")) == [], \
        "temp file must be cleaned up on failure"
