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

const whitelistPhones = new Set(
  (process.env.WHITELIST_PHONES || '')
    .split(',')
    .map((jid) => jid.trim())
    .filter(Boolean)
);

export const config = {
  whitelistPhones,
  aiApiUrl: process.env.AI_API_URL || 'http://localhost:8000',
  logLevel: process.env.LOG_LEVEL || 'info',
  server: {
    port: parseInt(process.env.WHATSAPP_CLOUD_PORT || '3002', 10),
    host: process.env.WHATSAPP_CLOUD_HOST || '0.0.0.0',
  },
  // Security
  whatsappApiKey: process.env.WHATSAPP_CLOUD_API_KEY || process.env.WHATSAPP_API_KEY || '',
  aiApiKey: process.env.AI_API_KEY || '',
  corsOrigins: process.env.CORS_ORIGINS || '',
  rateLimitGlobal: parseInt(process.env.RATE_LIMIT_GLOBAL || '30', 10),
  rateLimitExpensive: parseInt(process.env.RATE_LIMIT_EXPENSIVE || '5', 10),
  // Timeouts
  timeouts: {
    default: parseInt(process.env.FETCH_TIMEOUT_DEFAULT_MS || '30000', 10),
    transcription: parseInt(process.env.FETCH_TIMEOUT_TRANSCRIPTION_MS || '60000', 10),
    tts: parseInt(process.env.FETCH_TIMEOUT_TTS_MS || '45000', 10),
    polling: parseInt(process.env.FETCH_TIMEOUT_POLLING_MS || '5000', 10),
  },
  // Polling
  polling: {
    intervalMs: parseInt(process.env.POLL_INTERVAL_MS || '500', 10),
    maxIterations: parseInt(process.env.POLL_MAX_ITERATIONS || '240', 10),
    maxDurationMs: parseInt(process.env.POLL_MAX_DURATION_MS || '120000', 10),
  },
  // Meta / WhatsApp Cloud API
  meta: {
    phoneNumberId: process.env.META_PHONE_NUMBER_ID || '',
    accessToken: process.env.META_ACCESS_TOKEN || '',
    appSecret: process.env.META_APP_SECRET || '',
    webhookVerifyToken: process.env.META_WEBHOOK_VERIFY_TOKEN || '',
    graphApiVersion: process.env.META_GRAPH_API_VERSION || 'v21.0',
    graphApiBaseUrl: 'https://graph.facebook.com',
  },
} as const;
