import type { FastifyReply } from 'fastify';
import { GrammyError } from 'grammy';
import { isTelegramJid, jidToChatId } from './telegram-id.js';

/**
 * Sentinel for malformed `phoneNumber` input. Lets route handlers map input
 * errors to HTTP 400 instead of the generic 500 used for runtime failures.
 */
export class InvalidChatIdError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'InvalidChatIdError';
  }
}

/**
 * Normalize the AI API's `phoneNumber` field to a Telegram chat id.
 *
 * The Python `WhatsAppClient` is shared across all three clients and sends
 * conversation identifiers under `phoneNumber`. For Telegram that carries either
 * a `tg:<chat_id>` JID or a bare numeric chat id.
 */
export function resolveChatId(phoneNumber: string): number {
  if (isTelegramJid(phoneNumber)) return jidToChatId(phoneNumber);
  const id = Number(phoneNumber);
  if (!Number.isFinite(id) || !Number.isInteger(id)) {
    throw new InvalidChatIdError(`Invalid chat identifier: ${phoneNumber}`);
  }
  return id;
}

export function sendErrorResponse(
  reply: FastifyReply,
  err: unknown,
  fallbackMessage: string
): FastifyReply {
  if (err instanceof InvalidChatIdError) {
    return reply.code(400).send({ error: err.message });
  }
  // 403 from the Bot API means this chat can't be written to at all: the user
  // blocked the bot or deleted their account, or the bot was removed from the
  // group. Surfaced as a 403 (not the generic 500) so the AI API's broadcast
  // sender can mark the recipient undeliverable instead of retrying.
  if (err instanceof GrammyError && err.error_code === 403) {
    return reply.code(403).send({ error: err.description });
  }
  const error = err as Error;
  return reply.code(500).send({ error: error.message || fallbackMessage });
}
