"""
Kicks off the pilot: sends the rotating opener templates to contacts that were
imported into the store (delivery = "pendente"), on the warming ramp from
Setup_Aquecimento_e_Meta_Onboarding.md, and marks each as "enviado" so the
webhook can take over from there.

Contacts must already be in the store, import a base first (via the UI at
/importar, or with import_base_cli.py). This sender is the single source of
"who gets messaged": it only ever touches leads still in "pendente".

Only run after templates are APPROVED (WhatsApp Manager > Gerenciar modelos)
and the webhook is deployed and subscribed.

Usage:
    $env:PHONE_NUMBER_ID = "1187310294469355"
    $env:WHATSAPP_TOKEN = "<token>"
    python send_campaign.py --day 1

--day selects how many NEW (still-pendente) contacts to message today:
day 1: 20, day 2: 30, day 3: 45, day 4: 60, day 5: 80, day 6: remainder.
Re-running the same day is safe: contacts already sent to are no longer
"pendente", so they are never messaged twice.
"""

import argparse
import json
import time

import lead_store
import send

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


def first_name(nome):
    nome = (nome or "").strip()
    return nome.split(" ")[0] if nome else nome


def pick_template(perfil, index):
    pool = PJ_TEMPLATES if str(perfil).startswith("PJ") else PF_TEMPLATES
    return pool[index % len(pool)]


def pending_leads():
    leads = [l for l in lead_store.all_leads() if l.get("delivery") == "pendente"]
    # deterministic order so re-runs and day boundaries are stable
    return sorted(leads, key=lambda l: l.get("phone", ""))


def _wamid(resp_text):
    try:
        return json.loads(resp_text)["messages"][0]["id"]
    except Exception:  # noqa: BLE001
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", type=int, required=True, choices=list(RAMP.keys()))
    parser.add_argument("--pause", type=float, default=1.5, help="seconds between sends")
    args = parser.parse_args()

    pending = pending_leads()
    limit = RAMP[args.day]
    batch = pending if limit is None else pending[:limit]

    if not batch:
        print("Nothing pending to send. Import a base first, or the base is fully contacted.")
        return

    print(f"Day {args.day}: sending to {len(batch)} of {len(pending)} pending contacts.")

    sent, failed = 0, 0
    for i, lead in enumerate(batch):
        phone = lead["phone"]
        nome = lead.get("nome", "")
        perfil = lead.get("perfil", "PF")
        template = pick_template(perfil, i)

        status, resp_text = send.send_template(phone, template, first_name(nome))
        if status in (200, 201):
            lead_store.update_lead(
                phone,
                stage="contatado",
                last_template_used=template,
                last_wamid=_wamid(resp_text),
            )
            lead_store.advance_delivery(phone, "enviado")
            lead_store.append_history(phone, "bot", f"[template:{template}]")
            sent += 1
            print(f"OK   {phone}  {nome[:30]:30s}  {template}")
        else:
            lead_store.update_lead(phone, last_template_used=template)
            lead_store.advance_delivery(phone, "falhou")
            failed += 1
            print(f"FAIL {phone}  {nome[:30]:30s}  {template}  -> {status} {resp_text}")

        time.sleep(args.pause)

    print(f"\nDay {args.day} done: {sent} sent, {failed} failed.")


if __name__ == "__main__":
    main()
