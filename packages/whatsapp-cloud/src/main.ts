import crypto from 'node:crypto';
import { config } from './config.js';
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
import { setCloudApiReady } from './services/cloud-state.js';
import { registerHealthRoutes } from './routes/health.js';
import { registerWebhookRoutes } from './routes/webhook.js';
import { registerMessagingRoutes } from './routes/messaging.js';
import { registerMediaRoutes } from './routes/media.js';
import { registerOperationsRoutes } from './routes/operations.js';

/**
 * Transform function that handles both Zod and plain JSON Schema.
 * Multipart routes use plain JSON Schema; all other routes use Zod.
 */
function createMixedSchemaTransform() {
  const multipartRoutes = [
    '/whatsapp/send-image',
    '/whatsapp/send-document',
    '/whatsapp/send-audio',
    '/whatsapp/send-video',
  ];

  // Webhook routes also use plain JSON Schema
  const plainSchemaRoutes = ['/webhook'];

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return function mixedTransform(transformObject: any) {
    const { schema, url } = transformObject;

    if (multipartRoutes.includes(url) || plainSchemaRoutes.includes(url)) {
      return { schema, url };
    }

    return jsonSchemaTransform(transformObject);
  };
}

/**
 * Validate Cloud API credentials by calling the Graph API.
 * GET https://graph.facebook.com/{version}/{phone_number_id}
 */
async function validateCloudApiCredentials(): Promise<boolean> {
  const { graphApiBaseUrl, graphApiVersion, phoneNumberId, accessToken } = config.meta;

  if (!phoneNumberId || !accessToken) {
    return false;
  }

  try {
    const url = `${graphApiBaseUrl}/${graphApiVersion}/${phoneNumberId}`;
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${accessToken}`,
      },
    });

    return response.ok;
  } catch {
    return false;
  }
}

async function start() {
  // Validate required security config
  if (!config.whatsappApiKey) {
    throw new Error('WHATSAPP_CLOUD_API_KEY environment variable is required');
  }
  if (!config.aiApiKey) {
    throw new Error('AI_API_KEY environment variable is required');
  }
  if (!config.meta.phoneNumberId) {
    throw new Error('META_PHONE_NUMBER_ID environment variable is required');
  }
  if (!config.meta.accessToken) {
    throw new Error('META_ACCESS_TOKEN environment variable is required');
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
  // Excludes /health, /docs*, and /webhook (Meta needs unauthenticated access)
  app.addHook('onRequest', async (request, reply) => {
    if (
      request.url === '/health' ||
      request.url.startsWith('/docs') ||
      request.url.startsWith('/webhook')
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
    allowList: (req) => req.url === '/health' || req.url.startsWith('/webhook'),
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
        title: 'WhatsApp Cloud API',
        description: 'REST API for WhatsApp messaging via Meta Cloud API',
        version: '1.0.0',
      },
      servers: [{ url: `http://localhost:${config.server.port}`, description: 'Development' }],
      tags: [
        { name: 'Health', description: 'Health check endpoints' },
        { name: 'Webhook', description: 'Meta webhook endpoints' },
        { name: 'Messaging', description: 'Text messaging, reactions, typing' },
        { name: 'Media', description: 'Images, videos, documents, audio' },
        { name: 'Operations', description: 'Edit, delete messages' },
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

  // Validate Cloud API credentials
  app.log.info('Validating WhatsApp Cloud API credentials...');
  const isValid = await validateCloudApiCredentials();
  if (isValid) {
    setCloudApiReady(true);
    app.log.info('WhatsApp Cloud API credentials validated successfully');
  } else {
    app.log.warn(
      'Failed to validate Cloud API credentials — API will start but report unhealthy. ' +
        'Check META_PHONE_NUMBER_ID and META_ACCESS_TOKEN.'
    );
  }

  // Register API routes
  await registerHealthRoutes(app);
  await registerWebhookRoutes(app);
  await registerMessagingRoutes(app);
  await registerMediaRoutes(app);
  await registerOperationsRoutes(app);

  // Start server
  await app.listen({ port: config.server.port, host: config.server.host });

  app.log.info('='.repeat(60));
  app.log.info(
    `WhatsApp Cloud API listening on http://${config.server.host}:${config.server.port}`
  );
  app.log.info(`API Docs: http://localhost:${config.server.port}/docs`);
  app.log.info(`OpenAPI JSON: http://localhost:${config.server.port}/docs/json`);
  app.log.info(`Webhook URL: http://localhost:${config.server.port}/webhook`);
  app.log.info('='.repeat(60));
}

start().catch((error) => {
  console.error('Failed to start server:', error);
  process.exit(1);
});
