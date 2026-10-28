# Task tracking: Devflow

The full Devflow rules summarised in the root `CLAUDE.md` / `AGENTS.md`.

Work is tracked in **Devflow** (kanban), project **`CHATBOTS`** (org Terra Krya), shared by every bot repo (this one, curupira-bot, castanha-bot, fleetview), through the Devflow MCP server (the claude.ai Devflow connector in Claude Code, `flow-staging` in `agy`; both point at `https://staging.devflow.fractopia.org`). The board is in Brazilian Portuguese, so write titles, descriptions, comments and test instructions in pt-BR.

- **Board**: the default `Desenvolvimento` board (id `6aa2e19c6bf0a8f157d443e7`; ignore the second board with the same name). Columns: `Aguardando na fila` (backlog) → `Aguardando desenvolver` → `Em desenvolvimento` → `Aguardando teste` → `Testando` → `Aguardando publicacao` → `Em producao`; the last two count as done. The `Ciclos` board groups the same tasks by sprint
- **Before non-trivial work** (a feature, a bug fix, a migration): find the matching task and follow its description, often a markdown checklist to tick off. If none exists, create one: short title, markdown description, assignee `me`
- **Always set a priority** (`Prioridade`: `Baixa`, `Media`, `Alta`, `Urgente`) on every task you create, and before starting a task that has none. Pick it from the urgency the user expressed; ask when it is unclear
- **Starting**: move the task to `Em desenvolvimento`. **Finishing** (code done, PR open or merged): move it to `Aguardando teste`, passing the required `test_instructions` (concrete steps to check the change). Do not move it to `Aguardando publicacao` or `Em producao`, or complete it, unless the user says so, because testing and deploy are theirs
- Do not log time, change sprints or archive tasks unless asked
