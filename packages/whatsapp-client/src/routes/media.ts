import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import type { MultipartFile } from '@fastify/multipart';
import { getBaileysSocket, isBaileysReady } from '../services/baileys.js';
import { normalizeJid } from '../utils/jid.js';
import { validateMediaFile } from '../utils/file-validation.js';
import { buildVCard, validateContactInfo } from '../utils/vcard-builder.js';
import {
  SendLocationSchema,
  SendContactSchema,
  MediaResponseSchema,
  ErrorResponseSchema,
} from '../schemas/media.js';

export async function registerMediaRoutes(app: FastifyInstance) {
  // ==================== 1. POST /whatsapp/send-image ====================
  app.post(
    '/whatsapp/send-image',
    {
      schema: {
        tags: ['Media'],
        description: 'Send an image to WhatsApp user or group',
        consumes: ['multipart/form-data'],
        body: {
          type: 'object',
          required: ['file', 'phoneNumber'],
          properties: {
            file: { type: 'string', format: 'binary', description: 'Image file (JPEG, PNG, WebP)' },
            phoneNumber: { type: 'string', description: 'Recipient phone number' },
            caption: { type: 'string', description: 'Optional image caption' },
          },
        },
        response: {
          200: {
            type: 'object',
            properties: {
              success: { type: 'boolean' },
              message_id: { type: 'string' },
            },
          },
          400: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          404: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          500: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          503: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
        },
      },
      validatorCompiler: () => {
        // Skip body validation - we validate manually in handler
        return function (data) {
          return { value: data };
        };
      },
      serializerCompiler: () => {
        // Skip response serialization - return data as-is for plain JSON Schema
        return function (data) {
          return JSON.stringify(data);
        };
      },
    },
    async (request, reply) => {
      app.log.debug({ url: request.url }, 'Processing multipart request');

      if (!isBaileysReady()) {
        app.log.debug('Baileys not ready');
        return reply.code(503).send({ error: 'WhatsApp not connected' });
      }

      try {
        const { file, phoneNumber, caption } = request.body as {
          file: MultipartFile;
          phoneNumber: { value: string };
          caption?: { value: string };
        };

        app.log.debug(
          { phoneNumber: phoneNumber?.value, hasFile: !!file },
          'Extracted multipart fields'
        );

        if (!phoneNumber?.value) {
          app.log.debug('Missing phoneNumber');
          return reply.code(400).send({ error: 'phoneNumber is required' });
        }
        if (!file) {
          app.log.debug('Missing file');
          return reply.code(400).send({ error: 'file is required' });
        }

        app.log.debug({ mimetype: file.mimetype }, 'Converting file to buffer');
        const fileBuffer = await file.toBuffer();
        app.log.debug({ bufferSize: fileBuffer.length }, 'File converted to buffer');

        const mimetype = file.mimetype;

        const validation = validateMediaFile(mimetype, fileBuffer.length, 'image');
        if (!validation.valid) {
          app.log.debug({ validationError: validation.error }, 'File validation failed');
          return reply.code(400).send({ error: validation.error });
        }

        const normalizedJid = await normalizeJid(phoneNumber.value);
        const sock = getBaileysSocket();

        app.log.debug({ normalizedJid }, 'Sending message via Baileys');
        const result = await sock.sendMessage(normalizedJid, {
          image: fileBuffer,
          caption: caption?.value,
          mimetype: mimetype,
        });

        app.log.debug({ messageId: result?.key.id }, 'Message sent successfully');
        return { success: true, message_id: result?.key.id };
      } catch (err) {
        const error = err as Error & { code?: string };
        // Extract error details for proper logging
        const errorDetails = {
          message: error?.message || String(error) || 'Unknown error',
          code: error?.code,
          name: error?.constructor?.name,
          stack: error?.stack,
        };
        app.log.error(errorDetails, 'Failed to send image');

        // Handle specific multipart errors
        const { multipartErrors } = app;

        if (err instanceof multipartErrors.RequestFileTooLargeError) {
          return reply.code(413).send({
            error: 'File exceeds maximum size',
            code: 'FILE_TOO_LARGE',
          });
        }

        if (err instanceof multipartErrors.FilesLimitError) {
          return reply.code(413).send({
            error: 'Too many files uploaded',
            code: 'FILES_LIMIT',
          });
        }

        if (err instanceof multipartErrors.InvalidMultipartContentTypeError) {
          return reply.code(406).send({
            error: 'Request is not multipart',
            code: 'INVALID_CONTENT_TYPE',
          });
        }

        // Handle WhatsApp-specific errors
        if (error.message?.includes('not registered on WhatsApp')) {
          return reply.code(404).send({ error: error.message });
        }

        // Generic error with preserved message
        return reply.code(500).send({
          error: errorDetails.message,
        });
      }
    }
  );

  // ==================== 2. POST /whatsapp/send-document ====================
  app.post(
    '/whatsapp/send-document',
    {
      schema: {
        tags: ['Media'],
        description: 'Send a document to WhatsApp user or group',
        consumes: ['multipart/form-data'],
        body: {
          type: 'object',
          required: ['file', 'phoneNumber'],
          properties: {
            file: {
              type: 'string',
              format: 'binary',
              description: 'Document file (PDF, DOCX, etc.)',
            },
            phoneNumber: { type: 'string', description: 'Recipient phone number' },
            caption: { type: 'string', description: 'Optional document caption' },
            fileName: { type: 'string', description: 'Optional custom file name' },
          },
        },
        response: {
          200: {
            type: 'object',
            properties: {
              success: { type: 'boolean' },
              message_id: { type: 'string' },
            },
          },
          400: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          404: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          500: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          503: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
        },
      },
      validatorCompiler: () => {
        // Skip body validation - we validate manually in handler
        return function (data) {
          return { value: data };
        };
      },
      serializerCompiler: () => {
        // Skip response serialization - return data as-is for plain JSON Schema
        return function (data) {
          return JSON.stringify(data);
        };
      },
    },
    async (request, reply) => {
      app.log.debug({ url: request.url }, 'Processing multipart request');

      if (!isBaileysReady()) {
        app.log.debug('Baileys not ready');
        return reply.code(503).send({ error: 'WhatsApp not connected' });
      }

      try {
        const { file, phoneNumber, caption, fileName } = request.body as {
          file: MultipartFile;
          phoneNumber: { value: string };
          caption?: { value: string };
          fileName?: { value: string };
        };

        app.log.debug(
          { phoneNumber: phoneNumber?.value, hasFile: !!file, fileName: fileName?.value },
          'Extracted multipart fields'
        );

        if (!phoneNumber?.value) {
          app.log.debug('Missing phoneNumber');
          return reply.code(400).send({ error: 'phoneNumber is required' });
        }
        if (!file) {
          app.log.debug('Missing file');
          return reply.code(400).send({ error: 'file is required' });
        }

        app.log.debug({ mimetype: file.mimetype }, 'Converting file to buffer');
        const fileBuffer = await file.toBuffer();
        app.log.debug({ bufferSize: fileBuffer.length }, 'File converted to buffer');

        const mimetype = file.mimetype;

        const validation = validateMediaFile(mimetype, fileBuffer.length, 'document');
        if (!validation.valid) {
          app.log.debug({ validationError: validation.error }, 'File validation failed');
          return reply.code(400).send({ error: validation.error });
        }

        const normalizedJid = await normalizeJid(phoneNumber.value);
        const sock = getBaileysSocket();

        app.log.debug({ normalizedJid }, 'Sending message via Baileys');
        const result = await sock.sendMessage(normalizedJid, {
          document: fileBuffer,
          mimetype: mimetype,
          fileName: fileName?.value || file.filename,
          caption: caption?.value,
        });

        app.log.debug({ messageId: result?.key.id }, 'Message sent successfully');
        return { success: true, message_id: result?.key.id };
      } catch (err) {
        const error = err as Error & { code?: string };
        // Extract error details for proper logging
        const errorDetails = {
          message: error?.message || String(error) || 'Unknown error',
          code: error?.code,
          name: error?.constructor?.name,
          stack: error?.stack,
        };
        app.log.error(errorDetails, 'Failed to send document');

        // Handle specific multipart errors
        const { multipartErrors } = app;

        if (err instanceof multipartErrors.RequestFileTooLargeError) {
          return reply.code(413).send({
            error: 'File exceeds maximum size',
            code: 'FILE_TOO_LARGE',
          });
        }

        if (err instanceof multipartErrors.FilesLimitError) {
          return reply.code(413).send({
            error: 'Too many files uploaded',
            code: 'FILES_LIMIT',
          });
        }

        if (err instanceof multipartErrors.InvalidMultipartContentTypeError) {
          return reply.code(406).send({
            error: 'Request is not multipart',
            code: 'INVALID_CONTENT_TYPE',
          });
        }

        // Handle WhatsApp-specific errors
        if (error.message?.includes('not registered on WhatsApp')) {
          return reply.code(404).send({ error: error.message });
        }

        // Generic error with preserved message
        return reply.code(500).send({
          error: errorDetails.message,
        });
      }
    }
  );

  // ==================== 3. POST /whatsapp/send-audio ====================
  app.post(
    '/whatsapp/send-audio',
    {
      schema: {
        tags: ['Media'],
        description: 'Send audio or voice note to WhatsApp user or group',
        consumes: ['multipart/form-data'],
        body: {
          type: 'object',
          required: ['file', 'phoneNumber'],
          properties: {
            file: { type: 'string', format: 'binary', description: 'Audio file (MP3, OGG, M4A)' },
            phoneNumber: { type: 'string', description: 'Recipient phone number' },
            isVoiceNote: { type: 'boolean', description: 'Send as voice note (default: false)' },
          },
        },
        response: {
          200: {
            type: 'object',
            properties: {
              success: { type: 'boolean' },
              message_id: { type: 'string' },
            },
          },
          400: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          404: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          500: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          503: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
        },
      },
      validatorCompiler: () => {
        // Skip body validation - we validate manually in handler
        return function (data) {
          return { value: data };
        };
      },
      serializerCompiler: () => {
        // Skip response serialization - return data as-is for plain JSON Schema
        return function (data) {
          return JSON.stringify(data);
        };
      },
    },
    async (request, reply) => {
      app.log.debug({ url: request.url }, 'Processing multipart request');

      if (!isBaileysReady()) {
        app.log.debug('Baileys not ready');
        return reply.code(503).send({ error: 'WhatsApp not connected' });
      }

      try {
        const { file, phoneNumber, isVoiceNote } = request.body as {
          file: MultipartFile;
          phoneNumber: { value: string };
          isVoiceNote?: { value: string };
        };

        const ptt = isVoiceNote?.value === 'true';

        app.log.debug(
          { phoneNumber: phoneNumber?.value, hasFile: !!file, isVoiceNote: ptt },
          'Extracted multipart fields'
        );

        if (!phoneNumber?.value) {
          app.log.debug('Missing phoneNumber');
          return reply.code(400).send({ error: 'phoneNumber is required' });
        }
        if (!file) {
          app.log.debug('Missing file');
          return reply.code(400).send({ error: 'file is required' });
        }

        app.log.debug({ mimetype: file.mimetype }, 'Converting file to buffer');
        const fileBuffer = await file.toBuffer();
        app.log.debug({ bufferSize: fileBuffer.length }, 'File converted to buffer');

        const mimetype = file.mimetype;

        const validation = validateMediaFile(mimetype, fileBuffer.length, 'audio');
        if (!validation.valid) {
          app.log.debug({ validationError: validation.error }, 'File validation failed');
          return reply.code(400).send({ error: validation.error });
        }

        const normalizedJid = await normalizeJid(phoneNumber.value);
        const sock = getBaileysSocket();

        app.log.debug({ normalizedJid, ptt }, 'Sending message via Baileys');
        const result = await sock.sendMessage(normalizedJid, {
          audio: fileBuffer,
          mimetype: mimetype,
          ptt, // Push-to-talk (voice note)
        });

        app.log.debug({ messageId: result?.key.id }, 'Message sent successfully');
        return { success: true, message_id: result?.key.id };
      } catch (err) {
        const error = err as Error & { code?: string };
        // Extract error details for proper logging
        const errorDetails = {
          message: error?.message || String(error) || 'Unknown error',
          code: error?.code,
          name: error?.constructor?.name,
          stack: error?.stack,
        };
        app.log.error(errorDetails, 'Failed to send audio');

        // Handle specific multipart errors
        const { multipartErrors } = app;

        if (err instanceof multipartErrors.RequestFileTooLargeError) {
          return reply.code(413).send({
            error: 'File exceeds maximum size',
            code: 'FILE_TOO_LARGE',
          });
        }

        if (err instanceof multipartErrors.FilesLimitError) {
          return reply.code(413).send({
            error: 'Too many files uploaded',
            code: 'FILES_LIMIT',
          });
        }

        if (err instanceof multipartErrors.InvalidMultipartContentTypeError) {
          return reply.code(406).send({
            error: 'Request is not multipart',
            code: 'INVALID_CONTENT_TYPE',
          });
        }

        // Handle WhatsApp-specific errors
        if (error.message?.includes('not registered on WhatsApp')) {
          return reply.code(404).send({ error: error.message });
        }

        // Generic error with preserved message
        return reply.code(500).send({
          error: errorDetails.message,
        });
      }
    }
  );

  // ==================== 4. POST /whatsapp/send-video ====================
  app.post(
    '/whatsapp/send-video',
    {
      schema: {
        tags: ['Media'],
        description: 'Send video to WhatsApp user or group',
        consumes: ['multipart/form-data'],
        body: {
          type: 'object',
          required: ['file', 'phoneNumber'],
          properties: {
            file: { type: 'string', format: 'binary', description: 'Video file (MP4)' },
            phoneNumber: { type: 'string', description: 'Recipient phone number' },
            caption: { type: 'string', description: 'Optional video caption' },
            isGif: { type: 'boolean', description: 'Send as GIF (default: false)' },
          },
        },
        response: {
          200: {
            type: 'object',
            properties: {
              success: { type: 'boolean' },
              message_id: { type: 'string' },
            },
          },
          400: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          404: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          500: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          503: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
        },
      },
      validatorCompiler: () => {
        // Skip body validation - we validate manually in handler
        return function (data) {
          return { value: data };
        };
      },
      serializerCompiler: () => {
        // Skip response serialization - return data as-is for plain JSON Schema
        return function (data) {
          return JSON.stringify(data);
        };
      },
    },
    async (request, reply) => {
      app.log.debug({ url: request.url }, 'Processing multipart request');

      if (!isBaileysReady()) {
        app.log.debug('Baileys not ready');
        return reply.code(503).send({ error: 'WhatsApp not connected' });
      }

      try {
        const { file, phoneNumber, caption, isGif } = request.body as {
          file: MultipartFile;
          phoneNumber: { value: string };
          caption?: { value: string };
          isGif?: { value: string };
        };

        const gifPlayback = isGif?.value === 'true';

        app.log.debug(
          { phoneNumber: phoneNumber?.value, hasFile: !!file, isGif: gifPlayback },
          'Extracted multipart fields'
        );

        if (!phoneNumber?.value) {
          app.log.debug('Missing phoneNumber');
          return reply.code(400).send({ error: 'phoneNumber is required' });
        }
        if (!file) {
          app.log.debug('Missing file');
          return reply.code(400).send({ error: 'file is required' });
        }

        app.log.debug({ mimetype: file.mimetype }, 'Converting file to buffer');
        const fileBuffer = await file.toBuffer();
        app.log.debug({ bufferSize: fileBuffer.length }, 'File converted to buffer');

        const mimetype = file.mimetype;

        const validation = validateMediaFile(mimetype, fileBuffer.length, 'video');
        if (!validation.valid) {
          app.log.debug({ validationError: validation.error }, 'File validation failed');
          return reply.code(400).send({ error: validation.error });
        }

        const normalizedJid = await normalizeJid(phoneNumber.value);
        const sock = getBaileysSocket();

        app.log.debug({ normalizedJid, gifPlayback }, 'Sending message via Baileys');
        const result = await sock.sendMessage(normalizedJid, {
          video: fileBuffer,
          caption: caption?.value,
          mimetype: mimetype,
          gifPlayback,
        });

        app.log.debug({ messageId: result?.key.id }, 'Message sent successfully');
        return { success: true, message_id: result?.key.id };
      } catch (err) {
        const error = err as Error & { code?: string };
        // Extract error details for proper logging
        const errorDetails = {
          message: error?.message || String(error) || 'Unknown error',
          code: error?.code,
          name: error?.constructor?.name,
          stack: error?.stack,
        };
        app.log.error(errorDetails, 'Failed to send video');

        // Handle specific multipart errors
        const { multipartErrors } = app;

        if (err instanceof multipartErrors.RequestFileTooLargeError) {
          return reply.code(413).send({
            error: 'File exceeds maximum size',
            code: 'FILE_TOO_LARGE',
          });
        }

        if (err instanceof multipartErrors.FilesLimitError) {
          return reply.code(413).send({
            error: 'Too many files uploaded',
            code: 'FILES_LIMIT',
          });
        }

        if (err instanceof multipartErrors.InvalidMultipartContentTypeError) {
          return reply.code(406).send({
            error: 'Request is not multipart',
            code: 'INVALID_CONTENT_TYPE',
          });
        }

        // Handle WhatsApp-specific errors
        if (error.message?.includes('not registered on WhatsApp')) {
          return reply.code(404).send({ error: error.message });
        }

        // Generic error with preserved message
        return reply.code(500).send({
          error: errorDetails.message,
        });
      }
    }
  );

  // ==================== 5. POST /whatsapp/send-location ====================
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/send-location',
    {
      schema: {
        tags: ['Media'],
        description: 'Send location to WhatsApp user or group',
        body: SendLocationSchema,
        response: {
          200: MediaResponseSchema,
          400: ErrorResponseSchema,
          404: ErrorResponseSchema,
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isBaileysReady()) {
        return reply.code(503).send({ error: 'WhatsApp not connected' });
      }

      try {
        const { phoneNumber, latitude, longitude, name, address } = request.body;
        const normalizedJid = await normalizeJid(phoneNumber);
        const sock = getBaileysSocket();

        const result = await sock.sendMessage(normalizedJid, {
          location: {
            degreesLatitude: latitude,
            degreesLongitude: longitude,
            name: name,
            address: address,
          },
        });

        return { success: true, message_id: result?.key.id };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to send location');
        if (error.message?.includes('not registered on WhatsApp')) {
          return reply.code(404).send({ error: error.message });
        }
        return reply.code(500).send({ error: 'Failed to send location' });
      }
    }
  );

  // ==================== 6. POST /whatsapp/send-contact ====================
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/send-contact',
    {
      schema: {
        tags: ['Media'],
        description: 'Send contact card to WhatsApp user or group',
        body: SendContactSchema,
        response: {
          200: MediaResponseSchema,
          400: ErrorResponseSchema,
          404: ErrorResponseSchema,
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isBaileysReady()) {
        return reply.code(503).send({ error: 'WhatsApp not connected' });
      }

      try {
        const { phoneNumber, contactName, contactPhone, contactEmail, contactOrg } = request.body;

        // Validate contact info
        const contactValidation = validateContactInfo({
          name: contactName,
          phone: contactPhone,
          email: contactEmail,
          organization: contactOrg,
        });

        if (!contactValidation.valid) {
          return reply.code(400).send({ error: contactValidation.error });
        }

        // Build vCard
        const vcard = buildVCard({
          name: contactName,
          phone: contactPhone,
          email: contactEmail,
          organization: contactOrg,
        });

        const normalizedJid = await normalizeJid(phoneNumber);
        const sock = getBaileysSocket();

        const result = await sock.sendMessage(normalizedJid, {
          contacts: {
            displayName: contactName,
            contacts: [{ vcard }],
          },
        });

        return { success: true, message_id: result?.key.id };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to send contact');
        if (error.message?.includes('not registered on WhatsApp')) {
          return reply.code(404).send({ error: error.message });
        }
        return reply.code(500).send({ error: 'Failed to send contact' });
      }
    }
  );
}
