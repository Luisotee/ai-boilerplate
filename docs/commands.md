# Commands

The full command list. The root `CLAUDE.md` / `AGENTS.md` keeps the everyday subset.

```bash
# First-time setup
./setup.sh                              # Interactive: generates .env, installs Node + Python deps
./setup.sh --check                      # Report clashes with other bots on this host (read-only)
./setup.sh --fix                        # Move clashing ports in the existing .env (backup first)
./setup.sh --update                     # Add settings new in .env.example to the existing .env (backup first)

# Infrastructure (Docker Compose profiles)
docker compose up -d                                    # Core: postgres, redis, api, worker, whatsapp
docker compose --profile dev up -d                      # + Adminer (DB GUI, opt-in)
docker compose --profile cloud up -d                    # + WhatsApp Cloud API client (opt-in)
docker compose --profile telegram up -d                 # + Telegram client (opt-in)
docker compose --profile dev --profile cloud up -d      # Everything

# Development (from root)
pnpm dev:server                         # Start AI API (port 8000)
pnpm dev:whatsapp                       # Start Baileys WhatsApp client (port 3001)
pnpm dev:cloud                          # Start Cloud API WhatsApp client (port 3002)
pnpm dev:telegram                       # Start Telegram client (port 3003)
pnpm dev:queue                          # Start the stream worker (chat + PDF + broadcast consumers; required for replies, PDF processing AND broadcasts)
pnpm install:all                        # Install Node + Python dependencies

# Linting & Formatting
pnpm lint                               # Check TypeScript (ESLint) + Python (Ruff)
pnpm lint:fix                           # Auto-fix lint issues
pnpm format                             # Format TypeScript (Prettier) + Python (Ruff)
pnpm format:check                       # Verify formatting without changes (CI)
pnpm check:rules                        # Verify AGENTS.md rule size budget (<= 24 KB)

# Testing (from root)
pnpm test                               # Run all tests (TS + Python)
pnpm test:ts                            # Run TypeScript tests only (both clients)
pnpm test:python                        # Run Python tests only (uv run pytest)

# Per-package
cd packages/whatsapp-client && pnpm test        # Baileys client tests
cd packages/whatsapp-cloud && pnpm test         # Cloud client tests
cd packages/telegram-client && pnpm test        # Telegram client tests
cd packages/ai-api && uv run pytest             # AI API tests
cd packages/ai-api && uv run pytest tests/unit  # AI API unit tests only
cd packages/<pkg> && pnpm build                 # tsc type-check/build (the Docker build fails on type errors)

# Single test
cd packages/whatsapp-client && pnpm exec vitest run tests/unit/whitelist.test.ts   # one file
cd packages/whatsapp-client && pnpm exec vitest run -t "name substring"            # by test name
cd packages/ai-api && uv run pytest tests/mocked/test_model_chain_fallback.py -k "cooldown"
```

API docs: http://localhost:8000/docs (AI API), http://localhost:3001/docs (Baileys client), http://localhost:3002/docs (Cloud API client). DB GUI: http://localhost:8080 (Adminer).
