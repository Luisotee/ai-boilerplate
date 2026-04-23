import { describe, it, expect, afterEach } from 'vitest';
import { buildTestApp } from '../helpers/fastify.js';
import * as botState from '../../src/services/bot-state.js';

let app: Awaited<ReturnType<typeof buildTestApp>>;

afterEach(async () => {
  if (app) await app.close();
});

describe('GET /health', () => {
  it('returns 200 with healthy when the bot is ready', async () => {
    app = await buildTestApp();
    botState.markBotReady();
    const res = await app.inject({ method: 'GET', url: '/health' });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toMatchObject({ status: 'healthy', telegram_connected: true });
  });
});

describe('GET /health/ready', () => {
  it('returns 200 with ready once the bot is ready', async () => {
    app = await buildTestApp();
    botState.markBotReady();
    const res = await app.inject({ method: 'GET', url: '/health/ready' });
    expect(res.statusCode).toBe(200);
    expect(res.json().status).toBe('ready');
  });
});
