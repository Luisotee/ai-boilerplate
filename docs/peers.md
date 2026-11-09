# Peer projects

Six open-source chatbots that share our mechanisms are registered as agent-mesh peers in the group `chatbots`. Each is a clone under `~/research/chatbots/` with a standing Claude session that has already read both its project and this repo. Ask them before designing something they have built: `/delegate-claude` (or `/delegate-agy`) for one question, `/harvest-peers` for a research round. `mesh peers` lists them with their current notes.

| Peer | What it is | Ask it about |
|---|---|---|
| `hermes-agent-research` | Nous Research's Python agent with a Baileys bridge | Baileys bridge failures, LID↔phone mapping, cron invariants (`[SILENT]`, at-most-once), memory patch ops and budget, prompt-surface snapshot tests |
| `wa-llm-research` | WhatsApp group bot on nearly our stack (Pydantic AI, Logfire, pgvector) | Hybrid full-text + vector search, topic digests of group chat, scheduled group summaries, `pydantic_evals` |
| `nanobot-research` | Single-owner Python agent (WhatsApp via Neonize, not Baileys) | Background memory consolidation ("dream"), cron tool, merging bursts into a running turn, OpenAI-compatible API |
| `astrbot-research` | Large multi-platform bot framework, no WhatsApp (AGPL: read, never copy) | KB chunking and rank fusion, wake-word and bare-`@bot` handling, agent wakeups through the normal pipeline, agent-loop caps |
| `nanoclaw-research` | Containerised Claude-agent assistant (adapters on its `channels` branch) | Scheduled tasks (gate scripts, re-arming, one-door delivery), typed `@Name` mentions, sender approval, libsignal and `creds.json` lessons |
| `openclaw-research` | Very large TS agent gateway with WhatsApp and Telegram | Baileys reconnect, dedupe and creds persistence, its issue tracker of WhatsApp failure modes, cron semantics, prompt-cache stability |

The first round (2026-11-09) produced the `ai-boilerplate:` tasks in Devflow `CHATBOTS`; its answers are in the agent-mesh run `mfdcb` (`reports/peers-20261109T143516318004Z.md`).
