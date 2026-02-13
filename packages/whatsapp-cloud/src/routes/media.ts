import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import type { MultipartFile } from '@fastify/multipart';
import { isCloudApiConnected } from '../services/cloud-state.js';
import * as graphApi from '../services/graph-api.js';
import { jidToPhone } from '../utils/jid.js';
import { validateMediaFile } from '../utils/file-validation.js';
import { validateContactInfo } from '../utils/vcard-builder.js';
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
          406: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          413: {
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
    async (request, reply) => {
      if (!isCloudApiConnected()) {
        return reply.code(503).send({ error: 'WhatsApp Cloud API not connected' });
      }

      try {
        const { file, phoneNumber, caption } = request.body as {
          file: MultipartFile;
          phoneNumber: { value: string };
          caption?: { value: string };
        };

        if (!phoneNumber?.value) {
          return reply.code(400).send({ error: 'phoneNumber is required' });
        }
        if (!file) {
          return reply.code(400).send({ error: 'file is required' });
        }

        const fileBuffer = await file.toBuffer();
        const mimetype = file.mimetype;

        const validation = validateMediaFile(mimetype, fileBuffer.length, 'image');
        if (!validation.valid) {
          return reply.code(400).send({ error: validation.error });
        }

        const phone = jidToPhone(phoneNumber.value);
        const messageId = await graphApi.sendImage(phone, fileBuffer, mimetype, caption?.value);

        return { success: true, message_id: messageId };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error: error.message }, 'Failed to send image');

        const { multipartErrors } = app;
        if (err instanceof multipartErrors.RequestFileTooLargeError) {
          return reply.code(413).send({ error: 'File exceeds maximum size' });
        }
        if (err instanceof multipartErrors.InvalidMultipartContentTypeError) {
          return reply.code(406).send({ error: 'Request is not multipart' });
        }

        return reply.code(500).send({ error: error.message || 'Failed to send image' });
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
          406: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          413: {
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
    async (request, reply) => {
      if (!isCloudApiConnected()) {
        return reply.code(503).send({ error: 'WhatsApp Cloud API not connected' });
      }

      try {
        const { file, phoneNumber, caption, fileName } = request.body as {
          file: MultipartFile;
          phoneNumber: { value: string };
          caption?: { value: string };
          fileName?: { value: string };
        };

        if (!phoneNumber?.value) {
          return reply.code(400).send({ error: 'phoneNumber is required' });
        }
        if (!file) {
          return reply.code(400).send({ error: 'file is required' });
        }

        const fileBuffer = await file.toBuffer();
        const mimetype = file.mimetype;

        const validation = validateMediaFile(mimetype, fileBuffer.length, 'document');
        if (!validation.valid) {
          return reply.code(400).send({ error: validation.error });
        }

        const phone = jidToPhone(phoneNumber.value);
        const filename = fileName?.value || file.filename;
        const messageId = await graphApi.sendDocument(
          phone,
          fileBuffer,
          mimetype,
          filename,
          caption?.value
        );

        return { success: true, message_id: messageId };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error: error.message }, 'Failed to send document');

        const { multipartErrors } = app;
        if (err instanceof multipartErrors.RequestFileTooLargeError) {
          return reply.code(413).send({ error: 'File exceeds maximum size' });
        }
        if (err instanceof multipartErrors.InvalidMultipartContentTypeError) {
          return reply.code(406).send({ error: 'Request is not multipart' });
        }

        return reply.code(500).send({ error: error.message || 'Failed to send document' });
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
          406: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          413: {
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
    async (request, reply) => {
      if (!isCloudApiConnected()) {
        return reply.code(503).send({ error: 'WhatsApp Cloud API not connected' });
      }

      try {
        const { file, phoneNumber } = request.body as {
          file: MultipartFile;
          phoneNumber: { value: string };
        };

        if (!phoneNumber?.value) {
          return reply.code(400).send({ error: 'phoneNumber is required' });
        }
        if (!file) {
          return reply.code(400).send({ error: 'file is required' });
        }

        const fileBuffer = await file.toBuffer();
        const mimetype = file.mimetype;

        const validation = validateMediaFile(mimetype, fileBuffer.length, 'audio');
        if (!validation.valid) {
          return reply.code(400).send({ error: validation.error });
        }

        const phone = jidToPhone(phoneNumber.value);
        const messageId = await graphApi.sendAudio(phone, fileBuffer, mimetype);

        return { success: true, message_id: messageId };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error: error.message }, 'Failed to send audio');

        const { multipartErrors } = app;
        if (err instanceof multipartErrors.RequestFileTooLargeError) {
          return reply.code(413).send({ error: 'File exceeds maximum size' });
        }
        if (err instanceof multipartErrors.InvalidMultipartContentTypeError) {
          return reply.code(406).send({ error: 'Request is not multipart' });
        }

        return reply.code(500).send({ error: error.message || 'Failed to send audio' });
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
          406: {
            type: 'object',
            properties: {
              error: { type: 'string' },
            },
          },
          413: {
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
    async (request, reply) => {
      if (!isCloudApiConnected()) {
        return reply.code(503).send({ error: 'WhatsApp Cloud API not connected' });
      }

      try {
        const { file, phoneNumber, caption } = request.body as {
          file: MultipartFile;
          phoneNumber: { value: string };
          caption?: { value: string };
        };

        if (!phoneNumber?.value) {
          return reply.code(400).send({ error: 'phoneNumber is required' });
        }
        if (!file) {
          return reply.code(400).send({ error: 'file is required' });
        }

        const fileBuffer = await file.toBuffer();
        const mimetype = file.mimetype;

        const validation = validateMediaFile(mimetype, fileBuffer.length, 'video');
        if (!validation.valid) {
          return reply.code(400).send({ error: validation.error });
        }

        const phone = jidToPhone(phoneNumber.value);
        const messageId = await graphApi.sendVideo(phone, fileBuffer, mimetype, caption?.value);

        return { success: true, message_id: messageId };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error: error.message }, 'Failed to send video');

        const { multipartErrors } = app;
        if (err instanceof multipartErrors.RequestFileTooLargeError) {
          return reply.code(413).send({ error: 'File exceeds maximum size' });
        }
        if (err instanceof multipartErrors.InvalidMultipartContentTypeError) {
          return reply.code(406).send({ error: 'Request is not multipart' });
        }

        return reply.code(500).send({ error: error.message || 'Failed to send video' });
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
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isCloudApiConnected()) {
        return reply.code(503).send({ error: 'WhatsApp Cloud API not connected' });
      }

      try {
        const { phoneNumber, latitude, longitude, name, address } = request.body;
        const phone = jidToPhone(phoneNumber);

        const messageId = await graphApi.sendLocation(phone, latitude, longitude, name, address);
        return { success: true, message_id: messageId };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to send location');
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
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isCloudApiConnected()) {
        return reply.code(503).send({ error: 'WhatsApp Cloud API not connected' });
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
          return reply.code(400).send({ error: contactValidation.error! });
        }

        const phone = jidToPhone(phoneNumber);
        const messageId = await graphApi.sendContact(
          phone,
          contactName,
          contactPhone,
          contactEmail,
          contactOrg
        );

        return { success: true, message_id: messageId };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to send contact');
        return reply.code(500).send({ error: 'Failed to send contact' });
      }
    }
  );
}
