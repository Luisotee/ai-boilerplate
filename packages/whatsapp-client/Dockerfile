# Single stage build using tsx for TypeScript execution
FROM node:22-alpine

# Install pnpm
RUN corepack enable && corepack prepare pnpm@latest --activate

WORKDIR /app

# Copy workspace configuration and root lockfile
COPY pnpm-lock.yaml pnpm-workspace.yaml ./
COPY packages/whatsapp-client/package.json ./packages/whatsapp-client/

# Install all dependencies (need tsx for runtime)
RUN pnpm install --frozen-lockfile --filter whatsapp-client

# Copy source code
COPY packages/whatsapp-client/src ./packages/whatsapp-client/src
COPY packages/whatsapp-client/tsconfig.json ./packages/whatsapp-client/

# Create directory for WhatsApp session persistence
RUN mkdir -p /app/packages/whatsapp-client/auth_info_baileys

WORKDIR /app/packages/whatsapp-client

# Set environment variables
ENV NODE_ENV=production

# Expose port
EXPOSE 3001

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://localhost:3001/health || exit 1

# Start application using tsx (TypeScript executor)
CMD ["pnpm", "exec", "tsx", "src/main.ts"]
