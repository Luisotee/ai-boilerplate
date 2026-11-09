"""Checks applied to the golden-set cases (``pydantic_evals`` evaluators).

Every evaluator reads only what the case asks for in its ``metadata``
(``ExpectedBehaviour``) and returns True when there is nothing to check, so one
evaluator list serves every case.
"""

import re
from dataclasses import dataclass

from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorContext

from .harness import EvalInput, EvalOutput, ExpectedBehaviour

# A handful of very frequent function words per language. Enough to tell a
# Portuguese reply from an English or Spanish one; not a general detector.
_PT_WORDS = {
    "você", "voce", "não", "nao", "está", "esta", "é", "com", "para", "uma", "um", "os",
    "as", "do", "da", "dos", "das", "que", "isso", "aqui", "seu", "sua", "mais", "pode",
    "posso", "também", "tambem", "então", "entao", "já", "ja", "agora", "hoje", "sim",
    "no", "na", "em", "de", "o", "a", "e", "ao", "pra", "tá", "ta", "olá", "ola", "tudo",
    "vou", "disso", "te", "me", "mas", "por", "muito", "seus", "suas", "meu", "minha",
}  # fmt: skip
_EN_WORDS = {
    "you", "the", "is", "are", "and", "to", "of", "it", "this", "that", "with", "for",
    "your", "can", "here", "what", "have", "will", "i", "hello", "today", "not",
}  # fmt: skip
_ES_WORDS = {
    "usted", "está", "es", "el", "los", "las", "y", "para", "con", "que", "tu", "puedo",
    "hoy", "hola", "aquí", "también", "pero", "muy", "del", "una",
}  # fmt: skip

_MARKDOWN_PATTERNS = {
    "double-asterisk bold": re.compile(r"\*\*[^*\n]+\*\*"),
    "heading": re.compile(r"^\s{0,3}#{1,6}\s", re.MULTILINE),
    "markdown link": re.compile(r"\[[^\]\n]+\]\(https?://"),
    "table": re.compile(r"^\s*\|.*\|\s*$\n^\s*\|?\s*:?-{3,}", re.MULTILINE),
}

EvalContext = EvaluatorContext[EvalInput, EvalOutput, ExpectedBehaviour]


def guess_language(text: str) -> str:
    """'pt', 'en', 'es' or 'unknown', by counting frequent function words."""
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    if not words:
        return "unknown"
    scores = {
        "pt": sum(w in _PT_WORDS for w in words),
        "en": sum(w in _EN_WORDS for w in words),
        "es": sum(w in _ES_WORDS for w in words),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "unknown"


def _expected(ctx: EvalContext) -> ExpectedBehaviour:
    return ctx.metadata or ExpectedBehaviour()


@dataclass
class RoutedCorrectly(Evaluator[EvalInput, EvalOutput, ExpectedBehaviour]):
    """Slash commands are answered by commands.py; everything else reaches the agent."""

    def evaluate(self, ctx: EvalContext) -> EvaluationReason:
        want = _expected(ctx).route
        return EvaluationReason(
            value=ctx.output.route == want, reason=f"routed to {ctx.output.route}"
        )


@dataclass
class ExpectedToolsCalled(Evaluator[EvalInput, EvalOutput, ExpectedBehaviour]):
    """Every expected tool was called, with at least the expected arguments."""

    def evaluate(self, ctx: EvalContext) -> EvaluationReason:
        missing = []
        for want in _expected(ctx).tools:
            if not any(call.matches(want) for call in ctx.output.tool_calls):
                missing.append(f"{want.name}({want.args})")
        called = [f"{c.name}({c.args})" for c in ctx.output.tool_calls]
        return EvaluationReason(
            value=not missing,
            reason=f"missing {missing}; called {called}" if missing else "ok",
        )


@dataclass
class NoUnexpectedTools(Evaluator[EvalInput, EvalOutput, ExpectedBehaviour]):
    """For cases that must be answered without tools, or must avoid some tools."""

    def evaluate(self, ctx: EvalContext) -> EvaluationReason:
        expected = _expected(ctx)
        called = [c.name for c in ctx.output.tool_calls]
        if expected.no_tools and called:
            return EvaluationReason(value=False, reason=f"expected no tools, called {called}")
        bad = [name for name in called if name in expected.forbidden_tools]
        return EvaluationReason(value=not bad, reason=f"forbidden: {bad}" if bad else "ok")


@dataclass
class ToolReturnContains(Evaluator[EvalInput, EvalOutput, ExpectedBehaviour]):
    """A tool's own reply contains a marker (e.g. the group-admin refusal)."""

    def evaluate(self, ctx: EvalContext) -> EvaluationReason:
        for tool, marker in _expected(ctx).tool_return_contains.items():
            returned = ctx.output.tool_returns.get(tool, "")
            if marker not in returned:
                return EvaluationReason(value=False, reason=f"{tool} returned {returned!r}")
        return EvaluationReason(value=True, reason="ok")


@dataclass
class ReplyContains(Evaluator[EvalInput, EvalOutput, ExpectedBehaviour]):
    """The reply mentions every expected fragment (case-insensitive)."""

    def evaluate(self, ctx: EvalContext) -> EvaluationReason:
        reply = ctx.output.reply.lower()
        missing = [f for f in _expected(ctx).reply_contains if f.lower() not in reply]
        return EvaluationReason(value=not missing, reason=f"missing {missing}" if missing else "ok")


@dataclass
class RepliesInLanguage(Evaluator[EvalInput, EvalOutput, ExpectedBehaviour]):
    """The reply is in the language of the message being answered."""

    def evaluate(self, ctx: EvalContext) -> EvaluationReason:
        want = _expected(ctx).language
        if ctx.output.route != "agent" or not want:
            return EvaluationReason(value=True, reason="not checked")
        got = guess_language(ctx.output.reply)
        return EvaluationReason(value=got == want, reason=f"reply looks {got}")


@dataclass
class NoMarkdown(Evaluator[EvalInput, EvalOutput, ExpectedBehaviour]):
    """No Markdown the chat would show literally (``**``, headings, links, tables)."""

    def evaluate(self, ctx: EvalContext) -> EvaluationReason:
        found = [name for name, rx in _MARKDOWN_PATTERNS.items() if rx.search(ctx.output.reply)]
        return EvaluationReason(value=not found, reason=f"found {found}" if found else "ok")


ALL_EVALUATORS = [
    RoutedCorrectly(),
    ExpectedToolsCalled(),
    NoUnexpectedTools(),
    ToolReturnContains(),
    ReplyContains(),
    RepliesInLanguage(),
    NoMarkdown(),
]
