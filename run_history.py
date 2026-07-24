"""
Small JSON-backed log of import executions (analyses and confirmed imports),
shown below the upload form so the operator can see what has run and when.
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timedelta

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "history.json")
_lock = threading.Lock()
MAX = 50


def _now_br():
    # Brazil (Sao Paulo) has been fixed at UTC-3 with no DST since 2019.
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%d/%m/%Y %H:%M")


def _load():
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # A prior write that got interrupted mid-file, or a file that's simply
        # gone missing, should not break the panel -- start over rather than crash.
        return []


def record(acao, detalhe):
    with _lock:
        items = _load()
        items.insert(0, {"quando": _now_br(), "acao": acao, "detalhe": detalhe})
        items = items[:MAX]
        # Write to a temp file in the same directory, then atomically replace it.
        # A crash or kill mid-write can then never leave history.json truncated
        # or invalid -- os.replace either fully lands the new content or the old
        # file is untouched.
        directory = os.path.dirname(HISTORY_PATH) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, HISTORY_PATH)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise


def recent(n=10):
    return _load()[:n]
