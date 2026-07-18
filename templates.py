"""
Approved WhatsApp template bodies, for DISPLAY only.

Templates are sent to WhatsApp by name (the text lives on Meta's side), so the
conversation only recorded a marker like "[opener:reativacao_pf_faria_lima_v5]".
To show the operator the actual message that went out, we keep the same bodies
here and render them (substituting {{1}} with the contact's first name).

Keep these in sync with submit_templates.py, the script that registered them.
"""

TEMPLATE_BODIES = {
    "reativacao_pf_faria_lima_v1": (
        "Cyrela na Faria Lima, do lado do Shopping Eldorado, a partir de 299 mil.\n"
        "{{1}}, apareceu essa oportunidade agora e lembrei de voce.\n"
        "Studios pra investidor, num dos eixos mais valorizados de Sao Paulo, a 5 min do metro Faria Lima.\n"
        "Faz sentido eu te mostrar os numeros?"
    ),
    "reativacao_pf_faria_lima_v2": (
        "Pre-lancamento novo na Faria Lima.\n"
        "{{1}}, studios Cyrela a partir de 299 mil, endereco colado no Shopping Eldorado e a 5 min do metro.\n"
        "E o tipo de metro quadrado que investidor gosta: liquidez e valorizacao.\n"
        "Quer que eu te passe os detalhes?"
    ),
    "reativacao_pf_faria_lima_v3": (
        "299 mil num studio Cyrela na Faria Lima, {{1}}. Pre-lancamento.\n"
        "5 min do metro, do lado do Eldorado, lazer completo.\n"
        "Preco de entrada raro pra essa regiao.\n"
        "Posso te mandar a tabela e as condicoes?"
    ),
    "reativacao_pf_faria_lima_v4": (
        "Oportunidade nova pra investir na Faria Lima.\n"
        "{{1}}, e o pre-lancamento Cyrela Living, studios de 24 a 37 m2, 1 e 2 dorms, a partir de 299 mil.\n"
        "Regiao disputada, alto potencial de locacao e valorizacao.\n"
        "Faz sentido eu te apresentar?"
    ),
    "reativacao_pf_faria_lima_v5": (
        "Saiu pre-lancamento na Faria Lima, {{1}}, e e a sua praia de investimento.\n"
        "Studios Cyrela a partir de 299 mil, a 5 min do metro e do Shopping Eldorado.\n"
        "Condicao de investidor, antes de abrir pro publico geral.\n"
        "Quer que eu te mostre?"
    ),
    "reativacao_pf_faria_lima_v6": (
        "Direto ao ponto: saiu pre-lancamento na Faria Lima.\n"
        "{{1}}, Cyrela, studio a partir de 299 mil.\n"
        "Endereco estrategico, metro e Eldorado do lado.\n"
        "Ativo redondo pra quem investe em Sao Paulo.\n"
        "Te mando os detalhes?"
    ),
    "reativacao_pj_faria_lima_v1": (
        "Oportunidade de aquisicao na Faria Lima.\n"
        "{{1}}, pre-lancamento Cyrela Living, studios a partir de 299 mil.\n"
        "Endereco estrategico, a 5 min do metro e do Shopping Eldorado, com bom potencial de valorizacao e locacao.\n"
        "Posso encaminhar a tabela e as condicoes pra avaliacao?"
    ),
    "reativacao_pj_faria_lima_v2": (
        "Prezados {{1}}, pre-lancamento Cyrela na Faria Lima, studios de 24 a 37 m2 a partir de 299 mil.\n"
        "Ativo com liquidez e valorizacao numa das regioes mais disputadas de Sao Paulo.\n"
        "Faz sentido eu enviar o material completo pra voces analisarem?"
    ),
    "followup1_faria_lima_a": (
        "Passando rapidinho.\n"
        "{{1}}, aquele studio Cyrela na Faria Lima a partir de 299 mil ainda esta de pe. "
        "Sei que a correria aperta. Quer que eu te mande a tabela?"
    ),
    "followup1_faria_lima_b": (
        "So confirmando que chegou.\n"
        "{{1}}, pre-lancamento Faria Lima, 299 mil, condicoes de investidor. "
        "Te passo os detalhes?"
    ),
    "followup2_faria_lima_a": (
        "Ultima mensagem, pra nao incomodar.\n"
        "{{1}}, se quiser olhar a oportunidade da Faria Lima, e so me chamar aqui. Abraco."
    ),
    "followup2_faria_lima_b": (
        "Vou parar por aqui, pra nao ser inconveniente.\n"
        "{{1}}, quando quiser ver o pre-lancamento da Faria Lima, me da um oi. Abraco."
    ),
}


def render(name, first_name):
    """Return the template body with {{1}} filled in, or None if unknown."""
    body = TEMPLATE_BODIES.get(name)
    if not body:
        return None
    return body.replace("{{1}}", (first_name or "").strip()).strip()
