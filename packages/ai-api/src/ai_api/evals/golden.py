"""The pt-BR golden set: small, real phrasing, one behaviour per case.

Add a case when the bot gets something wrong in production: the message, the
chat it came from, and what should have happened. Tool names here must match
the live schemas; the local run (``tests/mocked/test_golden_evals.py``) fails
when a tool is renamed or its arguments change, which is the point.
"""

from pydantic_evals import Case, Dataset

from .evaluators import ALL_EVALUATORS
from .harness import EvalInput, ExpectedBehaviour, ExpectedCall

_REFUSAL = "Only a group admin"


def _case(name: str, inputs: EvalInput, expected: ExpectedBehaviour) -> Case:
    return Case(name=name, inputs=inputs, metadata=expected)


CASES = [
    # --- command vs chat: slash commands never reach the agent ---------------
    _case(
        "comando_settings",
        EvalInput("/settings"),
        ExpectedBehaviour(route="command", language=None),
    ),
    _case(
        "comando_com_mencao_no_grupo",
        EvalInput("@Assistente /tts on", chat="group", is_group_admin=True),
        ExpectedBehaviour(route="command", language=None),
    ),
    # --- tool choice ----------------------------------------------------------
    _case(
        "conversa_sem_ferramenta",
        EvalInput("oi, tudo bem?"),
        ExpectedBehaviour(no_tools=True, oracle_reply="Oi! Tudo ótimo, e com você?"),
    ),
    _case(
        "clima",
        EvalInput("como tá o tempo em Belém agora?"),
        ExpectedBehaviour(
            tools=[ExpectedCall("get_weather", {"city": "Belém"})],
            reply_contains=["31"],
            oracle_reply="Em Belém agora faz 31°C, parcialmente nublado.",
        ),
    ),
    _case(
        "calculo",
        EvalInput("quanto é 15% de 47,80?"),
        ExpectedBehaviour(
            tools=[ExpectedCall("calculate", {"expression": "47.8"})],
            reply_contains=["7,17"],
            oracle_reply="15% de 47,80 dá 7,17.",
        ),
    ),
    _case(
        "desligar_audio",
        EvalInput("para de me mandar áudio, prefiro texto"),
        ExpectedBehaviour(
            tools=[ExpectedCall("update_tts_settings", {"enabled": False})],
            oracle_reply="Pronto, desliguei as respostas em áudio.",
        ),
    ),
    _case(
        "transcricao_em_espanhol",
        EvalInput("meus áudios vão ser em espanhol a partir de agora, ajusta a transcrição"),
        ExpectedBehaviour(
            tools=[ExpectedCall("update_stt_settings", {"language": "es"})],
            oracle_reply="Feito: vou transcrever seus áudios em espanhol.",
        ),
    ),
    _case(
        "ver_configuracoes",
        EvalInput("quais são minhas configurações?"),
        ExpectedBehaviour(
            tools=[ExpectedCall("get_user_settings")],
            oracle_reply="Áudio desligado e transcrição automática.",
        ),
    ),
    _case(
        "parar_avisos",
        EvalInput("não quero mais receber essas novidades de vocês"),
        ExpectedBehaviour(
            tools=[ExpectedCall("set_broadcast_subscription", {"subscribed": False})],
            oracle_reply="Pronto, você não vai mais receber os avisos.",
        ),
    ),
    _case(
        "lembrar_fato",
        EvalInput("lembra que eu sou alérgico a camarão"),
        ExpectedBehaviour(
            tools=[ExpectedCall("update_core_memory", {"content": "camarão"})],
            oracle_reply="Anotado, vou lembrar.",
        ),
    ),
    _case(
        "historico_de_ontem",
        EvalInput("sobre o que a gente conversou ontem?"),
        ExpectedBehaviour(
            tools=[ExpectedCall("get_chat_history")],
            oracle_reply="Não encontrei mensagens de ontem nesta conversa.",
        ),
    ),
    _case(
        "noticia_atual",
        EvalInput("qual o preço da castanha hoje? procura na internet"),
        ExpectedBehaviour(
            tools=[ExpectedCall("web_search", {"query": "castanha"})],
            oracle_reply="Encontrei uma notícia recente sobre o preço da castanha.",
        ),
    ),
    # --- group replies --------------------------------------------------------
    _case(
        "grupo_membro_nao_apaga_historico",
        EvalInput("apaga todo o histórico do grupo", chat="group", is_group_admin=False),
        ExpectedBehaviour(
            tools=[ExpectedCall("clean_user_data")],
            tool_return_contains={"clean_user_data": _REFUSAL},
            oracle_reply="Só um admin do grupo pode apagar o histórico.",
        ),
    ),
    _case(
        "grupo_admin_desliga_audio",
        EvalInput("desliga o áudio aqui no grupo", chat="group", is_group_admin=True),
        ExpectedBehaviour(
            tools=[ExpectedCall("update_tts_settings", {"enabled": False})],
            forbidden_tools=["clean_user_data"],
            oracle_reply="Pronto, desliguei o áudio no grupo.",
        ),
    ),
    # --- language -------------------------------------------------------------
    _case(
        "idioma_apos_historico_em_ingles",
        EvalInput(
            "e você, consegue me explicar isso em poucas palavras?",
            history=(
                ("user", "Can you tell me what photosynthesis is?"),
                ("assistant", "Photosynthesis is how plants turn light into energy."),
            ),
        ),
        ExpectedBehaviour(
            no_tools=True,
            oracle_reply="Claro: é o processo em que a planta usa a luz para produzir energia.",
        ),
    ),
    _case(
        "idioma_ingles_responde_ingles",
        EvalInput("what can you do for me?"),
        ExpectedBehaviour(
            no_tools=True,
            language="en",
            oracle_reply="I can answer questions, search the web and remember your preferences.",
        ),
    ),
    # --- format ---------------------------------------------------------------
    _case(
        "formato_lista_sem_markdown",
        EvalInput("me dá uma lista curta com 3 dicas para economizar energia em casa"),
        ExpectedBehaviour(
            no_tools=True,
            oracle_reply=(
                "*Três dicas*\n"
                "- Desligue aparelhos da tomada\n"
                "- Use lâmpadas LED\n"
                "- Aproveite a luz do dia"
            ),
        ),
    ),
]


def golden_dataset() -> Dataset:
    return Dataset(name="golden_pt_br", cases=CASES, evaluators=ALL_EVALUATORS)
