import type { config } from './config.js';

/**
 * Validate the env vars `start()` needs before bootstrapping Fastify and
 * grammY. Lives in its own file (no top-level side effects) so unit tests
 * can exercise each branch without triggering the start() call in main.ts.
 *
 * Critical security check: when TELEGRAM_PUBLIC_WEBHOOK_URL is set,
 * TELEGRAM_WEBHOOK_SECRET must be non-empty — otherwise grammY's
 * webhookCallback skips header verification and the /webhook route
 * (already exempt from API-key auth) accepts any forged Telegram update.
 */
export function validateRequiredEnv(cfg: typeof config): void {
  if (!cfg.telegramApiKey) {
    throw new Error(
      'TELEGRAM_API_KEY (or fallback WHATSAPP_API_KEY) environment variable is required'
    );
  }
  if (!cfg.aiApiKey) {
    throw new Error('AI_API_KEY environment variable is required');
  }
  if (!cfg.telegram.botToken) {
    throw new Error('TELEGRAM_BOT_TOKEN environment variable is required');
  }
  if (cfg.telegram.publicWebhookUrl && !cfg.telegram.webhookSecret) {
    throw new Error(
      'TELEGRAM_WEBHOOK_SECRET environment variable is required when ' +
        'TELEGRAM_PUBLIC_WEBHOOK_URL is set — without it, grammY skips ' +
        'webhook header verification and the route accepts forged updates.'
    );
  }
}
