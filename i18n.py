"""
UI translation layer for the panel.

The panel is authored in Brazilian Portuguese. That source text doubles as the
translation KEY: T("Enviar agora") returns the same string for "br", the English
string for "en", and the European-Portuguese string for "pt". Anything without an
explicit entry falls back to the Brazilian source, so a missing translation never
breaks a page (it just stays in Portuguese).

The current language is read from flask.g (set per request from the ui_lang
cookie). Outside a request context it falls back to the default, so the render
helpers can still be called from tests.
"""

DEFAULT = "br"
LANGS = ("br", "pt", "en")
LANG_NAMES = {"br": "Portugues (BR)", "pt": "Portugues (PT)", "en": "English"}
# value for the <html lang="..."> attribute
HTML_LANG = {"br": "pt-BR", "pt": "pt-PT", "en": "en"}

try:
    from flask import g, has_request_context
except Exception:  # noqa: BLE001 - flask always present in prod; keep import-safe
    g = None
    def has_request_context():
        return False


def current():
    try:
        if has_request_context():
            lang = getattr(g, "ui_lang", DEFAULT)
            return lang if lang in LANGS else DEFAULT
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT


def html_lang():
    return HTML_LANG.get(current(), "pt-BR")


# English translations, keyed by the Brazilian source string.
EN = {
    # nav / chrome
    "Painel": "Dashboard",
    "Importar base": "Import base",
    "Trocar senha": "Change password",
    "Sair": "Log out",
    "Idioma": "Language",
    # page subtitles
    "Painel do piloto de reativacao": "Reactivation pilot dashboard",
    "Importacao de contatos": "Contact import",
    "Detalhe do contato": "Contact detail",
    "Contato": "Contact",
    # login
    "Entrar": "Sign in",
    "Painel de reativacao": "Reactivation panel",
    "Usuario": "Username",
    "Senha": "Password",
    "Usuario ou senha invalidos.": "Invalid username or password.",
    # account modal
    "Senha atual": "Current password",
    "Nova senha (minimo 6 caracteres)": "New password (minimum 6 characters)",
    "Confirmar nova senha": "Confirm new password",
    "Salvar": "Save",
    # campaign
    "Campanha": "Campaign",
    "Enviando &middot; faltam {n}": "Sending &middot; {n} left",
    "Pausada": "Paused",
    "Parada": "Stopped",
    "Envio pausado automaticamente apos varias falhas seguidas. Confirme os modelos aprovados e o token antes de enviar de novo.":
        "Sending was paused automatically after several failures in a row. Check the approved templates and the token before sending again.",
    "Total enviados {a} &middot; Pendentes {b}": "Total sent {a} &middot; Pending {b}",
    "Parar envio": "Stop sending",
    "Enviar agora": "Send now",
    "Quantos contatos enviar agora": "How many contacts to send now",
    # panel section headings
    "Funil": "Funnel",
    "Taxas de conversao": "Conversion rates",
    "Envios por dia": "Sends per day",
    "Leads quentes ({n})": "Hot leads ({n})",
    "Contatos por status ({n})": "Contacts by status ({n})",
    " &middot; {p} da etapa anterior": " &middot; {p} of previous stage",
    # rate tiles
    "Taxa de entrega": "Delivery rate",
    "Taxa de leitura": "Read rate",
    "Taxa de resposta": "Reply rate",
    "Taxa de qualificacao": "Qualification rate",
    "{n} de {d}": "{n} of {d}",
    # hot cards + signals
    "QUENTE": "HOT",
    "Objetivo": "Goal",
    "Experiencia": "Experience",
    "Forma de pagamento": "Payment method",
    "Unidades": "Units",
    "Timing": "Timing",
    "Nenhum lead quente ainda. Assim que um investidor esquentar, aparece aqui.":
        "No hot leads yet. As soon as an investor heats up, they show up here.",
    "(sem nome)": "(no name)",
    # board
    "Vazio": "Empty",
    "Nenhum contato ainda. Importe uma planilha para comecar.":
        "No contacts yet. Import a spreadsheet to get started.",
    "Buscar por nome, telefone, email ou tag...": "Search by name, phone, email or tag...",
    # stage labels
    "Pendentes": "Pending",
    "Contatados": "Contacted",
    "Responderam": "Replied",
    "Em Qualificacao": "Qualifying",
    "Em qualificacao": "Qualifying",
    "Quentes": "Hot",
    "Morno": "Warm",
    "Frio": "Cold",
    "Opt-out": "Opt-out",
    # delivery labels
    "Pendente": "Pending",
    "Enviado": "Sent",
    "Entregue": "Delivered",
    "Lido": "Read",
    "Respondeu": "Replied",
    "Falhou": "Failed",
    # conta toasts
    "Senha alterada com sucesso.": "Password changed successfully.",
    "Senha atual incorreta.": "Current password is incorrect.",
    "A nova senha precisa ter ao menos 6 caracteres.": "The new password must be at least 6 characters.",
    "A nova senha e a confirmacao nao conferem.": "The new password and its confirmation do not match.",
    # daily chart empty
    "Nenhum envio ainda. O grafico aparece aqui quando a campanha comecar a enviar.":
        "No sends yet. The chart shows up here once the campaign starts sending.",
    # contact page
    "Voltar ao painel": "Back to dashboard",
    "Voltar": "Back",
    "Contato nao encontrado": "Contact not found",
    "Contato nao encontrado.": "Contact not found.",
    "Abrir no WhatsApp": "Open in WhatsApp",
    "Motivo da falha": "Failure reason",
    "Qual o status do envio?": "What is the delivery status?",
    "Escolha como marcar este contato na coluna Contatados.": "Choose how to mark this contact in the Contacted column.",
    "Historico de estados": "State history",
    "Nenhuma mudanca de estado ainda.": "No state changes yet.",
    "Excluir contato": "Delete contact",
    "Excluir": "Delete",
    "Ja enviado": "Already sent",
    "{n} contatos ja enviados estao em Pendentes.": "{n} already-sent contacts are sitting in Pending.",
    "Mover para Contatados": "Move to Contacted",
    "Este contato ja recebeu a mensagem, mas esta em Pendentes. Provavelmente foi movido por engano.":
        "This contact was already messaged but is in Pending, it was probably moved here by mistake.",
    "Tem certeza que deseja excluir": "Are you sure you want to delete",
    "Esta acao nao pode ser desfeita.": "This action cannot be undone.",
    "Automatico": "Automatic",
    "Manual": "Manual",
    "Entrega": "Delivery",
    "IA": "AI",
    "Tags": "Tags",
    "Automaticas": "Automatic",
    "Manuais": "Manual",
    "Nenhuma tag manual.": "No manual tags.",
    "Nova tag...": "New tag...",
    "Adicionar": "Add",
    "Remover": "Remove",
    "Etapa": "Stage",
    "Atualizar etapa": "Update stage",
    "Sinais de qualificacao": "Qualification signals",
    "Notas": "Notes",
    "Escrever uma nota...": "Write a note...",
    "Adicionar nota": "Add note",
    "Nenhuma nota ainda.": "No notes yet.",
    "Conversa": "Conversation",
    "Nenhuma mensagem ainda.": "No messages yet.",
    "Abertura enviada": "Opener sent",
    "Follow-up 1 enviado": "Follow-up 1 sent",
    "Follow-up 2 enviado": "Follow-up 2 sent",
    "Mensagem enviada": "Message sent",
    "Investidor": "Investor",
    "Assistente": "Assistant",
    # import page
    "Importar planilha de contatos": "Import contacts spreadsheet",
    "Envie a planilha (.xlsx) com as colunas nome, telefone e email. A gente analisa, remove duplicados e mostra um resumo antes de importar de verdade.":
        "Upload the spreadsheet (.xlsx) with the columns name, phone and email. We analyze it, remove duplicates and show a summary before importing for real.",
    "Arraste a planilha aqui ou clique para selecionar": "Drag the spreadsheet here or click to select",
    "Apenas arquivos .xlsx": "Only .xlsx files",
    "Analisar planilha": "Analyze spreadsheet",
    "Cancelar": "Cancel",
    "Historico de execucoes": "Execution history",
    "Nenhuma execucao ainda.": "No runs yet.",
    "Analise": "Analysis",
    "Importacao": "Import",
    "Enviando": "Uploading",
    "Analisando": "Analyzing",
    "Esse arquivo nao e .xlsx. Envie uma planilha do Excel.": "That file is not .xlsx. Upload an Excel spreadsheet.",
    "Erro no envio ({s}). Tente de novo.": "Upload error ({s}). Try again.",
    "Falha de conexao no envio. Tente de novo.": "Connection failure during upload. Try again.",
    "Envie um arquivo .xlsx valido.": "Upload a valid .xlsx file.",
    "Nao consegui ler a planilha: {exc}": "Could not read the spreadsheet: {exc}",
    "Sessao de upload expirou, envie a planilha de novo.": "Upload session expired, send the spreadsheet again.",
    # import review
    "Revisao da base": "Base review",
    "Linhas na planilha": "Rows in spreadsheet",
    "Brasil limpos": "Brazil (clean)",
    "Internacionais": "International",
    "Duplicados removidos": "Duplicates removed",
    "Invalidos/ignorados": "Invalid/skipped",
    "Confirmar importacao": "Confirm import",
    "Serao importados <b>{n}</b> contatos do Brasil. Os {i} internacionais podem entrar junto (investidores de fora que compram em SP).":
        "<b>{n}</b> contacts from Brazil will be imported. The {i} international ones can come along (investors from abroad who buy in SP).",
    "Pais": "Country",
    "Contatos": "Contacts",
    "Incluir os {i} contatos internacionais": "Include the {i} international contacts",
    "Confirmar e importar": "Confirm and import",
    # import done
    "Importacao concluida": "Import complete",
    "{n} contatos importados{note}.": "{n} contacts imported{note}.",
    " (incluindo {i} internacionais)": " (including {i} international)",
    "{s} ja existiam na base e foram mantidos como estavam, sem sobrescrever conversas em andamento.":
        "{s} already existed in the base and were kept as they were, without overwriting ongoing conversations.",
    "Ver o painel": "View dashboard",
    "Importar outra planilha": "Import another spreadsheet",
    # state info popup descriptions (keyed by their Brazilian source text)
    "Controle manual do envio. Voce escolhe quantos contatos disparar agora e clica em Enviar agora. Pode repetir quantas vezes quiser. Os envios saem espacados automaticamente para proteger o numero.":
        "Manual send control. You choose how many contacts to fire now and click Send now. Repeat as many times as you like. Sends go out spaced automatically to protect the number.",
    "Contatos que ainda nao receberam nenhuma mensagem. Estao na fila para o primeiro contato quando a campanha rodar.":
        "Contacts that have not received any message yet. They are queued for the first contact when the campaign runs.",
    "Ja receberam a mensagem de abertura, mas ainda nao responderam. Aguardando resposta, ou entrando na cadencia de follow-up.":
        "Already received the opener but have not replied. Awaiting a reply, or entering the follow-up cadence.",
    "Responderam a primeira mensagem. A IA comeca a qualificacao a partir daqui.":
        "Replied to the first message. The AI starts qualification from here.",
    "Estao conversando com a IA agora, que mede intencao, capital, forma de pagamento e timing.":
        "Talking with the AI right now, which gauges intent, capital, payment method and timing.",
    "Leads qualificados, com alta intencao de compra. Ja entregues no seu WhatsApp para fechar.":
        "Qualified leads with high buying intent. Already delivered to your WhatsApp to close.",
    "Tem interesse, mas com horizonte mais longo ou capital ainda indefinido. Ficam sendo nutridos pela IA.":
        "Interested, but with a longer horizon or capital still undefined. Kept warm by the AI.",
    "Sem interesse real no momento. Saem do fluxo ativo da campanha.":
        "No real interest at the moment. They leave the active campaign flow.",
    "Pediram para nao receber mais mensagens. Removidos na hora e nunca mais contatados.":
        "Asked to stop receiving messages. Removed immediately and never contacted again.",
    "Percentual das mensagens enviadas que chegaram no aparelho do contato. Mede a qualidade da base e a saude do numero.":
        "Share of sent messages that reached the contact's device. Measures base quality and the number's health.",
    "Percentual das mensagens enviadas que foram lidas (abertas) pelo contato.":
        "Share of sent messages that were read (opened) by the contact.",
    "Percentual das mensagens enviadas que geraram uma resposta. E a principal metrica de reativacao.":
        "Share of sent messages that got a reply. It is the main reactivation metric.",
    "Dos que responderam, quantos viraram leads quentes (investidores prontos para fechar).":
        "Of those who replied, how many became hot leads (investors ready to close).",
}

