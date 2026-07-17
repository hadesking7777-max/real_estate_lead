"""
SQLite-backed lead store. Same public API as the earlier JSON version, so
nothing else in the app changes; the storage underneath is now a single
SQLite file (transactional, safe for the concurrent access that the webhook,
the background sender, and the UI will do once automation lands).

On first use it auto-migrates a legacy leads.json (if present) into the DB,
so existing imported contacts and any conversation history carry over.
"""

import json
import os
import re
import sqlite3
import threading

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "leads.db")
DB_PATH = DEFAULT_DB_PATH
_LEGACY_JSON = os.path.join(os.path.dirname(__file__), "leads.json")

_init_lock = threading.Lock()
_initialized_paths = set()

STAGES = ["pendente", "contatado", "respondeu", "qualificando", "quente", "morno", "frio", "opt_out"]

DELIVERY_STATES = ["pendente", "enviado", "entregue", "lido", "respondeu", "falhou"]
_DELIVERY_RANK = {s: i for i, s in enumerate(DELIVERY_STATES)}

SIGNAL_KEYS = ["objetivo", "experiencia", "forma_pagamento", "quantidade_unidades", "timing"]

# Columns update_lead is allowed to write directly (signals handled separately; phone is the key).
_WRITABLE_COLS = ["nome", "perfil", "origem", "pais", "stage", "delivery",
                  "last_template_used", "last_wamid", "followup_count"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  phone TEXT PRIMARY KEY,
  nome TEXT,
  perfil TEXT,
  origem TEXT,
  pais TEXT,
  stage TEXT,
  delivery TEXT,
  signals TEXT,
  last_template_used TEXT,
  last_wamid TEXT,
  followup_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT,
  role TEXT,
  text TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_phone ON history(phone);
CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(stage);
CREATE INDEX IF NOT EXISTS idx_leads_delivery ON leads(delivery);
"""


def _digits(phone):
    return re.sub(r"\D", "", str(phone))


def _empty_signals():
    return {k: None for k in SIGNAL_KEYS}


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_init():
    if DB_PATH in _initialized_paths:
        return
    with _init_lock:
        if DB_PATH in _initialized_paths:
            return
        with _conn() as conn:
            conn.executescript(_SCHEMA)
        _maybe_migrate_json()
        _initialized_paths.add(DB_PATH)


def _maybe_migrate_json():
    # Only migrate the real legacy file into the real DB, never in tests.
    if DB_PATH != DEFAULT_DB_PATH or not os.path.exists(_LEGACY_JSON):
        return
    with _conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]:
            return
        with open(_LEGACY_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        for phone, lead in data.items():
            _insert_row(conn, phone, lead)
            for h in lead.get("history", []):
                conn.execute("INSERT INTO history(phone,role,text) VALUES(?,?,?)",
                             (phone, h.get("role"), h.get("text")))
        conn.commit()
    try:
        os.rename(_LEGACY_JSON, _LEGACY_JSON + ".migrated")
    except OSError:
        pass


def _insert_row(conn, phone, lead):
    conn.execute(
        "INSERT OR IGNORE INTO leads(phone,nome,perfil,origem,pais,stage,delivery,"
        "signals,last_template_used,last_wamid,followup_count) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            phone, lead.get("nome", ""), lead.get("perfil", "PF"),
            lead.get("origem", "Brasil"), lead.get("pais", "Brasil"),
            lead.get("stage", "pendente"), lead.get("delivery", "pendente"),
            json.dumps(lead.get("signals") or _empty_signals()),
            lead.get("last_template_used"), lead.get("last_wamid"),
            lead.get("followup_count", 0),
        ),
    )


def _row_to_lead(conn, row, with_history=True):
    lead = {
        "phone": row["phone"],
        "nome": row["nome"] or "",
        "perfil": row["perfil"] or "PF",
        "origem": row["origem"] or "Brasil",
        "pais": row["pais"] or "Brasil",
        "stage": row["stage"],
        "delivery": row["delivery"],
        "signals": json.loads(row["signals"]) if row["signals"] else _empty_signals(),
        "last_template_used": row["last_template_used"],
        "last_wamid": row["last_wamid"],
        "followup_count": row["followup_count"] or 0,
        "history": [],
    }
    if with_history:
        hrows = conn.execute(
            "SELECT role, text FROM history WHERE phone=? ORDER BY id", (row["phone"],)
        ).fetchall()
        lead["history"] = [{"role": h["role"], "text": h["text"]} for h in hrows]
    return lead


def get_lead(phone):
    _ensure_init()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        return _row_to_lead(conn, row) if row else None


def get_or_create_lead(phone, nome=None, perfil="PF"):
    _ensure_init()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        if row is None:
            _insert_row(conn, phone, {
                "nome": nome or "", "perfil": perfil,
                "stage": "contatado", "delivery": "enviado",
            })
            conn.commit()
            row = conn.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        return _row_to_lead(conn, row)


def import_contacts(contacts):
    _ensure_init()
    imported = skipped = 0
    with _conn() as conn:
        for c in contacts:
            phone = _digits(c["telefone_e164"])
            if conn.execute("SELECT 1 FROM leads WHERE phone=?", (phone,)).fetchone():
                skipped += 1
                continue
            _insert_row(conn, phone, {
                "nome": c.get("nome", ""),
                "perfil": "PJ" if str(c.get("perfil", "")).startswith("PJ") else "PF",
                "origem": c.get("origem", "Brasil"),
                "pais": c.get("pais", "Brasil"),
                "stage": "pendente", "delivery": "pendente",
            })
            imported += 1
        conn.commit()
    return imported, skipped


def update_lead(phone, **fields):
    _ensure_init()
    with _conn() as conn:
        if not conn.execute("SELECT 1 FROM leads WHERE phone=?", (phone,)).fetchone():
            raise KeyError(f"Lead {phone} not found, call get_or_create_lead first")
        sets, vals = [], []
        for key, val in fields.items():
            if key == "signals":
                sets.append("signals=?"); vals.append(json.dumps(val))
            elif key in _WRITABLE_COLS:
                sets.append(f"{key}=?"); vals.append(val)
        if sets:
            vals.append(phone)
            conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE phone=?", vals)
            conn.commit()
        row = conn.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        return _row_to_lead(conn, row)


def append_history(phone, role, text):
    _ensure_init()
    with _conn() as conn:
        conn.execute("INSERT INTO history(phone,role,text) VALUES(?,?,?)", (phone, role, text))
        conn.commit()
        row = conn.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        return _row_to_lead(conn, row) if row else None


def set_stage(phone, stage):
    if stage not in STAGES:
        raise ValueError(f"Unknown stage: {stage}")
    return update_lead(phone, stage=stage)


def advance_delivery(phone, new_state):
    if new_state not in _DELIVERY_RANK:
        return None
    _ensure_init()
    with _conn() as conn:
        row = conn.execute("SELECT delivery FROM leads WHERE phone=?", (phone,)).fetchone()
        if not row:
            return None
        current = row["delivery"] or "pendente"
        if _DELIVERY_RANK.get(new_state, 0) > _DELIVERY_RANK.get(current, 0):
            conn.execute("UPDATE leads SET delivery=? WHERE phone=?", (new_state, phone))
            conn.commit()
        full = conn.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        return _row_to_lead(conn, full)


def all_leads():
    # History omitted here for efficiency (the contacts table and the sender
    # don't need it); use get_lead / hot_leads when the conversation is needed.
    _ensure_init()
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM leads ORDER BY nome").fetchall()
        return [_row_to_lead(conn, r, with_history=False) for r in rows]


def funnel_counts():
    _ensure_init()
    counts = {s: 0 for s in STAGES}
    with _conn() as conn:
        for r in conn.execute("SELECT stage, COUNT(*) AS c FROM leads GROUP BY stage"):
            counts[r["stage"]] = r["c"]
        total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    counts["total"] = total
    return counts


def delivery_counts():
    _ensure_init()
    counts = {s: 0 for s in DELIVERY_STATES}
    with _conn() as conn:
        for r in conn.execute("SELECT delivery, COUNT(*) AS c FROM leads GROUP BY delivery"):
            counts[r["delivery"]] = counts.get(r["delivery"], 0) + r["c"]
    return counts


def hot_leads():
    _ensure_init()
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM leads WHERE stage='quente' ORDER BY nome").fetchall()
        return [_row_to_lead(conn, r) for r in rows]
