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
    port: parseInt(process.env.TELEGRAM_PORT || '3003', 10),
    host: process.env.TELEGRAM_HOST || '0.0.0.0',
  },
  // Security
  telegramApiKey: process.env.TELEGRAM_API_KEY || process.env.WHATSAPP_API_KEY || '',
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
  // Telegram Bot API
  telegram: {
    botToken: process.env.TELEGRAM_BOT_TOKEN || '',
    webhookSecret: process.env.TELEGRAM_WEBHOOK_SECRET || '',
    publicWebhookUrl: process.env.TELEGRAM_PUBLIC_WEBHOOK_URL || '',
    // If empty we'll skip setWebhook on boot — useful for tests / local polling via ngrok etc.
    dropPendingUpdates: process.env.TELEGRAM_DROP_PENDING_UPDATES !== 'false',
  },
  messageSplit: {
    // Telegram per-chat limit is ~1 msg/sec, so enforce a higher base delay than Cloud.
    enabled: process.env.MESSAGE_SPLIT_ENABLED !== 'false',
    baseDelayMs: parseNonNegativeInt('MESSAGE_SPLIT_BASE_DELAY_MS', 1000),
    perCharMs: parseNonNegativeInt('MESSAGE_SPLIT_PER_CHAR_MS', 25),
    maxDelayMs: parseNonNegativeInt('MESSAGE_SPLIT_MAX_DELAY_MS', 3500),
    maxChunks: parseNonNegativeInt('MESSAGE_SPLIT_MAX_CHUNKS', 5),
  },
} as const;
