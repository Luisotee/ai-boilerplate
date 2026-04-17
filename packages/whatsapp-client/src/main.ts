import './instrument.js';
import { Sentry } from './instrument.js';
import crypto from 'node:crypto';
import { config } from './config.js';
import { logger } from './logger.js';
import Fastify from 'fastify';
import FastifySwagger from '@fastify/swagger';
import FastifySwaggerUI from '@fastify/swagger-ui';
import FastifyMultipart, { ajvFilePlugin } from '@fastify/multipart';
import FastifyCors from '@fastify/cors';
import FastifyRateLimit from '@fastify/rate-limit';
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
import { registerMetricsRoutes } from './routes/metrics.js';

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
  // Validate required security config
  if (!config.whatsappApiKey) {
    throw new Error('WHATSAPP_API_KEY environment variable is required');
  }
  if (!config.aiApiKey) {
    throw new Error('AI_API_KEY environment variable is required');
  }

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

  // Register CORS — parse allowed origins from env, block all if empty
  const corsOrigins = config.corsOrigins
    ? config.corsOrigins
        .split(',')
        .map((o) => o.trim())
        .filter(Boolean)
    : [];
  await app.register(FastifyCors, {
    origin: corsOrigins.length > 0 ? corsOrigins : false,
  });

  // API Key authentication hook
  app.addHook('onRequest', async (request, reply) => {
    if (
      request.url === '/health' ||
      request.url === '/metrics' ||
      request.url.startsWith('/docs')
    ) {
      return;
    }
    const apiKey = request.headers['x-api-key'];
    const expected = config.whatsappApiKey;
    if (
      !apiKey ||
      typeof apiKey !== 'string' ||
      apiKey.length !== expected.length ||
      !crypto.timingSafeEqual(Buffer.from(apiKey), Buffer.from(expected))
    ) {
      app.log.warn({ url: request.url, ip: request.ip }, 'Unauthorized request');
      return reply.code(401).send({ error: 'Invalid or missing API key' });
    }
  });

  // Register rate limiting
  await app.register(FastifyRateLimit, {
    max: config.rateLimitGlobal,
    timeWindow: '1 minute',
    allowList: (req) => req.url === '/health' || req.url === '/metrics',
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
      components: {
        securitySchemes: {
          ApiKeyAuth: {
            type: 'apiKey',
            in: 'header',
            name: 'X-API-Key',
            description: 'API key for authentication',
          },
        },
      },
      security: [{ ApiKeyAuth: [] }],
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
  await registerMetricsRoutes(app);

  // Sentry Fastify error handler — must be registered after all routes
  if (process.env.SENTRY_DSN_NODE) {
    Sentry.setupFastifyErrorHandler(app);
  }

  // Start server
  await app.listen({ port: config.server.port, host: config.server.host });

  app.log.info('='.repeat(60));
  app.log.info(`WhatsApp REST API listening on http://${config.server.host}:${config.server.port}`);
  app.log.info(`API Docs: http://localhost:${config.server.port}/docs`);
  app.log.info(`OpenAPI JSON: http://localhost:${config.server.port}/docs/json`);
  if (config.whitelistPhones.size > 0) {
    app.log.info({ count: config.whitelistPhones.size }, 'User whitelist ENABLED');
  } else {
    app.log.info('User whitelist DISABLED (all users allowed)');
  }
  app.log.info('='.repeat(60));
}

async function shutdownWithError(err: unknown, message: string): Promise<never> {
  Sentry.captureException(err);
  logger.fatal({ err }, message);
  // Sentry transport is async; flush before exiting or the event is lost.
  await Sentry.close(2000);
  process.exit(1);
}

start().catch((error) => {
  void shutdownWithError(error, 'Failed to start server');
});

process.on('unhandledRejection', (reason) => {
  void shutdownWithError(reason, 'Unhandled rejection');
});
