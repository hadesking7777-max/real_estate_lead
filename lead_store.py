"""
SQLite-backed lead store. Same public API as the earlier JSON version, so
nothing else in the app changes; the storage underneath is now a single
SQLite file (transactional, safe for the concurrent access that the webhook,
the background sender, and the UI will do once automation lands).

On first use it auto-migrates a legacy leads.json (if present) into the DB,
so existing imported contacts and any conversation history carry over.
"""

import functools
import json
import os
import re
import sqlite3
import threading
import time

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
_WRITABLE_COLS = ["nome", "email", "perfil", "origem", "pais", "stage", "delivery",
                  "last_template_used", "last_wamid", "followup_count", "last_send_ts",
                  "last_error"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  phone TEXT PRIMARY KEY,
  nome TEXT,
  email TEXT,
  perfil TEXT,
  origem TEXT,
  pais TEXT,
  stage TEXT,
  delivery TEXT,
  signals TEXT,
  last_template_used TEXT,
  last_wamid TEXT,
  followup_count INTEGER DEFAULT 0,
  last_send_ts REAL
);
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT,
  role TEXT,
  text TEXT
);
CREATE TABLE IF NOT EXISTS campaign (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  status TEXT DEFAULT 'idle',
  start_ts REAL,
  fail_streak INTEGER DEFAULT 0,
  manual_remaining INTEGER DEFAULT 0,
  manual_total INTEGER DEFAULT 0,
  outreach_paused INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS sends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT,
  kind TEXT,
  template TEXT,
  ts REAL,
  ok INTEGER
);
CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT,
  tag TEXT,
  UNIQUE(phone, tag)
);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT,
  text TEXT,
  ts REAL
);
CREATE TABLE IF NOT EXISTS state_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT,
  ts REAL,
  actor TEXT,
  field TEXT,
  from_val TEXT,
  to_val TEXT,
  source TEXT
);
CREATE TABLE IF NOT EXISTS processed_messages (
  message_id TEXT PRIMARY KEY,
  ts REAL
);
CREATE INDEX IF NOT EXISTS idx_state_log_phone ON state_log(phone);
CREATE INDEX IF NOT EXISTS idx_history_phone ON history(phone);
CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(stage);
CREATE INDEX IF NOT EXISTS idx_leads_delivery ON leads(delivery);
CREATE INDEX IF NOT EXISTS idx_sends_ts ON sends(ts);
CREATE INDEX IF NOT EXISTS idx_tags_phone ON tags(phone);
CREATE INDEX IF NOT EXISTS idx_notes_phone ON notes(phone);
"""


def _digits(phone):
    return re.sub(r"\D", "", str(phone))


def _empty_signals():
    return {k: None for k in SIGNAL_KEYS}


def _conn():
    # timeout is how long sqlite3 internally waits/retries before raising
    # "database is locked" -- 30s covers realistic contention (a large import
    # holding the writer lock while the webhook/scheduler also want to write).
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _retry_on_locked(max_attempts=3, base_delay=0.2):
    """Decorator: outer safety net on top of _conn()'s own busy-timeout. If a
    write still hits "database is locked" after that internal wait, retry the
    whole call a few times with backoff before giving up. Concurrent writers
    here are real: inbound webhooks, the scheduler's tick(), and panel actions
    all hit the same SQLite file.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return fn(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    attempt += 1
                    if "locked" not in str(e).lower() or attempt >= max_attempts:
                        raise
                    time.sleep(base_delay * attempt)
        return wrapper
    return decorator


def _ensure_init():
    if DB_PATH in _initialized_paths:
        return
    with _init_lock:
        if DB_PATH in _initialized_paths:
            return
        with _conn() as conn:
            conn.executescript(_SCHEMA)
            _migrate_schema(conn)
        _maybe_migrate_json()
        _initialized_paths.add(DB_PATH)


def _migrate_schema(conn):
    # add columns introduced after a DB was first created
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(leads)")}
    if "last_send_ts" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN last_send_ts REAL")
    if "email" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN email TEXT")
    if "last_error" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN last_error TEXT")
    camp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(campaign)")}
    if "manual_remaining" not in camp_cols:
        conn.execute("ALTER TABLE campaign ADD COLUMN manual_remaining INTEGER DEFAULT 0")
    if "manual_total" not in camp_cols:
        conn.execute("ALTER TABLE campaign ADD COLUMN manual_total INTEGER DEFAULT 0")
    if "auto_mode" not in camp_cols:
        conn.execute("ALTER TABLE campaign ADD COLUMN auto_mode INTEGER DEFAULT 0")
    if "outreach_paused" not in camp_cols:
        conn.execute("ALTER TABLE campaign ADD COLUMN outreach_paused INTEGER DEFAULT 0")
    # guarantee the single campaign row exists
    conn.execute("INSERT OR IGNORE INTO campaign(id, status, fail_streak) VALUES(1, 'idle', 0)")
    conn.commit()


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
        "INSERT OR IGNORE INTO leads(phone,nome,email,perfil,origem,pais,stage,delivery,"
        "signals,last_template_used,last_wamid,followup_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            phone, lead.get("nome", ""), lead.get("email", ""), lead.get("perfil", "PF"),
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
        "email": (row["email"] if "email" in row.keys() else "") or "",
        "perfil": row["perfil"] or "PF",
        "origem": row["origem"] or "Brasil",
        "pais": row["pais"] or "Brasil",
        "stage": row["stage"],
        "delivery": row["delivery"],
        "signals": json.loads(row["signals"]) if row["signals"] else _empty_signals(),
        "last_template_used": row["last_template_used"],
        "last_wamid": row["last_wamid"],
        "followup_count": row["followup_count"] or 0,
        "last_send_ts": row["last_send_ts"],
        "last_error": (row["last_error"] if "last_error" in row.keys() else "") or "",
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


@_retry_on_locked()
def import_contacts(contacts):
    _ensure_init()
    imported = skipped = 0
    with _conn() as conn:
        for c in contacts:
            phone = _digits(c["telefone_e164"])
            existing = conn.execute("SELECT email FROM leads WHERE phone=?", (phone,)).fetchone()
            if existing:
                # already a contact: keep it as-is, but backfill a missing email
                # so re-importing the same sheet fills emails without duplicating.
                if not (existing["email"] or "").strip() and c.get("email"):
                    conn.execute("UPDATE leads SET email=? WHERE phone=?", (c.get("email", ""), phone))
                skipped += 1
                continue
            _insert_row(conn, phone, {
                "nome": c.get("nome", ""),
                "email": c.get("email", ""),
                "perfil": "PJ" if str(c.get("perfil", "")).startswith("PJ") else "PF",
                "origem": c.get("origem", "Brasil"),
                "pais": c.get("pais", "Brasil"),
                "stage": "pendente", "delivery": "pendente",
            })
            imported += 1
        conn.commit()
    return imported, skipped


def delete_lead(phone):
    """Remove a contact and everything attached to it. Returns True if it existed."""
    _ensure_init()
    with _conn() as conn:
        existed = conn.execute("DELETE FROM leads WHERE phone=?", (phone,)).rowcount > 0
        for tbl in ("history", "tags", "notes", "sends", "state_log"):
            conn.execute(f"DELETE FROM {tbl} WHERE phone=?", (phone,))
        conn.commit()
        return existed


@_retry_on_locked()
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


@_retry_on_locked()
def append_history(phone, role, text):
    _ensure_init()
    with _conn() as conn:
        conn.execute("INSERT INTO history(phone,role,text) VALUES(?,?,?)", (phone, role, text))
        conn.commit()
        row = conn.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        return _row_to_lead(conn, row) if row else None


def _insert_state_log(conn, phone, field, from_val, to_val, actor, source, ts):
    conn.execute(
        "INSERT INTO state_log(phone,ts,actor,field,from_val,to_val,source) VALUES(?,?,?,?,?,?,?)",
        (phone, ts if ts is not None else time.time(), actor, field, from_val, to_val, source),
    )


@_retry_on_locked()
def log_state(phone, field, from_val, to_val, actor="auto", source="", ts=None):
    """Record a stage/delivery change for the contact's history."""
    _ensure_init()
    with _conn() as conn:
        _insert_state_log(conn, phone, field, from_val, to_val, actor, source, ts)
        conn.commit()


def get_state_log(phone, limit=100):
    _ensure_init()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ts, actor, field, from_val, to_val, source FROM state_log "
            "WHERE phone=? ORDER BY ts DESC, id DESC LIMIT ?", (phone, limit),
        ).fetchall()
        return [dict(r) for r in rows]


