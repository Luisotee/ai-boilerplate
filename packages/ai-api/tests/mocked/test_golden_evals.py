"""The pt-BR golden set, run locally against a scripted FunctionModel (no cost).

The oracle model plays each case's expected tool calls, so this does NOT
measure tool choice (``python -m ai_api.scripts.run_evals`` does, against the
real models). It guards what the golden set relies on: every expected tool
still exists with those arguments, the tools run against the stubs, the
group-admin refusal reaches the tool reply, and the evaluators themselves.
"""

import pytest

from ai_api.evals.evaluators import NoMarkdown, guess_language
from ai_api.evals.golden import CASES, golden_dataset
from ai_api.evals.harness import EvalInput, EvalOutput, oracle_model, run_case


def _expected_by_input():
    keys = [case.inputs for case in CASES]
    assert len(set(keys)) == len(keys), "golden inputs must be unique"
    return {case.inputs: case.metadata for case in CASES}


async def test_golden_set_passes_with_the_oracle_model():
    expected = _expected_by_input()

    async def task(inputs: EvalInput) -> EvalOutput:
        return await run_case(inputs, model=oracle_model(expected[inputs]))

    report = await golden_dataset().evaluate(task, max_concurrency=1, progress=False)

    failures = [
        f"{case.name}: {name} — {result.reason}"
        for case in report.cases
        for name, result in case.assertions.items()
        if not result.value
    ]
    errors = [f"{f.name}: {f.error_message}" for f in report.failures]
    assert not errors, errors
    assert not failures, failures
    assert len(report.cases) == len(CASES)


async def test_a_slash_command_never_reaches_the_agent():
    out = await run_case(EvalInput("/memories"), model=None)
    assert out.route == "command"


@pytest.mark.parametrize(
    "text,lang",
    [
        ("Claro, posso te ajudar com isso agora mesmo.", "pt"),
        ("Sure, I can help you with that right now.", "en"),
        ("Claro, puedo ayudarte con eso ahora mismo, muy bien.", "es"),
    ],
)
def test_guess_language(text, lang):
    assert guess_language(text) == lang


@pytest.mark.parametrize(
    "reply,ok",
    [
        ("*negrito* e _itálico_\n- item", True),
        ("isso é **importante**", False),
        ("## Título\ntexto", False),
        ("veja [aqui](https://site.com)", False),
        ("| a | b |\n|---|---|\n| 1 | 2 |", False),
    ],
)
def test_no_markdown_evaluator(reply, ok):
    class Ctx:
        output = EvalOutput(route="agent", reply=reply)

    assert NoMarkdown().evaluate(Ctx()).value is ok
