import { describe, it, expect, afterEach } from 'vitest';
import { buildTestApp } from '../helpers/fastify.js';

let app: Awaited<ReturnType<typeof buildTestApp>>;

afterEach(async () => {
  if (app) await app.close();
});

describe('POST /whatsapp/send-location', () => {
  it('returns 501 — Telegram client does not support location messages', async () => {
    app = await buildTestApp();
    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/send-location',
      payload: { phoneNumber: 'tg:123', latitude: 0, longitude: 0 },
    });
    expect(res.statusCode).toBe(501);
    expect(res.json().error).toMatch(/not supported/i);
  });
});

describe('POST /whatsapp/send-contact', () => {
  it('returns 501 — Telegram client does not support contact cards', async () => {
    app = await buildTestApp();
    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/send-contact',
      payload: { phoneNumber: 'tg:123', contactName: 'X', contactPhone: 'Y' },
    });
    expect(res.statusCode).toBe(501);
  });
});