@_retry_on_locked()
def set_stage(phone, stage, actor="auto", source="", ts=None):
    if stage not in STAGES:
        raise ValueError(f"Unknown stage: {stage}")
    _ensure_init()
    with _conn() as conn:
        row = conn.execute("SELECT stage FROM leads WHERE phone=?", (phone,)).fetchone()
        if row is None:
            raise KeyError(f"Lead {phone} not found, call get_or_create_lead first")
        old = row["stage"]
        # the stage change and its audit-log entry commit together, atomically,
        # so a crash or a concurrent delete between them can't leave the audit
        # trail claiming a transition that never actually landed (or vice versa)
        conn.execute("UPDATE leads SET stage=? WHERE phone=?", (stage, phone))
        if old != stage:
            _insert_state_log(conn, phone, "stage", old, stage, actor, source, ts)
        conn.commit()
        full = conn.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        return _row_to_lead(conn, full)


@_retry_on_locked()
def set_delivery(phone, new_state, actor="auto", source="", ts=None):
    """Set the delivery state directly (may move backward) and log the change."""
    if new_state not in _DELIVERY_RANK:
        return None
    _ensure_init()
    with _conn() as conn:
        row = conn.execute("SELECT delivery FROM leads WHERE phone=?", (phone,)).fetchone()
        if not row:
            return None
        current = row["delivery"] or "pendente"
        if current != new_state:
            conn.execute("UPDATE leads SET delivery=? WHERE phone=?", (new_state, phone))
            _insert_state_log(conn, phone, "delivery", current, new_state, actor, source, ts)
            conn.commit()
        full = conn.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        return _row_to_lead(conn, full)


