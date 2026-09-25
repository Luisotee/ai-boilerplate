/**
 * Unit tests for handlers/link-prompt.ts (ported from curupira).
 *
 * The client deliberately makes NO authorization decisions — it forwards both
 * `contact.user_id` and `ctx.from.id` and lets the AI API refuse. These tests
 * pin that contract (both ids are always sent, unmodified) plus the two things
 * the client does own: the private-chat restriction that Telegram imposes on
 * `request_contact`, and removing the keyboard afterwards.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../src/api-client.js', () => ({
  linkPhone: vi.fn(),
}));

vi.mock('../../src/logger.js', () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));

import { offerPhoneLink, handleSharedContact } from '../../src/handlers/link-prompt.js';
import * as apiClient from '../../src/api-client.js';

const mockLinkPhone = apiClient.linkPhone as ReturnType<typeof vi.fn>;

interface FakeCtx {
  chat?: { id: number; type: string };
  from?: { id: number };
  msg?: { contact?: { phone_number: string; user_id?: number; first_name: string } };
  reply: ReturnType<typeof vi.fn>;
}

function makeCtx(overrides: Partial<FakeCtx> = {}): FakeCtx {
  return {
    chat: { id: 555, type: 'private' },
    from: { id: 555 },
    reply: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

describe('offerPhoneLink', () => {
  beforeEach(() => vi.clearAllMocks());

  it('sends a contact-request keyboard in a private chat', async () => {
    const ctx = makeCtx();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await offerPhoneLink(ctx as any);

    expect(ctx.reply).toHaveBeenCalledTimes(1);
    const [, opts] = ctx.reply.mock.calls[0];
    const keyboard = opts.reply_markup.build();
    expect(keyboard[0][0]).toMatchObject({ request_contact: true });
  });

  it('warns the user to DM instead when called in a group', async () => {
    // Telegram only allows request_contact in private chats, so the button
    // would simply never appear.
    const ctx = makeCtx({ chat: { id: -1001234567890, type: 'supergroup' } });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await offerPhoneLink(ctx as any);

    const [text, opts] = ctx.reply.mock.calls[0];
    expect(text).toMatch(/private chat/i);
    expect(opts).toBeUndefined();
  });

  it('tells the user the Telegram history will be discarded', async () => {
    // The merge is irreversible, so the offer has to say so up front.
    const ctx = makeCtx();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await offerPhoneLink(ctx as any);

    expect(ctx.reply.mock.calls[0][0]).toMatch(/discarded/i);
  });

  it('mentions the /link fallback for users who decline', async () => {
    const ctx = makeCtx();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await offerPhoneLink(ctx as any);

    expect(ctx.reply.mock.calls[0][0]).toMatch(/\/link/);
  });
});

describe('handleSharedContact', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockLinkPhone.mockResolvedValue('Linked successfully.');
  });

  it('forwards the phone and BOTH user ids to the API', async () => {
    const ctx = makeCtx({
      msg: { contact: { phone_number: '5511987654321', user_id: 555, first_name: 'Ana' } },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleSharedContact(ctx as any);

    expect(mockLinkPhone).toHaveBeenCalledWith('tg:555', {
      phone: '5511987654321',
      contactUserId: 555,
      senderUserId: 555,
    });
  });

  it('forwards a mismatched contact id unchanged, letting the API refuse', async () => {
    // The client must NOT pre-filter this: keeping one authority for the
    // anti-hijack rule means it cannot drift between clients.
    const ctx = makeCtx({
      msg: { contact: { phone_number: '5511000000000', user_id: 999, first_name: 'Bob' } },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleSharedContact(ctx as any);

    expect(mockLinkPhone).toHaveBeenCalledWith(
      'tg:555',
      expect.objectContaining({ contactUserId: 999, senderUserId: 555 })
    );
  });

  it('forwards a contact with no user_id rather than dropping it', async () => {
    const ctx = makeCtx({
      msg: { contact: { phone_number: '5511987654321', first_name: 'Ana' } },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleSharedContact(ctx as any);

    expect(mockLinkPhone).toHaveBeenCalledWith(
      'tg:555',
      expect.objectContaining({ contactUserId: undefined })
    );
  });

  it('shows the API message and removes the keyboard', async () => {
    const ctx = makeCtx({
      msg: { contact: { phone_number: '5511987654321', user_id: 555, first_name: 'Ana' } },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleSharedContact(ctx as any);

    expect(ctx.reply).toHaveBeenCalledWith('Linked successfully.', {
      reply_markup: { remove_keyboard: true },
    });
  });

  it('removes the keyboard even when the link is refused', async () => {
    mockLinkPhone.mockResolvedValue("That contact isn't you.");
    const ctx = makeCtx({
      msg: { contact: { phone_number: '5511000000000', user_id: 999, first_name: 'Bob' } },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleSharedContact(ctx as any);

    const [, opts] = ctx.reply.mock.calls[0];
    expect(opts.reply_markup).toEqual({ remove_keyboard: true });
  });

  it('falls back to a generic message when the API call itself fails', async () => {
    mockLinkPhone.mockResolvedValue(null);
    const ctx = makeCtx({
      msg: { contact: { phone_number: '5511987654321', user_id: 555, first_name: 'Ana' } },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleSharedContact(ctx as any);

    expect(ctx.reply.mock.calls[0][0]).toMatch(/try again/i);
  });

  it('ignores a contact shared in a group', async () => {
    const ctx = makeCtx({
      chat: { id: -1001234567890, type: 'supergroup' },
      msg: { contact: { phone_number: '5511987654321', user_id: 555, first_name: 'Ana' } },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleSharedContact(ctx as any);

    expect(mockLinkPhone).not.toHaveBeenCalled();
    expect(ctx.reply).not.toHaveBeenCalled();
  });

  it('does nothing when there is no contact on the message', async () => {
    const ctx = makeCtx({ msg: {} });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleSharedContact(ctx as any);

    expect(mockLinkPhone).not.toHaveBeenCalled();
  });
});
