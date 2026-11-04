/**
 * Integration tests for the telegram-client status route.
 *
 * GET /whatsapp/status returns connection status and bot username.
 */

import { describe, it, expect, vi, beforeAll, beforeEach, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';

import { buildTestApp } from '../helpers/fastify.js';
import {
  markBotReady,
  markBotDisconnected,
  setBotUsernameForTests,
  _resetBotReadyForTests,
} from '../../src/services/bot-state.js';

describe('Status routes (telegram-client)', () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    app = await buildTestApp();
  });

  afterAll(async () => {
    await app.close();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    _resetBotReadyForTests();
  });

  describe('GET /whatsapp/status', () => {
    it('returns connected status and bot username when bot is ready', async () => {
      markBotReady();
      setBotUsernameForTests('curupira_bot');

      const res = await app.inject({ method: 'GET', url: '/whatsapp/status' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({
        status: 'connected',
        username: 'curupira_bot',
      });
    });

    it('returns disconnected and null username when bot is not ready', async () => {
      markBotDisconnected();
      setBotUsernameForTests(null);

      const res = await app.inject({ method: 'GET', url: '/whatsapp/status' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({
        status: 'disconnected',
        username: null,
      });
    });

    it('returns connected status but null username if botInfo has no username', async () => {
      markBotReady();
      setBotUsernameForTests(null);

      const res = await app.inject({ method: 'GET', url: '/whatsapp/status' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({
        status: 'connected',
        username: null,
      });
    });
  });
});