@_retry_on_locked()
def advance_delivery(phone, new_state, actor="auto", source="", ts=None):
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
            _insert_state_log(conn, phone, "delivery", current, new_state, actor, source, ts)
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


def hot_since_map():
    """phone -> ts of the most recent transition INTO stage='quente', for every
    phone that has one on record. Batched (one query, not one per card) so the
    panel can flag hot leads that have been sitting a while without closing.
    """
    _ensure_init()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT phone, MAX(ts) AS ts FROM state_log WHERE field='stage' AND to_val='quente' "
            "GROUP BY phone"
        ).fetchall()
        return {r["phone"]: r["ts"] for r in rows}


# ---------- campaign state + send log (used by the automation scheduler) ----------

def get_setting(key, default=None):
    _ensure_init()
    with _conn() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key, value):
    _ensure_init()
    with _conn() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()


def get_campaign():
    _ensure_init()
    with _conn() as conn:
        r = conn.execute(
            "SELECT status, start_ts, fail_streak, manual_remaining, manual_total, auto_mode, "
            "outreach_paused FROM campaign WHERE id=1"
        ).fetchone()
        return {"status": r["status"], "start_ts": r["start_ts"], "fail_streak": r["fail_streak"],
                "manual_remaining": r["manual_remaining"] or 0, "manual_total": r["manual_total"] or 0,
                "auto_mode": bool(r["auto_mode"]), "outreach_paused": bool(r["outreach_paused"])}


def set_campaign(**fields):
    allowed = {"status", "start_ts", "fail_streak", "manual_remaining", "manual_total", "auto_mode",
               "outreach_paused"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return get_campaign()
    _ensure_init()
    with _conn() as conn:
        conn.execute(f"UPDATE campaign SET {', '.join(sets)} WHERE id=1", vals)
        conn.commit()
    return get_campaign()


@_retry_on_locked()
def record_send(phone, kind, template, ts, ok):
    _ensure_init()
    with _conn() as conn:
        conn.execute("INSERT INTO sends(phone, kind, template, ts, ok) VALUES(?,?,?,?,?)",
                     (phone, kind, template, ts, 1 if ok else 0))
        conn.commit()


def already_processed(message_id, ts=None):
    """Idempotency guard for inbound webhook events: WhatsApp can redeliver the
    same message if it doesn't get a fast enough 200 response. Returns True if
    this message_id was already handled (caller should skip re-processing),
    False and records it as processed otherwise. Atomic via INSERT OR IGNORE
    plus rowcount, so two near-simultaneous deliveries can't both pass.
    """
    if not message_id:
        return False  # no id to dedupe on -- process it, better than dropping
    _ensure_init()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO processed_messages(message_id, ts) VALUES(?,?)",
            (message_id, ts if ts is not None else time.time()),
        )
        conn.commit()
        return cur.rowcount == 0  # 0 rows inserted -> the id was already there


def last_send_ts():
    _ensure_init()
    with _conn() as conn:
        r = conn.execute("SELECT MAX(ts) AS m FROM sends").fetchone()
        return r["m"]


def count_ok_sends_between(ts_from, ts_to):
    _ensure_init()
    with _conn() as conn:
        r = conn.execute("SELECT COUNT(*) AS c FROM sends WHERE ok=1 AND ts>=? AND ts<?",
                         (ts_from, ts_to)).fetchone()
        return r["c"]


