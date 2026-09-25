/**
 * Integration tests for the shared-groups route (POST /whatsapp/shared-groups).
 *
 * Tests the HTTP route via Fastify's inject() with mocked Baileys readiness
 * and a mocked findSharedGroups service.
 */

import { describe, it, expect, vi, beforeAll, beforeEach, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';

const FAKE_SOCK = { fake: 'socket' };

vi.mock('../../src/services/baileys.js', () => ({
  getBaileysSocket: vi.fn(() => FAKE_SOCK),
  isBaileysReady: vi.fn(),
  setBaileysSocket: vi.fn(),
  getConnectionInfo: vi.fn(),
}));

vi.mock('../../src/services/groups.js', () => ({
  findSharedGroups: vi.fn(),
}));

import { buildTestApp } from '../helpers/fastify.js';
import { isBaileysReady } from '../../src/services/baileys.js';
import { findSharedGroups } from '../../src/services/groups.js';

const mockIsBaileysReady = isBaileysReady as ReturnType<typeof vi.fn>;
const mockFindSharedGroups = findSharedGroups as ReturnType<typeof vi.fn>;

describe('Groups routes — POST /whatsapp/shared-groups', () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    app = await buildTestApp();
  });

  afterAll(async () => {
    await app.close();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns 503 when Baileys is not ready', async () => {
    mockIsBaileysReady.mockReturnValue(false);

    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/shared-groups',
      payload: { jid: '5511999999999@s.whatsapp.net' },
    });

    expect(res.statusCode).toBe(503);
    expect(res.json()).toEqual({ error: 'WhatsApp not connected' });
    expect(mockFindSharedGroups).not.toHaveBeenCalled();
  });

  it('returns 400 when no identifier is provided', async () => {
    mockIsBaileysReady.mockReturnValue(true);

    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/shared-groups',
      payload: {},
    });

    expect(res.statusCode).toBe(400);
  });

  it('returns 200 with the shared groups', async () => {
    mockIsBaileysReady.mockReturnValue(true);
    mockFindSharedGroups.mockResolvedValue([
      { groupJid: '120363000000001@g.us', subject: 'Book Club' },
    ]);

    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/shared-groups',
      payload: { jid: '5511999999999@s.whatsapp.net', phone: '+5511999999999' },
    });

    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({
      groups: [{ groupJid: '120363000000001@g.us', subject: 'Book Club' }],
    });
    expect(mockFindSharedGroups).toHaveBeenCalledWith(FAKE_SOCK, {
      jid: '5511999999999@s.whatsapp.net',
      phone: '+5511999999999',
    });
  });

  it('returns 500 when findSharedGroups throws', async () => {
    mockIsBaileysReady.mockReturnValue(true);
    mockFindSharedGroups.mockRejectedValueOnce(new Error('boom'));

    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/shared-groups',
      payload: { jid: '5511999999999@s.whatsapp.net' },
    });

    expect(res.statusCode).toBe(500);
    expect(res.json()).toEqual({ error: 'Failed to list shared groups' });
  });
});
