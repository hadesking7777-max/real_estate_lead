"""
Claude-driven qualification engine for the Cyrela Living Faria Lima pilot.
Given a lead's history and their latest reply, produces the next WhatsApp
message and updates stage/signals per the roteiro's scoring rules.
"""

import os
import anthropic

import lead_store

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """Voce e um assistente que conduz, via WhatsApp, a qualificacao de investidores
imobiliarios para o corretor Lucas Guerra, no pre-lancamento Cyrela Living Faria Lima
(studios de 24 a 37 m2, 1 e 2 dorms, a partir de R$ 299 mil, a 5 min do metro Faria Lima
e do Shopping Eldorado).

Tom: direto ao ponto, natural, uma pergunta por vez, nunca em formato de interrogatorio.
Nunca usa emoji, nunca usa travessao ou meia risca (apenas hifen normal quando fizer parte
de uma palavra), nunca usa bullet points, nunca usa negrito ou qualquer marcacao. Texto
corrido, como uma pessoa digitando no celular.

Fluxo de qualificacao (uma pergunta de cada vez, na ordem que fizer sentido pela conversa):
1. Confirmar interesse: quer entender melhor essa oportunidade da Faria Lima?
2. Objetivo: valorizacao, renda de locacao, ou revenda?
3. Experiencia: ja investe em imovel? primeira aquisicao ou mais uma no portfolio?
4. Forma de pagamento: a vista, financiamento, ou aproveitar a tabela de pre-lancamento?
5. Volume: uma unidade ou mais de uma?
6. Timing: quer avancar agora ou esta so avaliando?

Classificacao de estagio:
quente: quer avancar agora ou em prazo curto, forma de pagamento definida (a vista ou
financiavel), confirma a faixa a partir de 299 mil.
morno: tem interesse mas quer pensar, timing indefinido, ou capital nao definido.
frio: sem interesse claro ou contato claramente errado.
opt_out: pediu pra parar, sair, ou nao receber mais mensagens (gatilhos: nao tenho
interesse, pare, sair, remover, nao me mande mais). Nesse caso a resposta deve ser uma
despedida educada confirmando a remocao, sem insistir.

Sempre responda usando a ferramenta responder_e_qualificar. Preencha os campos de sinais
apenas quando o lead de fato informar aquilo na conversa, deixe em branco (string vazia) o
que ainda nao foi dito. Avance no fluxo com uma pergunta natural sempre que fizer sentido,
sem repetir pergunta ja respondida."""

TOOL = {
    "name": "responder_e_qualificar",
    "description": "Gera a resposta de WhatsApp e atualiza o estado de qualificacao do lead",
    "input_schema": {
        "type": "object",
        "properties": {
            "reply_text": {
                "type": "string",
                "description": "Mensagem a enviar ao lead via WhatsApp, em portugues, tom direto, sem emoji, sem travessao, sem bullets",
            },
            "stage": {
                "type": "string",
                "enum": ["qualificando", "quente", "morno", "frio", "opt_out"],
            },
            "objetivo": {"type": "string"},
            "experiencia": {"type": "string"},
            "forma_pagamento": {"type": "string"},
            "quantidade_unidades": {"type": "string"},
            "timing": {"type": "string"},
            "motivo_estagio": {
                "type": "string",
                "description": "Nota curta interna sobre por que esse estagio foi escolhido",
            },
        },
        "required": ["reply_text", "stage", "motivo_estagio"],
    },
}


def _client():
    api_key = lead_store.get_setting("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Set ANTHROPIC_API_KEY (via Configuracoes or the environment) before "
                           "calling the qualification engine.")
    return anthropic.Anthropic(api_key=api_key)


def _history_to_messages(lead):
    messages = []
    for turn in lead["history"]:
        role = "assistant" if turn["role"] == "bot" else "user"
        messages.append({"role": role, "content": turn["text"]})
    return messages


def process_incoming(lead, incoming_text):
    """
    lead: dict from lead_store.get_or_create_lead
    incoming_text: the lead's latest WhatsApp message
    Returns: (reply_text, updates_dict) where updates_dict has stage + any signals to save.
    """
    client = _client()
    messages = _history_to_messages(lead) + [{"role": "user", "content": incoming_text}]

    known_signals = ", ".join(
        f"{k}={v}" for k, v in lead["signals"].items() if v
    ) or "nenhum ainda"

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT + f"\n\nNome do lead: {lead['nome'] or 'desconhecido'}. "
        f"Perfil: {lead['perfil']}. Sinais ja confirmados: {known_signals}.",
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "responder_e_qualificar"},
        messages=messages,
    )

    tool_use = next(b for b in resp.content if b.type == "tool_use")
    result = tool_use.input

    updates = {"stage": result["stage"]}
    signals = lead["signals"].copy()
    for key in ("objetivo", "experiencia", "forma_pagamento", "quantidade_unidades", "timing"):
        if result.get(key):
            signals[key] = result[key]
    updates["signals"] = signals

    return result["reply_text"], updates