# European Portuguese: only entries that differ from the Brazilian source.
PT = {
    "Trocar senha": "Alterar palavra-passe",
    "Sair": "Terminar sessao",
    "Painel do piloto de reativacao": "Painel do piloto de reativacao",
    "Importacao de contatos": "Importacao de contactos",
    "Detalhe do contato": "Detalhe do contacto",
    "Contato": "Contacto",
    "Usuario": "Utilizador",
    "Senha": "Palavra-passe",
    "Usuario ou senha invalidos.": "Utilizador ou palavra-passe invalidos.",
    "Senha atual": "Palavra-passe atual",
    "Nova senha (minimo 6 caracteres)": "Nova palavra-passe (minimo 6 caracteres)",
    "Confirmar nova senha": "Confirmar nova palavra-passe",
    "Enviando &middot; faltam {n}": "A enviar &middot; faltam {n}",
    "Envio pausado automaticamente apos varias falhas seguidas. Confirme os modelos aprovados e o token antes de enviar de novo.":
        "Envio pausado automaticamente apos varias falhas seguidas. Confirme os modelos aprovados e o token antes de enviar novamente.",
    "Contatos por status ({n})": "Contactos por estado ({n})",
    "Quantos contatos enviar agora": "Quantos contactos enviar agora",
    "Nenhum lead quente ainda. Assim que um investidor esquentar, aparece aqui.":
        "Ainda nao ha leads quentes. Assim que um investidor aquecer, aparece aqui.",
    "Nenhum contato ainda. Importe uma planilha para comecar.":
        "Ainda nao ha contactos. Importe uma folha de calculo para comecar.",
    "Buscar por nome, telefone, email ou tag...": "Procurar por nome, telefone, email ou etiqueta...",
    "Senha alterada com sucesso.": "Palavra-passe alterada com sucesso.",
    "Senha atual incorreta.": "Palavra-passe atual incorreta.",
    "A nova senha precisa ter ao menos 6 caracteres.": "A nova palavra-passe tem de ter pelo menos 6 caracteres.",
    "A nova senha e a confirmacao nao conferem.": "A nova palavra-passe e a confirmacao nao coincidem.",
    "Nenhum envio ainda. O grafico aparece aqui quando a campanha comecar a enviar.":
        "Ainda nao ha envios. O grafico aparece aqui quando a campanha comecar a enviar.",
    "Contato nao encontrado": "Contacto nao encontrado",
    "Contato nao encontrado.": "Contacto nao encontrado.",
    "Nova tag...": "Nova etiqueta...",
    "Motivo da falha": "Motivo da falha",
    "Excluir contato": "Eliminar contacto",
    "Excluir": "Eliminar",
    "Este contato ja recebeu a mensagem, mas esta em Pendentes. Provavelmente foi movido por engano.":
        "Este contacto ja recebeu a mensagem, mas esta em Pendentes. Provavelmente foi movido por engano.",
    "{n} contatos ja enviados estao em Pendentes.": "{n} contactos ja enviados estao em Pendentes.",
    "Mover para Contatados": "Mover para Contactados",
    "Tem certeza que deseja excluir": "Tem a certeza de que deseja eliminar",
    "Qual o status do envio?": "Qual o estado do envio?",
    "Escolha como marcar este contato na coluna Contatados.": "Escolha como marcar este contacto na coluna Contactados.",
    "Importar planilha de contatos": "Importar folha de calculo de contactos",
    "Envie a planilha (.xlsx) com as colunas nome, telefone e email. A gente analisa, remove duplicados e mostra um resumo antes de importar de verdade.":
        "Envie a folha de calculo (.xlsx) com as colunas nome, telefone e email. Analisamos, removemos duplicados e mostramos um resumo antes de importar a serio.",
    "Arraste a planilha aqui ou clique para selecionar": "Arraste a folha de calculo aqui ou clique para selecionar",
    "Apenas arquivos .xlsx": "Apenas ficheiros .xlsx",
    "Analisar planilha": "Analisar folha de calculo",
    "Esse arquivo nao e .xlsx. Envie uma planilha do Excel.": "Este ficheiro nao e .xlsx. Envie uma folha de calculo do Excel.",
    "Erro no envio ({s}). Tente de novo.": "Erro no envio ({s}). Tente novamente.",
    "Falha de conexao no envio. Tente de novo.": "Falha de ligacao no envio. Tente novamente.",
    "Envie um arquivo .xlsx valido.": "Envie um ficheiro .xlsx valido.",
    "Nao consegui ler a planilha: {exc}": "Nao consegui ler a folha de calculo: {exc}",
    "Sessao de upload expirou, envie a planilha de novo.": "A sessao de envio expirou, envie a folha de calculo novamente.",
    "Serao importados <b>{n}</b> contatos do Brasil. Os {i} internacionais podem entrar junto (investidores de fora que compram em SP).":
        "Serao importados <b>{n}</b> contactos do Brasil. Os {i} internacionais podem entrar tambem (investidores de fora que compram em SP).",
    "Contatos": "Contactos",
    "Incluir os {i} contatos internacionais": "Incluir os {i} contactos internacionais",
    "{n} contatos importados{note}.": "{n} contactos importados{note}.",
    "{s} ja existiam na base e foram mantidos como estavam, sem sobrescrever conversas em andamento.":
        "{s} ja existiam na base e foram mantidos como estavam, sem substituir conversas em curso.",
    "Importar outra planilha": "Importar outra folha de calculo",
}


def T(_src, **kw):
    lang = current()
    if lang == "en":
        out = EN.get(_src, _src)
    elif lang == "pt":
        out = PT.get(_src, _src)
    else:
        out = _src
    if kw:
        try:
            out = out.format(**kw)
        except (KeyError, IndexError, ValueError):
            return _src.format(**kw)
    return out
