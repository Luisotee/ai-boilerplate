import { config } from './config.js';
import Fastify from 'fastify';
import FastifySwagger from '@fastify/swagger';
import FastifySwaggerUI from '@fastify/swagger-ui';
import FastifyMultipart, { ajvFilePlugin } from '@fastify/multipart';
import FastifyCors from '@fastify/cors';
import {
  serializerCompiler,
  validatorCompiler,
  jsonSchemaTransform,
  type ZodTypeProvider,
} from 'fastify-type-provider-zod';
import { initializeWhatsApp } from './whatsapp.js';
import { registerHealthRoutes } from './routes/health.js';
import { registerMessagingRoutes } from './routes/messaging.js';
import { registerMediaRoutes } from './routes/media.js';
import { registerOperationsRoutes } from './routes/operations.js';

/**
 * Transform function that handles both Zod and plain JSON Schema.
 * Pattern based on fastify-zod-openapi's approach of passing through non-Zod schemas.
 *
 * @see https://www.npmjs.com/package/fastify-zod-openapi
 * "This library assumes that if a response schema provided is not a Zod Schema,
 *  it is a JSON Schema and will naively pass it straight through"
 */
function createMixedSchemaTransform() {
  const multipartRoutes = [
    '/whatsapp/send-image',
    '/whatsapp/send-document',
    '/whatsapp/send-audio',
    '/whatsapp/send-video',
  ];

  return function mixedTransform(transformObject) {
    const { schema, url } = transformObject;

    // Multipart routes use plain JSON Schema - pass through unchanged
    if (multipartRoutes.includes(url)) {
      return { schema, url };
    }

    // All other routes use Zod - apply Zod transformation
    // Pass through the full transform object, not just schema and url
    return jsonSchemaTransform(transformObject);
  };
}

async function start() {
  // Initialize Fastify with built-in Pino logger and ZodTypeProvider
  const app = Fastify({
    logger: {
      level: config.logLevel,
      transport: {
        target: 'pino-pretty',
        options: {
          translateTime: 'HH:MM:ss Z',
          ignore: 'pid,hostname',
        },
      },
    },
    ajv: {
      plugins: [ajvFilePlugin],
    },
  }).withTypeProvider<ZodTypeProvider>();

  // Set Zod validators and serializers
  app.setValidatorCompiler(validatorCompiler);
  app.setSerializerCompiler(serializerCompiler);

  // Register CORS
  await app.register(FastifyCors, {
    origin: true, // Allow all origins (configure as needed)
  });

  // Register multipart for file uploads
  await app.register(FastifyMultipart, {
    attachFieldsToBody: true,
    limits: {
      fileSize: 50 * 1024 * 1024, // 50MB max file size
    },
  });

  // Register Swagger for OpenAPI docs
  await app.register(FastifySwagger, {
    openapi: {
      openapi: '3.0.3',
      info: {
        title: 'WhatsApp REST API',
        description: 'REST API for WhatsApp messaging via Baileys',
        version: '1.0.0',
      },
      servers: [{ url: `http://localhost:${config.server.port}`, description: 'Development' }],
      tags: [
        { name: 'Health', description: 'Health check endpoints' },
        { name: 'Messaging', description: 'Text messaging, reactions, typing' },
        { name: 'Media', description: 'Images, videos, documents, audio' },
        { name: 'Operations', description: 'Edit, delete, forward messages' },
      ],
    },
    transform: createMixedSchemaTransform(),
  });

  // Register Swagger UI
  await app.register(FastifySwaggerUI, {
    routePrefix: '/docs',
    uiConfig: {
      docExpansion: 'list',
      deepLinking: false,
    },
  });

  // Initialize WhatsApp connection (Baileys)
  app.log.info('Initializing WhatsApp connection...');
  await initializeWhatsApp();

  // Register API routes
  await registerHealthRoutes(app);
  await registerMessagingRoutes(app);
  await registerMediaRoutes(app);
  await registerOperationsRoutes(app);

  // Start server
  await app.listen({ port: config.server.port, host: config.server.host });

  app.log.info('='.repeat(60));
  app.log.info(`WhatsApp REST API listening on http://${config.server.host}:${config.server.port}`);
  app.log.info(`API Docs: http://localhost:${config.server.port}/docs`);
  app.log.info(`OpenAPI JSON: http://localhost:${config.server.port}/docs/json`);
  app.log.info('='.repeat(60));
}

start().catch((error) => {
  console.error('Failed to start server:', error);
  process.exit(1);
});
