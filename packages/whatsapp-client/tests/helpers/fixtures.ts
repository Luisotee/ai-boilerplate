/**
 * WAMessage stub factory and mock Baileys socket builder for whatsapp-client tests.
 *
 * These helpers produce minimal but structurally correct objects that satisfy
 * the shapes expected by handlers (text.ts, audio.ts, etc.) and the main
 * message router (whatsapp.ts).
 */

import { vi } from 'vitest';

// ---------------------------------------------------------------------------
// WAMessage factory
// ---------------------------------------------------------------------------

const DEFAULT_JID = '5511999999999@s.whatsapp.net';

/**
 * Build a minimal WAMessage-like object with an extendedTextMessage.
 *
 * @param text  - The message text body.
 * @param jid   - Remote JID (defaults to a private chat JID).
 * @returns A WAMessage-compatible plain object.
 */
export function makeTextMsg(text: string, jid?: string) {
  const remoteJid = jid ?? DEFAULT_JID;
  return {
    key: {
      remoteJid,
      fromMe: false,
      id: `MSG_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      participant: undefined as string | undefined,
    },
    message: {
      extendedTextMessage: {
        text,
        contextInfo: undefined as Record<string, unknown> | undefined,
      },
    },
    pushName: 'Test User',
    verifiedBizName: undefined as string | undefined,
    messageTimestamp: Math.floor(Date.now() / 1000),
  };
}

/**
 * Build a minimal WAMessage-like object for an image message.
 *
 * @param caption - Optional image caption.
 * @param jid     - Remote JID (defaults to a private chat JID).
 */
export function makeImageMsg(caption?: string, jid?: string) {
  const remoteJid = jid ?? DEFAULT_JID;
  return {
    key: {
      remoteJid,
      fromMe: false,
      id: `MSG_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      participant: undefined as string | undefined,
    },
    message: {
      imageMessage: {
        caption: caption ?? undefined,
        mimetype: 'image/jpeg',
        contextInfo: undefined as Record<string, unknown> | undefined,
      },
    },
    pushName: 'Test User',
    verifiedBizName: undefined as string | undefined,
    messageTimestamp: Math.floor(Date.now() / 1000),
  };
}

/**
 * Build a minimal WAMessage-like object for an audio message.
 *
 * @param jid - Remote JID (defaults to a private chat JID).
 */
export function makeAudioMsg(jid?: string) {
  const remoteJid = jid ?? DEFAULT_JID;
  return {
    key: {
      remoteJid,
      fromMe: false,
      id: `MSG_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      participant: undefined as string | undefined,
    },
    message: {
      audioMessage: {
        mimetype: 'audio/ogg; codecs=opus',
        ptt: true,
        contextInfo: undefined as Record<string, unknown> | undefined,
      },
    },
    pushName: 'Test User',
    verifiedBizName: undefined as string | undefined,
    messageTimestamp: Math.floor(Date.now() / 1000),
  };
}

/**
 * Build a minimal WAMessage for a group chat with an optional participant.
 *
 * @param text        - The message text body.
 * @param groupJid    - Group JID (defaults to a sample group JID).
 * @param participant - Participant JID who sent the message in the group.
 */
export function makeGroupTextMsg(text: string, groupJid?: string, participant?: string) {
  const msg = makeTextMsg(text, groupJid ?? '120363012345678@g.us');
  msg.key.participant = participant ?? '5511888888888@s.whatsapp.net';
  return msg;
}

// ---------------------------------------------------------------------------
// Mock Baileys socket
// ---------------------------------------------------------------------------

/**
 * Build a mock Baileys WASocket with vi.fn() stubs for every method
 * referenced in whatsapp.ts, handlers, and route modules.
 *
 * Methods included:
 *  - sendMessage
 *  - sendPresenceUpdate
 *  - presenceSubscribe
 *  - readMessages
 *  - groupMetadata
 *
 * The `user` property is pre-populated with a realistic bot identity
 * (both JID and LID).
 *
 * The `ev` property exposes a minimal event-emitter interface with `on`.
 */
export function makeMockSocket() {
  return {
    // Bot identity
    user: {
      id: '5511000000000:42@s.whatsapp.net',
      lid: '123456789:42@lid',
      name: 'Test Bot',
    },

    // Core messaging
    sendMessage: vi.fn().mockResolvedValue({
      key: {
        remoteJid: DEFAULT_JID,
        fromMe: true,
        id: 'SENT_MSG_ID',
      },
    }),

    // Presence / typing indicators
    sendPresenceUpdate: vi.fn().mockResolvedValue(undefined),
    presenceSubscribe: vi.fn().mockResolvedValue(undefined),

    // Read receipts
    readMessages: vi.fn().mockResolvedValue(undefined),

    // Group metadata (for admin checks)
    groupMetadata: vi.fn().mockResolvedValue({
      id: '120363012345678@g.us',
      subject: 'Test Group',
      participants: [
        { id: '5511000000000@s.whatsapp.net', admin: null },
        { id: '5511888888888@s.whatsapp.net', admin: 'admin' },
      ],
    }),

    // Event emitter (for connection.update, messages.upsert, creds.update)
    ev: {
      on: vi.fn(),
    },
  };
}
