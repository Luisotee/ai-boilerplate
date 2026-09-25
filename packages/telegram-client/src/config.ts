import { config as dotenvConfig, parse as dotenvParse } from 'dotenv';
import { existsSync, readFileSync } from 'fs';
import { dirname, resolve } from 'path';
import { fileURLToPath } from 'url';
import { parseWhitelist } from './utils/whitelist.js';
import { parseGroupGating } from './utils/gating.js';

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

const whitelistPhones = parseWhitelist(process.env.WHITELIST_PHONES || '');

/**
 * TELEGRAM_MODE: how updates arrive. `webhook` (default) — Telegram POSTs to
 * /webhook, which needs a public HTTPS URL. `polling` — long polling via
 * @grammyjs/runner, no public URL or tunnel needed. Anything else is kept as-is
 * so validateRequiredEnv can refuse to start with a clear message.
 */
const telegramMode = (process.env.TELEGRAM_MODE || 'webhook').trim().toLowerCase();

// GROUP_GATING=jid|membership — see utils/gating.ts. An unknown value falls back
// to the stricter `jid` rather than failing startup.
const groupGating = parseGroupGating(process.env.GROUP_GATING);
if (groupGating.invalid) {
  console.warn(`[config] Invalid GROUP_GATING "${process.env.GROUP_GATING}", using "jid"`);
}

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
  groupGating: groupGating.mode,
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
    mode: telegramMode,
    // --- polling mode only ---
    // getUpdates long-poll timeout (seconds).
    pollTimeoutSeconds: parseNonNegativeInt('TELEGRAM_POLL_TIMEOUT_SECONDS', 30),
    // How long the runner keeps retrying a failing getUpdates before giving up
    // (the process then exits and Docker restarts it). The runner's own default
    // is 15 HOURS of backoff — far too long to sit silently deaf.
    maxPollRetryMs: parseNonNegativeInt('TELEGRAM_POLL_MAX_RETRY_MS', 5 * 60_000),
    // Max updates processed concurrently (per-chat order is kept by sequentialize).
    concurrency: parseNonNegativeInt('TELEGRAM_POLL_CONCURRENCY', 50) || 50,
    // How long shutdown waits for in-flight updates. Just above the AI-API
    // polling ceiling, so a reply waiting on the model gets to finish.
    shutdownDrainMs: parseNonNegativeInt(
      'TELEGRAM_SHUTDOWN_DRAIN_MS',
      parseInt(process.env.POLL_MAX_DURATION_MS || '120000', 10) + 10_000
    ),
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
