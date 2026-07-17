"""
Reusable base-analysis logic, the same cleaning we ran by hand earlier, now
callable from the web UI. Takes an .xlsx path, returns a structured analysis
(clean BR + international contacts, duplicates removed, per-country breakdown)
without touching the lead store. Importing is a separate explicit step.
"""

import re

import openpyxl

COMPANY = re.compile(
    r"\b(ltda|ltd|s/?a|eireli|administra|imoveis|imóveis|holding|participa|"
    r"contab|advocacia|comercio|comércio|servi|medicos|médicos)\b",
    re.I,
)

# Country dialing codes we expect to see in this kind of base. Longest-prefix wins.
CC = {
    "55": "Brasil", "1": "EUA/Canada", "52": "Mexico", "244": "Angola",
    "353": "Irlanda", "33": "Franca", "44": "Reino Unido", "34": "Espanha",
    "39": "Italia", "965": "Kuwait", "41": "Suica", "351": "Portugal",
    "31": "Holanda",
}


def _s(x):
    return str(x).strip() if x is not None else ""


def _digits(p):
    return re.sub(r"\D", "", p)


def _country(e164_digits):
    for length in (3, 2, 1):
        if e164_digits[:length] in CC:
            return CC[e164_digits[:length]]
    return "Internacional"


def analyze(xlsx_path):
    """
    Returns a dict:
      {
        "total_rows": int,
        "clean": [ {nome, telefone_e164, email, origem, pais, perfil, segmento}, ... ],
        "internacionais": [ ... same shape ... ],
        "removed_duplicates": int,
        "landlines_or_invalid": int,
        "by_country": [ (pais, count), ... ],
        "sp_capital": int,
        "pj": int,
        "header": [...],
      }
    'clean' is Brazil-only; 'internacionais' is kept separate so the UI can
    let the user choose whether to include them.
    """
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        ws = wb.active
        rows = [r for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()  # read-only mode holds the file handle; release it so the upload can be deleted
    header = [_s(c) for c in rows[0]] if rows else []
    data_rows = [r for r in rows[1:] if any(c is not None for c in r)]

    seen = set()
    clean, intl = [], []
    removed_dupes = 0
    invalid = 0

    for r in data_rows:
        nome, tel, email = _s(r[0]), _s(r[1] if len(r) > 1 else ""), _s(r[2] if len(r) > 2 else "")
        d = _digits(tel)
        if not d:
            invalid += 1
            continue

        is_intl = tel.strip().startswith("+") and not tel.strip().startswith("+55")
        if is_intl:
            e164 = "+" + d
            origem, pais = "Internacional", _country(d)
        else:
            local = d[2:] if d.startswith("55") and len(d) in (12, 13) else d
            if len(local) != 11:
                invalid += 1
                continue
            e164 = "+55" + local
            origem, pais = "Brasil", "Brasil"

        if e164 in seen:
            removed_dupes += 1
            continue
        seen.add(e164)

        perfil = "PJ/Holding" if COMPANY.search(nome) else "PF"
        seg = "SP capital" if e164.startswith("+5511") else (pais if is_intl else "Interior/UF")
        rec = {
            "nome": nome, "telefone_e164": e164, "email": email,
            "origem": origem, "pais": pais, "perfil": perfil, "segmento": seg,
        }
        (intl if is_intl else clean).append(rec)

    from collections import Counter
    by_country = Counter(c["pais"] for c in intl).most_common()
    sp_capital = sum(1 for c in clean if c["segmento"] == "SP capital")
    pj = sum(1 for c in (clean + intl) if c["perfil"] == "PJ/Holding")

    return {
        "total_rows": len(data_rows),
        "clean": clean,
        "internacionais": intl,
        "removed_duplicates": removed_dupes,
        "landlines_or_invalid": invalid,
        "by_country": by_country,
        "sp_capital": sp_capital,
        "pj": pj,
        "header": header,
    }
