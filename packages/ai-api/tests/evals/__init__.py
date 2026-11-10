"""pt-BR golden set for the chat agent, run with ``pydantic_evals``.

- ``golden.py``: the cases (tool choice, command vs chat, group replies,
  language, format).
- ``evaluators.py``: the checks applied to every case.
- ``harness.py``: runs one case through the production agent path with the
  database, network and chat clients stubbed, and a ``FunctionModel`` oracle
  for the free local run.

Local run (no cost, part of the test suite): ``tests/mocked/test_golden_evals.py``.
Real models (opt-in, costs tokens): ``uv run python -m tests.evals.run``.
"""
