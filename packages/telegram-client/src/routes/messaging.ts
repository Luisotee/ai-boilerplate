import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { isBotReady } from '../services/bot-state.js';
import * as telegramApi from '../services/telegram-api.js';
import { resolveChatId, sendErrorResponse } from '../utils/chat-id.js';
import {
  SendTextSchema,
  SendReactionSchema,
  TypingIndicatorSchema,
  SendTextResponseSchema,
  SuccessResponseSchema,
  ErrorResponseSchema,
  GroupMemberSchema,
  GroupMemberResponseSchema,
} from '../schemas/messaging.js';

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

  // POST /whatsapp/group-member
  // Live membership check, used by the AI API right before relaying a message
  // into a group on a user's behalf (send_group_message). Shared-group
  // discovery on Telegram is derived from stored message authorship, which
  // never expires — without this a user removed from a group would keep relay
  // access to it indefinitely.
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/group-member',
    {
      schema: {
        tags: ['Messaging'],
        description: 'Check whether a user is currently a member of a group chat',
        body: GroupMemberSchema,
        response: {
          200: GroupMemberResponseSchema,
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
      let userId: number;
      try {
        chatId = resolveChatId(request.body.phoneNumber);
        userId = resolveChatId(request.body.userJid);
      } catch (err) {
        return sendErrorResponse(reply, err, 'Invalid chat or user identifier');
      }

      try {
        return { is_member: await telegramApi.isChatMember(chatId, userId) };
      } catch (err) {
        // Never translate a lookup failure into "not a member" here: surfacing
        // the error keeps the decision (and its fail-closed default) in one
        // place, on the Python side.
        request.log.error({ err, chatId }, 'Failed to check group membership');
        return sendErrorResponse(reply, err, 'Failed to check group membership');
      }
    }
  );
}
