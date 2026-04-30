import type { FastifyInstance, FastifyReply } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { isBotReady } from '../services/bot-state.js';
import * as telegramApi from '../services/telegram-api.js';
import { jidToChatId, isTelegramJid } from '../utils/telegram-id.js';
import {
  SendTextSchema,
  SendReactionSchema,
  TypingIndicatorSchema,
  SendTextResponseSchema,
  SuccessResponseSchema,
  ErrorResponseSchema,
} from '../schemas/messaging.js';

/**
 * Sentinel for malformed `phoneNumber` input. Lets route handlers map input
 * errors to HTTP 400 instead of the generic 500 used for runtime failures.
 */
class InvalidChatIdError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'InvalidChatIdError';
  }
}

/**
 * The AI API's WhatsAppClient sends conversation identifiers under the
 * `phoneNumber` field. For Telegram we accept either "tg:<chat_id>" or a bare
 * numeric chat id and normalize to a number.
 */
function resolveChatId(phoneNumber: string): number {
  if (isTelegramJid(phoneNumber)) return jidToChatId(phoneNumber);
  const id = Number(phoneNumber);
  if (!Number.isFinite(id) || !Number.isInteger(id)) {
    throw new InvalidChatIdError(`Invalid chat identifier: ${phoneNumber}`);
  }
  return id;
}

function sendErrorResponse(
  reply: FastifyReply,
  err: unknown,
  fallbackMessage: string
): FastifyReply {
  if (err instanceof InvalidChatIdError) {
    return reply.code(400).send({ error: err.message });
  }
  const error = err as Error;
  return reply.code(500).send({ error: error.message || fallbackMessage });
}

export async function registerMessagingRoutes(app: FastifyInstance) {
  // POST /whatsapp/send-text
  // Path mirrors the existing WhatsApp clients so the Python WhatsAppClient
  // works unchanged. `phoneNumber` carries a "tg:<chat_id>" JID or chat id.
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/send-text',
    {
      schema: {
        tags: ['Messaging'],
        description: 'Send a text message to a Telegram chat',
        body: SendTextSchema,
        response: {
          200: SendTextResponseSchema,
          400: ErrorResponseSchema,
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isBotReady()) {
        return reply.code(503).send({ error: 'Telegram bot not ready' });
      }

      let chatId: number;
      try {
        chatId = resolveChatId(request.body.phoneNumber);
      } catch (err) {
        return sendErrorResponse(reply, err, 'Failed to send text');
      }

      let replyTo: number | undefined;
      if (request.body.quoted_message_id !== undefined) {
        const parsed = Number(request.body.quoted_message_id);
        if (!Number.isFinite(parsed) || !Number.isInteger(parsed)) {
          return reply.code(400).send({ error: 'Invalid quoted_message_id' });
        }
        replyTo = parsed;
      }

      try {
        const messageId = await telegramApi.sendText(chatId, request.body.text, replyTo);
        return { success: true, message_id: String(messageId) };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to send text');
        return sendErrorResponse(reply, err, 'Failed to send text');
      }
    }
  );

  // POST /whatsapp/send-reaction
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/send-reaction',
    {
      schema: {
        tags: ['Messaging'],
        description:
          "React to a Telegram message. ⏳/✅/❌ are substituted to 🤔/👍/👎 since they are not in Telegram's allowed reaction list.",
        body: SendReactionSchema,
        response: {
          200: SuccessResponseSchema,
          400: ErrorResponseSchema,
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isBotReady()) {
        return reply.code(503).send({ error: 'Telegram bot not ready' });
      }

      let chatId: number;
      try {
        chatId = resolveChatId(request.body.phoneNumber);
      } catch (err) {
        return sendErrorResponse(reply, err, 'Failed to send reaction');
      }

      const messageId = Number(request.body.message_id);
      if (!Number.isFinite(messageId)) {
        return reply.code(400).send({ error: 'Invalid message_id' });
      }

      try {
        await telegramApi.sendReaction(chatId, messageId, request.body.emoji);
        return { success: true };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to send reaction');
        return sendErrorResponse(reply, err, 'Failed to send reaction');
      }
    }
  );

  // POST /whatsapp/typing
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/typing',
    {
      schema: {
        tags: ['Messaging'],
        description:
          'Send a "typing" chat action. Lasts ~5s on Telegram; callers wanting sustained typing should re-fire.',
        body: TypingIndicatorSchema,
        response: {
          200: SuccessResponseSchema,
          400: ErrorResponseSchema,
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isBotReady()) {
        return reply.code(503).send({ error: 'Telegram bot not ready' });
      }
      const { state, phoneNumber } = request.body;
      if (state === 'paused') return { success: true }; // no-op

      let chatId: number;
      try {
        chatId = resolveChatId(phoneNumber);
      } catch (err) {
        return sendErrorResponse(reply, err, 'Failed to send chat action');
      }

      try {
        await telegramApi.sendChatAction(chatId, 'typing');
        return { success: true };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to send chat action');
        return sendErrorResponse(reply, err, 'Failed to send chat action');
      }
    }
  );
}
