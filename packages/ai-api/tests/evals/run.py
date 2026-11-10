#!/usr/bin/env python3
"""Run the pt-BR golden set against the REAL models (opt-in: it costs tokens).

    cd packages/ai-api
    uv run python -m tests.evals.run                 # the production chain
    uv run python -m tests.evals.run --model gemini  # Gemini alone
    uv run python -m tests.evals.run --repeat 3 --min-pass 0.9
    uv run python -m tests.evals.run --model gemini --delay 12  # free-tier key

Needs ``GEMINI_API_KEY`` (and ``DEEPSEEK_API_KEY`` for the chain) in the root
``.env`` or the environment. No database, Redis or chat client is used: the
harness stubs them (``tests/evals/harness.py``). Exits 1 when the share of
passed assertions is below ``--min-pass``.
"""

import argparse
import asyncio
import os
import sys

# Settings fields the harness never uses but config.py requires. Placeholders,
# never secrets; a real value from .env or the environment wins.
for _key, _value in {
    "DATABASE_URL": "postgresql://eval:eval@localhost:5432/eval",
    "AI_API_KEY": "eval",
    "WHATSAPP_API_KEY": "eval",
}.items():
    os.environ.setdefault(_key, _value)
os.environ["LOGFIRE_TOKEN"] = ""  # an eval run ships no telemetry


async def _main(model_choice: str, repeat: int, min_pass: float, delay: float) -> int:
    from pydantic_ai.models.google import GoogleModel

    from ai_api.agent.model_chain import google_provider
    from ai_api.config import settings
    from tests.evals.golden import golden_dataset
    from tests.evals.harness import EvalInput, EvalOutput, run_case

    model = None  # the production chain via build_runtime_model()
    if model_choice == "gemini":
        model = GoogleModel(settings.gemini_model, provider=google_provider)

    async def task(inputs: EvalInput) -> EvalOutput:
        # Free-tier keys allow ~15 requests a minute; a case makes 1-3.
        await asyncio.sleep(delay)
        return await run_case(inputs, model=model)

    report = await golden_dataset().evaluate(
        task, name=f"golden_pt_br[{model_choice}]", max_concurrency=1, repeat=repeat
    )
    report.print(include_input=True, include_output=False, include_reasons=True)
    # A case that errored (rate limit, model error) counts as failed, not skipped.
    passed = sum(r.value is True for c in report.cases for r in c.assertions.values())
    total = sum(len(c.assertions) for c in report.cases)
    total += len(report.failures) * len(golden_dataset().evaluators)
    rate = passed / total if total else 0.0
    print(f"\nAssertions passed: {passed}/{total} = {rate:.1%} (minimum {min_pass:.0%})")
    for failure in report.failures:
        print(f"ERROR {failure.name}: {failure.error_message[:300]}")
    # A tool failure means the harness or the code is broken, so the run says
    # nothing about the model: fail it whatever the pass rate.
    broken = [
        c.name
        for c in report.cases
        if any(name == "NoToolFailed" and not r.value for name, r in c.assertions.items())
    ]
    if broken:
        print(f"TOOL FAILURES in {broken}: fix the harness or the tool, then rerun.")
        return 1
    return 0 if rate >= min_pass else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", choices=["chain", "gemini"], default="chain")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--min-pass", type=float, default=0.9)
    parser.add_argument(
        "--delay", type=float, default=0.0, help="seconds to wait before each case (rate limits)"
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.model, args.repeat, args.min_pass, args.delay)))


if __name__ == "__main__":
    main()
