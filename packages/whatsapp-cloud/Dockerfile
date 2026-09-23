# WhatsApp Cloud API client — multi-stage build: tsc -> dist/, production deps only at runtime.
# Build context is the repo root (pnpm workspace + lockfile); see docker-compose.yml.

# ---------- Stage 1: compile TypeScript ----------
FROM node:22-alpine AS builder

RUN corepack enable && corepack prepare pnpm@10.14.0 --activate

WORKDIR /app

COPY pnpm-lock.yaml pnpm-workspace.yaml ./
COPY packages/whatsapp-cloud/package.json ./packages/whatsapp-cloud/

# All dependencies (typescript is a devDependency)
RUN pnpm install --frozen-lockfile --filter whatsapp-cloud

COPY packages/whatsapp-cloud/tsconfig.json ./packages/whatsapp-cloud/
COPY packages/whatsapp-cloud/src ./packages/whatsapp-cloud/src

# Type errors FAIL the build (tsc exits non-zero); never append `|| true`.
RUN pnpm --filter whatsapp-cloud build \
    && node --check /app/packages/whatsapp-cloud/dist/main.js \
    && node --check /app/packages/whatsapp-cloud/dist/instrument.js

# ---------- Stage 2: runtime ----------
FROM node:22-alpine

RUN corepack enable && corepack prepare pnpm@10.14.0 --activate

WORKDIR /app

COPY pnpm-lock.yaml pnpm-workspace.yaml ./
COPY packages/whatsapp-cloud/package.json ./packages/whatsapp-cloud/

# Production dependencies only
RUN pnpm install --frozen-lockfile --filter whatsapp-cloud --prod \
    && pnpm store prune

COPY --from=builder /app/packages/whatsapp-cloud/dist ./packages/whatsapp-cloud/dist

WORKDIR /app/packages/whatsapp-cloud

ENV NODE_ENV=production

# Run as the unprivileged user shipped with the node image (uid 1000)
USER node

EXPOSE 3002

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://localhost:3002/health || exit 1

# --import loads instrument.js (Sentry) BEFORE main.js's module graph is linked:
# in ESM, static imports are hoisted, so importing it first inside main is too
# late for Sentry's auto-instrumentation hooks. main.js's own
# `import './instrument.js'` resolves to the same URL and is not re-evaluated.
CMD ["node", "--import", "./dist/instrument.js", "dist/main.js"]
