/**
 * Unit tests for the location and contact senders in services/telegram-api.ts.
 *
 * Telegram splits what WhatsApp treats as one call: `sendLocation` takes ONLY
 * coordinates — it has no name/address parameters — while a titled pin needs
 * `sendVenue`, which *requires* both title and address. The agent's location
 * tool may send name+address, so getting this dispatch wrong would either drop
 * the labels or 400 the whole send.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { bot } from '../../src/bot.js';
import { sendContact, sendLocation } from '../../src/services/telegram-api.js';

describe('sendLocation', () => {
  let locationSpy: ReturnType<typeof vi.spyOn>;
  let venueSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    locationSpy = vi.spyOn(bot.api, 'sendLocation').mockResolvedValue({ message_id: 11 } as never);
    venueSpy = vi.spyOn(bot.api, 'sendVenue').mockResolvedValue({ message_id: 22 } as never);
  });

  it('uses sendLocation for a bare pin', async () => {
    const id = await sendLocation(555, -15.794, -47.882);

    expect(id).toBe(11);
    expect(locationSpy).toHaveBeenCalledWith(555, -15.794, -47.882);
    expect(venueSpy).not.toHaveBeenCalled();
  });

  it('uses sendVenue when both name and address are present', async () => {
    const id = await sendLocation(
      555,
      -15.794,
      -47.882,
      'Meeting point: Central Park',
      'New York, NY'
    );

    expect(id).toBe(22);
    expect(venueSpy).toHaveBeenCalledWith(
      555,
      -15.794,
      -47.882,
      'Meeting point: Central Park',
      'New York, NY'
    );
    expect(locationSpy).not.toHaveBeenCalled();
  });

  it('falls back to a bare pin when only the name is present', async () => {
    // sendVenue requires BOTH title and address; calling it with one would 400.
    await sendLocation(555, -15.794, -47.882, 'Meeting point');

    expect(locationSpy).toHaveBeenCalled();
    expect(venueSpy).not.toHaveBeenCalled();
  });

  it('falls back to a bare pin when only the address is present', async () => {
    await sendLocation(555, -15.794, -47.882, undefined, 'New York, NY');

    expect(locationSpy).toHaveBeenCalled();
    expect(venueSpy).not.toHaveBeenCalled();
  });
});

describe('sendContact', () => {
  let contactSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    contactSpy = vi.spyOn(bot.api, 'sendContact').mockResolvedValue({ message_id: 33 } as never);
  });

  it('splits a full name into first and last', async () => {
    const id = await sendContact(555, { name: 'Ana Paula Silva', phone: '+5511987654321' });

    expect(id).toBe(33);
    const [chatId, phone, firstName, opts] = contactSpy.mock.calls[0];
    expect(chatId).toBe(555);
    expect(phone).toBe('+5511987654321');
    expect(firstName).toBe('Ana');
    expect(opts).toMatchObject({ last_name: 'Paula Silva' });
  });

  it('omits last_name for a single-word name', async () => {
    await sendContact(555, { name: 'Ana', phone: '+5511987654321' });

    const [, , firstName, opts] = contactSpy.mock.calls[0];
    expect(firstName).toBe('Ana');
    expect(opts).not.toHaveProperty('last_name');
  });

  it('omits the vcard when there is no email or org', async () => {
    // sendContact carries phone/first/last natively; a vCard is only needed for
    // the fields Telegram has no parameter for.
    await sendContact(555, { name: 'Ana', phone: '+5511987654321' });

    expect(contactSpy.mock.calls[0][3]).not.toHaveProperty('vcard');
  });

  it('builds a vcard carrying the email', async () => {
    await sendContact(555, {
      name: 'Ana',
      phone: '+5511987654321',
      email: 'ana@example.com',
    });

    const opts = contactSpy.mock.calls[0][3] as { vcard?: string };
    expect(opts.vcard).toContain('ana@example.com');
  });

  it('builds a vcard carrying the organization', async () => {
    await sendContact(555, {
      name: 'Ana',
      phone: '+5511987654321',
      org: 'Brigada Norte',
    });

    const opts = contactSpy.mock.calls[0][3] as { vcard?: string };
    expect(opts.vcard).toContain('Brigada Norte');
  });

  it('handles extra whitespace in the name', async () => {
    await sendContact(555, { name: '  Ana   Paula  ', phone: '+5511987654321' });

    const [, , firstName, opts] = contactSpy.mock.calls[0];
    expect(firstName).toBe('Ana');
    expect(opts).toMatchObject({ last_name: 'Paula' });
  });
});
