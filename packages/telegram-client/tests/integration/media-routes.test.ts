/**
 * Integration tests for the media routes.
 *
 * These were 501 stubs, so the agent's send_whatsapp_location /
 * send_whatsapp_contact tools failed inside every Telegram conversation.
 *
 * The interesting behaviour is the location dispatch. Telegram splits what
 * WhatsApp treats as one call — `sendLocation` takes only coordinates, while a
 * titled pin requires `sendVenue`, which mandates BOTH title and address.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../../src/services/bot-state.js', () => ({
  isBotReady: vi.fn(() => true),
  markBotReady: vi.fn(),
}));

vi.mock('../../src/services/telegram-api.js', () => ({
  sendText: vi.fn(),
  sendReaction: vi.fn(),
  sendChatAction: vi.fn(),
  sendLocation: vi.fn(async () => 4242),
  sendContact: vi.fn(async () => 4243),
}));

import { buildTestApp } from '../helpers/fastify.js';
import * as botState from '../../src/services/bot-state.js';
import * as telegramApi from '../../src/services/telegram-api.js';

const mockIsBotReady = botState.isBotReady as ReturnType<typeof vi.fn>;
const mockSendLocation = telegramApi.sendLocation as ReturnType<typeof vi.fn>;
const mockSendContact = telegramApi.sendContact as ReturnType<typeof vi.fn>;

let app: Awaited<ReturnType<typeof buildTestApp>>;

beforeEach(async () => {
  vi.clearAllMocks();
  mockIsBotReady.mockReturnValue(true);
  mockSendLocation.mockResolvedValue(4242);
  mockSendContact.mockResolvedValue(4243);
  app = await buildTestApp();
});

afterEach(async () => {
  if (app) await app.close();
});

describe('POST /whatsapp/send-location', () => {
  it('sends a bare pin when no name or address is given', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/send-location',
      payload: { phoneNumber: 'tg:123', latitude: -15.794, longitude: -47.882 },
    });

    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({ success: true, message_id: '4242' });
    expect(mockSendLocation).toHaveBeenCalledWith(123, -15.794, -47.882, undefined, undefined);
  });

  it('passes name and address through for a venue card', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/send-location',
      payload: {
        phoneNumber: 'tg:123',
        latitude: -15.794,
        longitude: -47.882,
        name: 'Meeting point: Central Park',
        address: 'New York, NY',
      },
    });

    expect(res.statusCode).toBe(200);
    expect(mockSendLocation).toHaveBeenCalledWith(
      123,
      -15.794,
      -47.882,
      'Meeting point: Central Park',
      'New York, NY'
    );
  });

  it('accepts a bare numeric chat id', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/send-location',
      payload: { phoneNumber: '-1001234567890', latitude: 0, longitude: 0 },
    });

    expect(res.statusCode).toBe(200);
    expect(mockSendLocation).toHaveBeenCalledWith(-1001234567890, 0, 0, undefined, undefined);
  });

  it('returns 400 for a malformed chat identifier', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/send-location',
      payload: { phoneNumber: 'not-a-chat', latitude: 0, longitude: 0 },
    });

    expect(res.statusCode).toBe(400);
  });

  it('returns 400 for out-of-range coordinates', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/send-location',
      payload: { phoneNumber: 'tg:123', latitude: 999, longitude: 0 },
    });

    expect(res.statusCode).toBe(400);
    expect(mockSendLocation).not.toHaveBeenCalled();
  });

  it('returns 503 when the bot is not connected', async () => {
    mockIsBotReady.mockReturnValue(false);
    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/send-location',
      payload: { phoneNumber: 'tg:123', latitude: 0, longitude: 0 },
    });

    expect(res.statusCode).toBe(503);
  });

  it('returns 500 when the Bot API call fails', async () => {
    mockSendLocation.mockRejectedValue(new Error('chat not found'));
    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/send-location',
      payload: { phoneNumber: 'tg:123', latitude: 0, longitude: 0 },
    });

    expect(res.statusCode).toBe(500);
  });
});

describe('POST /whatsapp/send-contact', () => {
  it('forwards the contact details', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/send-contact',
      payload: {
        phoneNumber: 'tg:123',
        contactName: 'Ana Paula Silva',
        contactPhone: '+5511987654321',
        contactEmail: 'ana@example.com',
        contactOrg: 'Brigada Norte',
      },
    });

    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({ success: true, message_id: '4243' });
    expect(mockSendContact).toHaveBeenCalledWith(123, {
      name: 'Ana Paula Silva',
      phone: '+5511987654321',
      email: 'ana@example.com',
      org: 'Brigada Norte',
    });
  });

  it('works without the optional email and org', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/send-contact',
      payload: { phoneNumber: 'tg:123', contactName: 'Ana', contactPhone: '+5511987654321' },
    });

    expect(res.statusCode).toBe(200);
    expect(mockSendContact).toHaveBeenCalledWith(123, {
      name: 'Ana',
      phone: '+5511987654321',
      email: undefined,
      org: undefined,
    });
  });

  it('returns 400 for a malformed email', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/send-contact',
      payload: {
        phoneNumber: 'tg:123',
        contactName: 'Ana',
        contactPhone: '+5511987654321',
        contactEmail: 'not-an-email',
      },
    });

    expect(res.statusCode).toBe(400);
  });

  it('returns 503 when the bot is not connected', async () => {
    mockIsBotReady.mockReturnValue(false);
    const res = await app.inject({
      method: 'POST',
      url: '/whatsapp/send-contact',
      payload: { phoneNumber: 'tg:123', contactName: 'Ana', contactPhone: '+55119' },
    });

    expect(res.statusCode).toBe(503);
  });
});
