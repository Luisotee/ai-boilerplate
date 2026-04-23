# Single stage build using tsx for TypeScript execution
FROM node:22-alpine

# Install pnpm
RUN corepack enable && corepack prepare pnpm@latest --activate

WORKDIR /app

# Copy workspace configuration and root lockfile
COPY pnpm-lock.yaml pnpm-workspace.yaml ./
COPY packages/telegram-client/package.json ./packages/telegram-client/

# Install all dependencies (need tsx for runtime)
RUN pnpm install --frozen-lockfile --filter telegram-client

# Copy source code
COPY packages/telegram-client/src ./packages/telegram-client/src
COPY packages/telegram-client/tsconfig.json ./packages/telegram-client/

WORKDIR /app/packages/telegram-client

ENV NODE_ENV=production

EXPOSE 3003

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://localhost:3003/health || exit 1

CMD ["pnpm", "exec", "tsx", "src/main.ts"]
