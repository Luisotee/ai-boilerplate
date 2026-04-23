import { config as dotenvConfig, parse as dotenvParse } from 'dotenv';
import { existsSync, readFileSync } from 'fs';
import { dirname, resolve } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const packageRoot = resolve(__dirname, '..');
const monorepoRoot = resolve(packageRoot, '../..');

const rootEnvPath = resolve(monorepoRoot, '.env');
const localEnvPath = resolve(packageRoot, '.env.local');

const rootVars: Record<string, string> = existsSync(rootEnvPath)
  ? dotenvParse(readFileSync(rootEnvPath))
  : {};

if (existsSync(rootEnvPath)) {
  dotenvConfig({ path: rootEnvPath });
}

if (existsSync(localEnvPath)) {
  const localVars = dotenvParse(readFileSync(localEnvPath));
  for (const [key, localValue] of Object.entries(localVars)) {
    const rootValue = rootVars[key];
    if (rootValue !== undefined && rootValue !== localValue) {
      console.warn(`[config] .env.local overrides root .env: ${key}`);
    }
  }
  dotenvConfig({ path: localEnvPath, override: true });
}

const whitelistPhones = new Set(
  (process.env.WHITELIST_PHONES || '')
    .split(',')
    .map((jid) => jid.trim())
    .filter(Boolean)
);

function parseNonNegativeInt(name: string, defaultValue: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return defaultValue;
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n) || n < 0) {
    console.warn(`[config] Invalid value for ${name}: "${raw}", using default ${defaultValue}`);
    return defaultValue;
  }
  return n;
}

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
  messageSplit: {
    enabled: process.env.MESSAGE_SPLIT_ENABLED !== 'false',
    baseDelayMs: parseNonNegativeInt('MESSAGE_SPLIT_BASE_DELAY_MS', 600),
    perCharMs: parseNonNegativeInt('MESSAGE_SPLIT_PER_CHAR_MS', 25),
    maxDelayMs: parseNonNegativeInt('MESSAGE_SPLIT_MAX_DELAY_MS', 3500),
    maxChunks: parseNonNegativeInt('MESSAGE_SPLIT_MAX_CHUNKS', 5),
  },
} as const;