def count_sends_between(ts_from, ts_to):
    """All send attempts (successful or not) in the window. Used for the daily
    ramp cap: a failed attempt still hit WhatsApp's API and can still affect
    the number's quality rating, so it must count against the cap same as a
    successful one -- otherwise scattered failures let the ramp be exceeded.
    """
    _ensure_init()
    with _conn() as conn:
        r = conn.execute("SELECT COUNT(*) AS c FROM sends WHERE ts>=? AND ts<?",
                         (ts_from, ts_to)).fetchone()
        return r["c"]


def count_state_transitions(field, to_val, ts_from, ts_to):
    """How many times `field` (e.g. 'stage') changed TO `to_val` (e.g. 'quente')
    within [ts_from, ts_to). Used for digest/summary reporting off the same
    audit trail set_stage/set_delivery/advance_delivery already write to.
    """
    _ensure_init()
    with _conn() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS c FROM state_log WHERE field=? AND to_val=? AND ts>=? AND ts<?",
            (field, to_val, ts_from, ts_to),
        ).fetchone()
        return r["c"]


def _sp_day_floor(ts):
    shifted = ts - 3 * 3600  # Sao Paulo UTC-3
    return (shifted - (shifted % 86400)) + 3 * 3600


def sends_by_day():
    """[(sp_day_start_ts, ok_count), ...] ascending, for the daily-sends chart."""
    _ensure_init()
    buckets = {}
    with _conn() as conn:
        for r in conn.execute("SELECT ts FROM sends WHERE ok=1"):
            day = _sp_day_floor(r["ts"])
            buckets[day] = buckets.get(day, 0) + 1
    return sorted(buckets.items())


def next_pending_opener():
    _ensure_init()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE stage='pendente' ORDER BY phone LIMIT 1"
        ).fetchone()
        return _row_to_lead(conn, row, with_history=False) if row else None


def add_tag(phone, tag):
    tag = (tag or "").strip()
    if not tag:
        return
    _ensure_init()
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO tags(phone, tag) VALUES(?,?)", (phone, tag))
        conn.commit()


def remove_tag(phone, tag):
    _ensure_init()
    with _conn() as conn:
        conn.execute("DELETE FROM tags WHERE phone=? AND tag=?", (phone, tag))
        conn.commit()


def get_tags(phone):
    _ensure_init()
    with _conn() as conn:
        return [r["tag"] for r in
                conn.execute("SELECT tag FROM tags WHERE phone=? ORDER BY tag", (phone,))]


def tags_map():
    """phone -> [tags], for the contacts table without an N+1."""
    _ensure_init()
    out = {}
    with _conn() as conn:
        for r in conn.execute("SELECT phone, tag FROM tags ORDER BY tag"):
            out.setdefault(r["phone"], []).append(r["tag"])
    return out


def all_tag_names():
    _ensure_init()
    with _conn() as conn:
        return [r["tag"] for r in
                conn.execute("SELECT DISTINCT tag FROM tags ORDER BY tag")]


def add_note(phone, text, ts):
    text = (text or "").strip()
    if not text:
        return
    _ensure_init()
    with _conn() as conn:
        conn.execute("INSERT INTO notes(phone, text, ts) VALUES(?,?,?)", (phone, text, ts))
        conn.commit()


def get_notes(phone):
    _ensure_init()
    with _conn() as conn:
        return [{"text": r["text"], "ts": r["ts"]} for r in
                conn.execute("SELECT text, ts FROM notes WHERE phone=? ORDER BY id DESC", (phone,))]


def due_followup(now, delay1, delay2):
    """Highest-priority follow-up due, or None.

    Targets only genuine non-responders: the opener went out (delivery is
    enviado/entregue/lido, never respondeu or falhou), the lead is still
    'contatado' (no AI stage change), and the pacing delay since the last send
    to that lead has elapsed. The delivery filter matters because an inbound
    reply flips delivery to 'respondeu' before qualification runs, so even a
    reply whose AI call errored (stage stuck at 'contatado') is excluded here.
    """
    _ensure_init()
    delivered = "delivery IN ('enviado','entregue','lido')"
    with _conn() as conn:
        row = conn.execute(
            f"SELECT * FROM leads WHERE stage='contatado' AND {delivered} AND followup_count=1 "
            "AND last_send_ts IS NOT NULL AND last_send_ts<=? ORDER BY last_send_ts LIMIT 1",
            (now - delay2,),
        ).fetchone()
        if row:
            return _row_to_lead(conn, row, with_history=False), "followup2"
        row = conn.execute(
            f"SELECT * FROM leads WHERE stage='contatado' AND {delivered} AND followup_count=0 "
            "AND last_send_ts IS NOT NULL AND last_send_ts<=? ORDER BY last_send_ts LIMIT 1",
            (now - delay1,),
        ).fetchone()
        if row:
            return _row_to_lead(conn, row, with_history=False), "followup1"
        return None
