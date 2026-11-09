# Group topics

An opt-in job in the stream worker summarises each group's finished conversations into `{subject, summary}` topics. In a group, `search_conversation_history` searches those topics first and falls back to the per-message search when none match. Idea and wa_llm bugs to avoid: the `wa-llm-research` peer report (Devflow `6af1dcc1be4cc60a82acc1b6`).

## Switch and cost

- `GROUP_TOPICS_ENABLED` (default `false`, hot via `PATCH /admin/settings` as `group_topics_enabled`). Both the worker loop and the search tool read it through `runtime_config.get()`, so turning it off also stops topic search.
- `GROUP_TOPICS_INTERVAL_MINUTES` (default `60`, min `5`, read once at worker start): time between runs.
- **Privacy**: off by default because it sends every group message, mentioned or not, to the chat model, and to DeepSeek (China) when `DEEPSEEK_API_KEY` is set. Without it, a non-mentioned message reaches a chat model only as one of the last `HISTORY_LIMIT_GROUP` history lines when someone mentions the bot. (Every group message already goes to Google's embedding API either way.)
- **Cost**: one model call per finished block, no tool schemas. A typical active group (100-300 messages a day in 2-5 conversations) costs about 3-6 calls a day, each about 2-6k input and ~300 output tokens: around US$ 0.01 per group per day on `gemini-3.1-flash-lite`, less on `deepseek-flash`. One embedding per topic. A group with nothing new costs one indexed query per run. A first run reads at most the last 7 days, and each run summarises at most 10 blocks per group.

## How a run works

`streams/group_topics.py` `run_group_topics_loop` → `run_group_topics_once` → `rag/group_topics.py` `ingest_group` per group.

1. **Lease**: `SET group_topics:lock NX EX 1800`, so one worker process runs at a time; released by token at the end. A run stops starting new groups after 15 minutes; groups that waited longest go first.
2. **Groups**: `users.conversation_type = 'group'` rows with messages after their cursor (`groups_with_new_messages`).
3. **Read**: messages after the cursor, ordered by `(timestamp, id)` with a row comparison (ties can't skip or repeat), plus the 5 messages before them as context.
4. **Split** (`split_into_blocks`, pure): a pause over 2 h ends a conversation; one over 200 messages is cut into 200-message pieces; a piece under 25 is merged into the next. Only finished conversations are taken: a chat still going (last message under 2 h old) is held, and a finished piece under 25 waits for the next conversation until it is 24 h old. Everything taken is at least 2 h old, so a message committed late (timestamps are insert-time) can't land behind the cursor. Each block carries the 5 messages before it, marked as context only.
5. **Summarise** (`summarise_block`): a narrow `Agent(output_type=list[Topic])` with no tools, on `build_runtime_model()` (DeepSeek → Gemini, honouring `/admin` model overrides and the DeepSeek cooldown), inside `asyncio.timeout(120)` because non-streaming calls don't get `GuardedModel`'s first-chunk timeout. No `instrument=` (Logfire's global instrumentation applies). At most 5 topics; each message is clipped to 800 characters. **Language**: the prompt asks for the language most of the messages are written in, so topics follow the chat (and the bot, which answers in it), and members' queries meet topics in the same language.
6. **Embed** with `embed_documents` (one batch call, `RETRIEVAL_DOCUMENT`, raises on failure) the text `# subject\nsummary`.
7. **Store** (`store_block`), in ONE transaction: insert the topics (`ON CONFLICT DO NOTHING`) and move the cursor to the block's last message. It first takes `FOR SHARE` on that message: if `/clean` deleted it, nothing is stored; if `/clean` runs at the same moment, its DELETE waits for this commit and then deletes these topics too.

## The two wa_llm bugs, avoided

- **Watermark**: `group_topic_cursors` stores the `(timestamp, id)` of the last message of the last stored block, never `now()`. wa_llm set `now()` after a minutes-long run, so messages saved meanwhile were never ingested. The cursor update only moves forward (`WHERE (old) < (new)`).
- **Topic ids**: `uuid5(namespace, f"{block_key}:{n}")`, where `block_key` is sha256 of the block's ordered message ids. wa_llm hashed the LLM's subject, so re-running a block after a crash duplicated its topics. Here topics and cursor commit together, and a re-run hits the same ids.

## Failures

- **Provider unavailable** (`ModelAPIError`, `FallbackExceptionGroup`, `openai.APIError`, `httpx.TransportError`, timeout, Gemini `ServerError`): the run stops, nothing is counted, the next run retries.
- **Anything else** (output validation exhausted, a rejected embedding): counted in Redis `group_topics:fail:{group_id}:{block_key}` (7-day TTL). The group waits on that block; after 3 failed runs the block is skipped (cursor moved past it, no topics) with a warning, so one bad block can't stop a group's topics forever.
- One group's unexpected error is logged and the run goes on to the next group.

## Search

`agent/tools/search.py` `search_conversation_history`: when the chat is a group (`is_group_jid`) and the setting is on, `search_group_topics` runs first with the run's own `users.id`; any match returns the topics (subject, time range, message count, summary). No match falls back to the message search. The tool's signature and docstring did not change. A private chat never takes this branch, and `get_group_context` (the opt-in private-to-group bridge) still searches messages only. Same SQL shape as conversation search: inner `ORDER BY embedding::halfvec(3072) <=> …` with `prepare_hnsw_search`, outer threshold (`semantic_similarity_threshold`) and re-sort; limit `semantic_search_limit`.

## Schema

`group_topics` (id, `user_id` → the group's `users.id` with `ON DELETE CASCADE`, `block_key`, subject, summary, `start_at`, `end_at`, `message_count`, `embedding vector(3072)`, `created_at`) with HNSW index `idx_group_topics_embedding_hnsw` on `embedding::halfvec(3072)`, and `group_topic_cursors` (`user_id` PK, `last_message_at`, `last_message_id`, `updated_at`). Both are new tables, so `create_all()` creates them and the index at API or worker start: **no migration file**. `/clean` (any level) deletes a chat's topics and cursor with its messages.

## Not done yet

- Save-only group messages are still embedded one by one in `routes/chat.py` `save_message_only`. With topics on, that could stop (fewer embedding calls, faster saves); left for after the pipeline and groups PRs that rewrite that file.
- A daily/weekly summary posted into the group: Devflow `6af21a47be4cc60a82acd731`, to go through the scheduler's paced send lane.
- A message deleted or edited after its block was summarised stays in the topic summary.
- Sender names are not pseudonymised before the model call (wa_llm does): they already reach the model in every group reply's history.
