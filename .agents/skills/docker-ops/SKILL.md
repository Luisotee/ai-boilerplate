---
name: docker-ops
description: >-
  Manages Docker Compose infrastructure: start/stop services, view logs, debug
  connectivity, check health, and manage volumes. Use for any Docker, container,
  infrastructure, or deployment task.
---

# Docker Compose Infrastructure Operations (`docker-ops`)

Operational guide for the Docker Compose services in this monorepo.

## Service Architecture & Profiles

| Service | Container Name | Port | Profile | Image / Build Context |
|---------|----------------|------|---------|-----------------------|
| `postgres` | `<SERVICE_NAME>-postgres` | `127.0.0.1:5432` | (core) | `pgvector/pgvector:pg16` |
| `redis` | `<SERVICE_NAME>-redis` | `127.0.0.1:6379` | (core) | `redis:7-alpine` |
| `api` | `<SERVICE_NAME>-api` | `8000` | (core) | `packages/ai-api/Dockerfile` |
| `worker` | `<SERVICE_NAME>-worker` | — | (core) | Reuses `api` image |
| `whatsapp` | `<SERVICE_NAME>-whatsapp` | `3001` | (core) | `packages/whatsapp-client/Dockerfile` |
| `whatsapp-cloud` | `<SERVICE_NAME>-whatsapp-cloud` | `3002` | `cloud` | `packages/whatsapp-cloud/Dockerfile` |
| `telegram` | `<SERVICE_NAME>-telegram` | `3003` | `telegram` | `packages/telegram-client/Dockerfile` |
| `adminer` | `<SERVICE_NAME>-adminer` | `127.0.0.1:8080` | `dev` | `adminer:latest` |

> [!NOTE]
> Container names and network (`<SERVICE_NAME>-network`) use `SERVICE_NAME` from root `.env` (default: `aiagent`). Run `./setup.sh --check` to verify no port collisions with other bot instances on the host.

## Common Operations

### Starting Services
```bash
docker compose up -d                                    # Core stack: postgres, redis, api, worker, whatsapp
docker compose --profile dev up -d                      # Core + Adminer DB web UI
docker compose --profile cloud up -d                    # Core + WhatsApp Cloud API client
docker compose --profile telegram up -d                 # Core + Telegram client
docker compose --profile dev --profile cloud --profile telegram up -d  # All services
```

### Inspecting Logs
```bash
docker compose logs -f api              # AI API
docker compose logs -f worker           # Stream worker (chat + PDF consumers)
docker compose logs -f whatsapp         # WhatsApp Baileys client
docker compose logs -f whatsapp-cloud   # WhatsApp Cloud client
docker compose logs -f telegram         # Telegram client
docker compose logs --tail=100 api      # Recent 100 lines
```

### Health Checks
```bash
curl -s http://localhost:8000/health     # AI API
curl -s http://localhost:3001/health     # WhatsApp Baileys client
curl -s http://localhost:3002/health     # WhatsApp Cloud client
curl -s http://localhost:3003/health     # Telegram client
docker compose exec postgres pg_isready -U aiagent
docker compose exec redis redis-cli ping
```

### Database Access
```bash
# Direct psql shell
docker compose exec postgres psql -U aiagent -d aiagent

# Apply SQL migration file
docker compose exec -T postgres psql -U aiagent -d aiagent \
  < packages/ai-api/docs/migrations/<migration>.sql
```

### Rebuilding Containers
```bash
docker compose build api                 # Rebuilds shared API image (used by worker too)
docker compose build whatsapp            # Rebuilds Baileys client
docker compose build whatsapp-cloud      # Rebuilds Cloud client
docker compose build telegram            # Rebuilds Telegram client
docker compose up -d                     # Restart with updated images
```

## Persistent Volumes

| Volume | Purpose | Reset Command |
|--------|---------|---------------|
| `postgres-data` | Database tables & vectors | `docker volume rm <name>_postgres-data` (Caution: data loss) |
| `redis-data` | Redis job streams & queues | `docker volume rm <name>_redis-data` |
| `knowledge-base-data` | Uploaded raw PDF files | `docker volume rm <name>_knowledge-base-data` |
| `whatsapp-session` | Baileys authentication state | `docker volume rm <name>_whatsapp-session` (forces QR re-scan) |
