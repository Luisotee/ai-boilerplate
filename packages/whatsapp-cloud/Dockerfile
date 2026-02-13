# Single stage build using tsx for TypeScript execution
FROM node:22-alpine

# Install pnpm
RUN corepack enable && corepack prepare pnpm@latest --activate

WORKDIR /app

# Copy workspace configuration and root lockfile
COPY pnpm-lock.yaml pnpm-workspace.yaml ./
COPY packages/whatsapp-cloud/package.json ./packages/whatsapp-cloud/

# Install all dependencies (need tsx for runtime)
RUN pnpm install --frozen-lockfile --filter whatsapp-cloud

# Copy source code
COPY packages/whatsapp-cloud/src ./packages/whatsapp-cloud/src
COPY packages/whatsapp-cloud/tsconfig.json ./packages/whatsapp-cloud/

WORKDIR /app/packages/whatsapp-cloud

# Set environment variables
ENV NODE_ENV=production

# Expose port
EXPOSE 3002

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://localhost:3002/health || exit 1

# Start application using tsx (TypeScript executor)
CMD ["pnpm", "exec", "tsx", "src/main.ts"]
