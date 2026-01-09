import { config as dotenvConfig } from 'dotenv';
import { existsSync } from 'fs';
import { dirname, resolve } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const packageRoot = resolve(__dirname, '..');
const monorepoRoot = resolve(packageRoot, '../..');

// Load root .env first (shared vars)
const rootEnvPath = resolve(monorepoRoot, '.env');
if (existsSync(rootEnvPath)) {
  dotenvConfig({ path: rootEnvPath });
}

// Load local .env.local for overrides
const localEnvPath = resolve(packageRoot, '.env.local');
if (existsSync(localEnvPath)) {
  dotenvConfig({ path: localEnvPath, override: true });
}

export const config = {
  aiApiUrl: process.env.AI_API_URL || 'http://localhost:8000',
  logLevel: process.env.LOG_LEVEL || 'info',
  server: {
    port: parseInt(process.env.WHATSAPP_API_PORT || '3001', 10),
    host: process.env.WHATSAPP_API_HOST || '0.0.0.0',
  },
} as const;
