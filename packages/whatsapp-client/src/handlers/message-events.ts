import {
  normalizeMessageContent,
  WAMessageStubType,
  type WAMessageUpdate,
  type WASocket,
} from '@whiskeysockets/baileys';
import { logger } from '../logger.js';
import { config } from '../config.js';
import { editMessage, revokeMessage, type MessageEvent } from '../api-client.js';
import { resolveSenderGroupAdmin } from '../services/group-admin.js';
import {
  isGroupChat,
  resolveParticipantJid,
  resolveSenderPhone,
  stripDeviceSuffix,
} from '../utils/jid.js';
import { isWhitelisted, type Whitelist } from '../utils/whitelist.js';

/**
 * What a `messages.update` entry means for saved history, if anything.
 *
 * Baileys 7 turns the protocol messages into updates keyed by the TARGET
 * message's id, with the actor's (deleter's / editor's) participant:
 *   - REVOKE (delete for everyone): `update.message === null` and
 *     `update.messageStubType === REVOKE`;
 *   - MESSAGE_EDIT: `update.message.editedMessage.message` is the new content.
 * Every other update (receipts, reactions, polls, …) is not ours.
 */
export type MessageEventKind =
  | { kind: 'revoke' }
  | { kind: 'edit'; text: string }
  | { kind: 'edit-no-text' }
  | null;

export function classifyMessageUpdate({ key, update }: WAMessageUpdate): MessageEventKind {
  if (key.fromMe || !key.id || !key.remoteJid || key.remoteJid === 'status@broadcast') return null;
  if (update.message === null && update.messageStubType === WAMessageStubType.REVOKE) {
    return { kind: 'revoke' };
  }
  const edited = update.message?.editedMessage?.message;
  if (edited) {
    // Only text edits: a caption edit would have to rebuild the
    // `[Image: …]` / `[Document: …]` marker the row was saved with.
    const inner = normalizeMessageContent(edited);
    const text = inner?.conversation || inner?.extendedTextMessage?.text;
    return text ? { kind: 'edit', text } : { kind: 'edit-no-text' };
  }
  return null;
}

/**
 * Cheap client-side scope check before an edit/revoke costs an AI-API call.
 * Fails closed on an unavailable whitelist (`null`). A group in
 * GROUP_GATING=membership mode is passed through: only rows saved while it was
 * in scope exist, and the API acts on nothing else.
 */
export function eventInScope(
  wl: Whitelist | null,
  chatJid: string,
  isGroup: boolean,
  phone: string | undefined
): boolean {
  if (!wl) return false;
  if (wl.size === 0) return true;
  if (isGroup && config.groupGating === 'membership') return true;
  return isWhitelisted(wl, chatJid, phone);
}

/**
 * Apply one `messages.update` entry to saved history: a revoke deletes the
 * saved copy, a text edit replaces it (the AI API re-embeds it). Never throws
 * and never reacts or replies: these are silent history maintenance.
 */
export async function handleMessageUpdate(
  sock: WASocket,
  entry: WAMessageUpdate,
  getWhitelist: () => Promise<Whitelist | null>
): Promise<void> {
  const event = classifyMessageUpdate(entry);
  if (!event) return;
  const { key } = entry;
  const chatJid = stripDeviceSuffix(key.remoteJid!);
  try {
    if (event.kind === 'edit-no-text') {
      logger.debug({ chatJid, messageId: key.id }, 'Ignoring a non-text message edit');
      return;
    }

    const isGroup = isGroupChat(chatJid);
    const phone = isGroup
      ? undefined
      : await resolveSenderPhone(sock, key.remoteJid!, key.remoteJidAlt);
    if (!eventInScope(await getWhitelist(), chatJid, isGroup, phone)) {
      logger.debug({ chatJid, isGroup }, 'Skipping message event for an out-of-scope chat');
      return;
    }

    const payload: MessageEvent = {
      whatsappJid: chatJid,
      messageId: key.id!,
      senderJid: isGroup
        ? await resolveParticipantJid(sock, key.participant, key.participantAlt)
        : undefined,
      phone,
    };

    if (event.kind === 'revoke') {
      // An admin may delete someone else's message; the API only honours that
      // for an explicit true (cached metadata, bounded; failure → undefined).
      const isGroupAdmin = isGroup
        ? await resolveSenderGroupAdmin(sock, chatJid, key, '')
        : undefined;
      const changed = await revokeMessage(payload, isGroupAdmin);
      logger.info({ chatJid, messageId: key.id, changed }, 'Applied message revoke');
    } else {
      const changed = await editMessage(payload, event.text);
      logger.info({ chatJid, messageId: key.id, changed }, 'Applied message edit');
    }
  } catch (error) {
    logger.warn({ error, chatJid, messageId: key.id }, 'Failed to apply message update');
  }
}
