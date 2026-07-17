"""
Small JSON-backed log of import executions (analyses and confirmed imports),
shown below the upload form so the operator can see what has run and when.
"""

import json
import os
import threading
from datetime import datetime, timedelta

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "history.json")
_lock = threading.Lock()
MAX = 50


def _now_br():
    # Brazil (Sao Paulo) has been fixed at UTC-3 with no DST since 2019.
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%d/%m/%Y %H:%M")


def record(acao, detalhe):
    with _lock:
        items = []
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                items = json.load(f)
        items.insert(0, {"quando": _now_br(), "acao": acao, "detalhe": detalhe})
        items = items[:MAX]
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)


def recent(n=10):
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)[:n]
