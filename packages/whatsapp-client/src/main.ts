import './instrument.js';
import { Sentry } from './instrument.js';
import { config } from './config.js';
import { hasValidApiKey } from './utils/api-key.js';
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
import { registerConnectionRoutes } from './routes/connection.js';
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

  return function mixedTransform(transformObject: Parameters<typeof jsonSchemaTransform>[0]) {
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
  // pino-pretty only in development: production (NODE_ENV=production, set in
  // the Dockerfile) emits plain JSON lines, which log shippers can parse and
  // which avoid pino-pretty's worker-thread transport overhead.
  const isDev = process.env.NODE_ENV !== 'production';
  const app = Fastify({
    logger: {
      level: config.logLevel,
      ...(isDev && {
        transport: {
          target: 'pino-pretty',
          options: {
            translateTime: 'HH:MM:ss Z',
            ignore: 'pid,hostname',
          },
        },
      }),
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
    if (request.url.startsWith('/health') || request.url.startsWith('/docs')) {
      return;
    }
    if (!hasValidApiKey(request.headers['x-api-key'], config.whatsappApiKey)) {
      app.log.warn({ url: request.url, ip: request.ip }, 'Unauthorized request');
      return reply.code(401).send({ error: 'Invalid or missing API key' });
    }
  });

  // Register rate limiting
  await app.register(FastifyRateLimit, {
    max: config.rateLimitGlobal,
    timeWindow: '1 minute',
    // Authenticated inter-service calls are exempt: they all share the AI API's
    // IP, so a per-IP budget would throttle legitimate bot traffic. The auth hook
    // above runs first (route-level rate-limit hooks run last), so bad-key
    // requests get a 401 without ever being counted — the limiter does NOT
    // throttle API-key guessing.
    allowList: (req) =>
      req.url.startsWith('/health') ||
      hasValidApiKey(req.headers['x-api-key'], config.whatsappApiKey),
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
  await registerConnectionRoutes(app);
  await registerMessagingRoutes(app);
  await registerMediaRoutes(app);
  await registerOperationsRoutes(app);

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

  // Graceful shutdown: stop accepting requests and let in-flight ones finish,
  // flush Sentry, then exit 0. Errors while closing are logged, never rethrown.
  for (const signal of ['SIGTERM', 'SIGINT'] as const) {
    process.on(signal, async () => {
      app.log.info({ signal }, 'Received shutdown signal, shutting down gracefully...');
      try {
        await app.close();
      } catch (err) {
        app.log.error({ err }, 'Error during graceful shutdown');
      }
      await Sentry.close(2000);
      process.exit(0);
    });
  }
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
