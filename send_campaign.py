"""
Kicks off the pilot: reads the cleaned base, sends the rotating opener
templates on the warming ramp from Setup_Aquecimento_e_Meta_Onboarding.md,
and seeds lead_store so the webhook can pick up replies from there.

Only run this after templates are APPROVED (check WhatsApp Manager >
Gerenciar modelos) and the webhook is deployed and subscribed.

Usage:
    $env:PHONE_NUMBER_ID = "1187310294469355"
    $env:WHATSAPP_TOKEN = "<token>"
    python send_campaign.py --day 1

--day selects how many NEW contacts to message today, per the ramp:
day 1: 20, day 2: 30, day 3: 45, day 4: 60, day 5: 80, day 6: remainder.
Re-running with the same --day is safe, contacts already marked
"contatado" or beyond are skipped automatically.
"""

import argparse
import csv
import os
import sys
import time

import lead_store
import send

CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "clean_base_import_ready.csv")

RAMP = {1: 20, 2: 30, 3: 45, 4: 60, 5: 80, 6: None}  # None = remainder

PF_TEMPLATES = [
    "reativacao_pf_faria_lima_v1",
    "reativacao_pf_faria_lima_v2",
    "reativacao_pf_faria_lima_v3",
    "reativacao_pf_faria_lima_v4",
    "reativacao_pf_faria_lima_v5",
    "reativacao_pf_faria_lima_v6",
]
PJ_TEMPLATES = ["reativacao_pj_faria_lima_v1", "reativacao_pj_faria_lima_v2"]

COMPANY_MARKERS = ("ltda", "holding", "participa", "administra", "imoveis", "imóveis")


def load_base():
    with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def perfil_for(nome):
    low = nome.lower()
    return "PJ" if any(m in low for m in COMPANY_MARKERS) else "PF"


def first_name(nome):
    return nome.strip().split(" ")[0] if nome.strip() else nome


def pick_template(perfil, index):
    pool = PJ_TEMPLATES if perfil == "PJ" else PF_TEMPLATES
    return pool[index % len(pool)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", type=int, required=True, choices=list(RAMP.keys()))
    parser.add_argument("--pause", type=float, default=1.5, help="seconds between sends")
    args = parser.parse_args()

    rows = load_base()
    pending = [r for r in rows if lead_store.get_lead(_digits(r["telefone_e164"])) is None]

    limit = RAMP[args.day]
    batch = pending if limit is None else pending[:limit]

    if not batch:
        print("Nothing left to send, base fully seeded.")
        return

    print(f"Day {args.day}: sending to {len(batch)} new contacts "
          f"({len(pending)} were still pending, {len(rows) - len(pending)} already contacted).")

    sent, failed = 0, 0
    for i, row in enumerate(batch):
        phone = _digits(row["telefone_e164"])
        nome = row["nome"]
        perfil = perfil_for(nome)
        template = pick_template(perfil, i)

        status, resp_text = send.send_template(phone, template, first_name(nome))
        if status in (200, 201):
            lead = lead_store.get_or_create_lead(phone, nome=nome, perfil=perfil)
            lead_store.update_lead(phone, last_template_used=template)
            lead_store.append_history(phone, "bot", f"[template:{template}]")
            sent += 1
            print(f"OK   {phone}  {nome[:30]:30s}  {template}")
        else:
            failed += 1
            print(f"FAIL {phone}  {nome[:30]:30s}  {template}  -> {status} {resp_text}")

        time.sleep(args.pause)

    print(f"\nDay {args.day} done: {sent} sent, {failed} failed.")


def _digits(e164):
    return e164.lstrip("+")


if __name__ == "__main__":
    main()
