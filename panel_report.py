"""
Minimal CLI panel for the pilot: prints the funnel counts and the hot-lead
cards, matching Painel_Funil_e_Handoff_Config.md. Good enough for 295
contacts; replace with a real dashboard only if this scales past a pilot.

Usage:
    python panel_report.py
"""

import lead_store

STAGE_LABELS = {
    "contatado": "Contatados",
    "respondeu": "Responderam",
    "qualificando": "Em Qualificacao",
    "quente": "Quentes",
    "morno": "Morno",
    "frio": "Frio",
    "opt_out": "Opt-out",
}


def print_funnel():
    counts = lead_store.funnel_counts()
    print("FUNIL")
    print(f"  Total importado: {counts['total']}")
    for stage in ["contatado", "respondeu", "qualificando", "quente"]:
        print(f"  {STAGE_LABELS[stage]:20s} {counts.get(stage, 0)}")
    print("\nParalelos")
    for stage in ["morno", "frio", "opt_out"]:
        print(f"  {STAGE_LABELS[stage]:20s} {counts.get(stage, 0)}")


def print_hot_cards():
    hot = lead_store.hot_leads()
    print(f"\nLEADS QUENTES ({len(hot)})")
    for lead in hot:
        s = lead["signals"]
        print("-" * 60)
        print(f"Nome: {lead['nome'] or '(sem nome)'} | Perfil: {lead['perfil']}")
        print(f"Telefone: {lead['phone']}")
        print(f"Objetivo: {s['objetivo'] or '-'}")
        print(f"Experiencia: {s['experiencia'] or '-'}")
        print(f"Forma de pagamento: {s['forma_pagamento'] or '-'}")
        print(f"Quantidade de unidades: {s['quantidade_unidades'] or '-'}")
        print(f"Timing: {s['timing'] or '-'}")
        lead_messages = [h for h in lead["history"] if h["role"] == "lead"]
        if lead_messages:
            print(f"Ultima mensagem do lead: {lead_messages[-1]['text']}")


if __name__ == "__main__":
    print_funnel()
    print_hot_cards()
