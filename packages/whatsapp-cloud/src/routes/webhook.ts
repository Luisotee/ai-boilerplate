import type { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import { config } from '../config.js';
import { logger } from '../logger.js';
import { verifyWebhookSignature } from '../utils/webhook-signature.js';
import {
  extractMessages,
  getSenderPhone,
  getSenderName,
  getMessageText,
  getMediaId,
  getDocumentFilename,
} from '../utils/message.js';
import type { WebhookBody } from '../utils/message.js';
import { handleTextMessage } from '../handlers/text.js';
import { extractAndTranscribeAudio } from '../handlers/audio.js';
import { extractImageData } from '../handlers/image.js';
import { extractDocumentData } from '../handlers/document.js';

export async function registerWebhookRoutes(app: FastifyInstance) {
  // ==================== GET /webhook — Meta verification ====================
  app.get(
    '/webhook',
    {
      schema: {
        tags: ['Webhook'],
        description: 'Meta webhook verification endpoint',
        querystring: {
          type: 'object',
          properties: {
            'hub.mode': { type: 'string' },
            'hub.verify_token': { type: 'string' },
            'hub.challenge': { type: 'string' },
          },
        },
      },
      // Skip Zod validation for this route — plain JSON Schema for query params
      validatorCompiler: () => {
        return function (data) {
          return { value: data };
        };
      },
      serializerCompiler: () => {
        return function (data) {
          return typeof data === 'string' ? data : JSON.stringify(data);
        };
      },
    },
    async (
      request: FastifyRequest<{
        Querystring: { 'hub.mode'?: string; 'hub.verify_token'?: string; 'hub.challenge'?: string };
      }>,
      reply: FastifyReply
    ) => {
      const mode = request.query['hub.mode'];
      const token = request.query['hub.verify_token'];
      const challenge = request.query['hub.challenge'];

      logger.info(
        { mode, hasToken: !!token, hasChallenge: !!challenge },
        'Webhook verification request'
      );

      if (mode === 'subscribe' && token === config.meta.webhookVerifyToken) {
        logger.info('Webhook verification successful');
        return reply
          .code(200)
          .type('text/plain')
          .send(challenge || '');
      }

      logger.warn(
        { mode, tokenMatch: token === config.meta.webhookVerifyToken },
        'Webhook verification failed'
      );
      return reply.code(403).send({ error: 'Verification failed' });
    }
  );

  // ==================== POST /webhook — Receive messages ====================
  app.post(
    '/webhook',
    {
      schema: {
        tags: ['Webhook'],
        description: 'Receive incoming WhatsApp messages from Meta',
      },
      // Skip Zod validation — we handle the body manually
      validatorCompiler: () => {
        return function (data) {
          return { value: data };
        };
      },
      serializerCompiler: () => {
        return function (data) {
          return JSON.stringify(data);
        };
      },
    },
    async (request: FastifyRequest, reply: FastifyReply) => {
      // Verify webhook signature if app secret is configured
      if (config.meta.appSecret) {
        const signature = request.headers['x-hub-signature-256'] as string | undefined;

        if (!signature) {
          logger.warn('Missing X-Hub-Signature-256 header');
          return reply.code(401).send({ error: 'Missing signature' });
        }

        // Reconstruct raw body from parsed JSON for HMAC verification
        const rawBody = JSON.stringify(request.body);
        const isValid = verifyWebhookSignature(rawBody, signature, config.meta.appSecret);

        if (!isValid) {
          logger.warn('Invalid webhook signature');
          return reply.code(401).send({ error: 'Invalid signature' });
        }
      }

      // Return 200 immediately — process messages asynchronously
      const body = request.body as WebhookBody;

      // Fire and forget: process messages in the background
      processWebhookMessages(body).catch((error) => {
        logger.error({ error }, 'Error processing webhook messages');
      });

      return reply.code(200).send('EVENT_RECEIVED');
    }
  );
}

/**
 * Process incoming webhook messages asynchronously.
 * This runs after the 200 response has been sent to Meta.
 */
async function processWebhookMessages(body: WebhookBody): Promise<void> {
  const extracted = extractMessages(body);

  if (extracted.length === 0) {
    logger.debug('No messages in webhook payload (likely a status update)');
    return;
  }

  for (const { message, contact } of extracted) {
    const senderPhone = getSenderPhone(message);
    const senderName = getSenderName(contact);
    const messageId = message.id;

    logger.info(
      { type: message.type, from: senderPhone, messageId },
      'Processing incoming message'
    );

    try {
      switch (message.type) {
        case 'text': {
          const text = getMessageText(message);
          if (text) {
            await handleTextMessage(senderPhone, messageId, text, senderName);
          }
          break;
        }

        case 'image': {
          const mediaId = getMediaId(message);
          if (mediaId) {
            const imageData = await extractImageData(mediaId);
            const caption = message.image?.caption || '';
            const text = caption || 'Image received';
            await handleTextMessage(
              senderPhone,
              messageId,
              text,
              senderName,
              imageData ?? undefined
            );
          }
          break;
        }

        case 'audio': {
          const mediaId = getMediaId(message);
          if (mediaId) {
            const mimetype = message.audio?.mime_type || 'audio/ogg';
            const transcription = await extractAndTranscribeAudio(mediaId, mimetype);
            if (transcription) {
              await handleTextMessage(senderPhone, messageId, transcription, senderName);
            } else {
              logger.warn({ messageId, senderPhone }, 'Audio transcription failed, skipping');
            }
          }
          break;
        }

        case 'document': {
          const mediaId = getMediaId(message);
          if (mediaId) {
            const mimetype = message.document?.mime_type || 'application/pdf';
            const filename = getDocumentFilename(message) || `document_${Date.now()}.pdf`;
            const documentData = await extractDocumentData(mediaId, mimetype, filename);
            const caption = message.document?.caption || '';
            const text = caption || `Document: ${filename}`;

            if (documentData) {
              await handleTextMessage(
                senderPhone,
                messageId,
                text,
                senderName,
                undefined,
                documentData
              );
            } else {
              // Non-PDF or download failure — send user-facing error
              const { sendText } = await import('../services/graph-api.js');
              await sendText(
                senderPhone,
                'Sorry, I can only process PDF documents. Please send a PDF file.'
              );
            }
          }
          break;
        }

        case 'reaction': {
          logger.debug(
            { messageId, emoji: message.reaction?.emoji, senderPhone },
            'Received reaction, skipping'
          );
          break;
        }

        default: {
          logger.debug(
            { type: message.type, messageId, senderPhone },
            'Unsupported message type, skipping'
          );
          break;
        }
      }
    } catch (error) {
      logger.error(
        { error, type: message.type, messageId, senderPhone },
        'Error processing individual message'
      );
    }
  }
}
