/**
 * Unit tests for validateRequiredEnv from main.ts.
 *
 * Critical security path: when TELEGRAM_PUBLIC_WEBHOOK_URL is set,
 * TELEGRAM_WEBHOOK_SECRET must be non-empty — otherwise grammY's
 * webhookCallback skips header verification and the /webhook route
 * (already exempt from API-key auth) accepts any forged update.
 */

import { describe, it, expect } from 'vitest';
import { validateRequiredEnv } from '../../src/config-validation.js';

type ConfigShape = Parameters<typeof validateRequiredEnv>[0];

function makeConfig(overrides: {
  telegramApiKey?: string;
  aiApiKey?: string;
  botToken?: string;
  publicWebhookUrl?: string;
  webhookSecret?: string;
} = {}): ConfigShape {
  return {
    telegramApiKey: overrides.telegramApiKey ?? 'k',
    aiApiKey: overrides.aiApiKey ?? 'k',
    telegram: {
      botToken: overrides.botToken ?? 't',
      publicWebhookUrl: overrides.publicWebhookUrl ?? '',
      webhookSecret: overrides.webhookSecret ?? '',
      dropPendingUpdates: true,
    },
    // The validator only inspects the fields above; cast for the rest.
  } as unknown as ConfigShape;
}

describe('validateRequiredEnv', () => {
  it('passes when all required env is set and webhook is local-only (no public URL)', () => {
    expect(() => validateRequiredEnv(makeConfig())).not.toThrow();
  });

  it('throws when TELEGRAM_API_KEY is missing', () => {
    expect(() => validateRequiredEnv(makeConfig({ telegramApiKey: '' }))).toThrow(
      /TELEGRAM_API_KEY/
    );
  });

  it('throws when AI_API_KEY is missing', () => {
    expect(() => validateRequiredEnv(makeConfig({ aiApiKey: '' }))).toThrow(/AI_API_KEY/);
  });

  it('throws when TELEGRAM_BOT_TOKEN is missing', () => {
    expect(() => validateRequiredEnv(makeConfig({ botToken: '' }))).toThrow(/TELEGRAM_BOT_TOKEN/);
  });

  it('throws when TELEGRAM_PUBLIC_WEBHOOK_URL is set but TELEGRAM_WEBHOOK_SECRET is empty', () => {
    expect(() =>
      validateRequiredEnv(
        makeConfig({ publicWebhookUrl: 'https://example.com/webhook', webhookSecret: '' })
      )
    ).toThrow(/TELEGRAM_WEBHOOK_SECRET/);
  });

  it('passes when both TELEGRAM_PUBLIC_WEBHOOK_URL and TELEGRAM_WEBHOOK_SECRET are set', () => {
    expect(() =>
      validateRequiredEnv(
        makeConfig({ publicWebhookUrl: 'https://example.com/webhook', webhookSecret: 'sek' })
      )
    ).not.toThrow();
  });

  it('passes when TELEGRAM_PUBLIC_WEBHOOK_URL is empty regardless of webhook secret (local dev)', () => {
    expect(() =>
      validateRequiredEnv(makeConfig({ publicWebhookUrl: '', webhookSecret: '' }))
    ).not.toThrow();
  });
});
