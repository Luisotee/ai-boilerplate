import './instrument.js';
import { Sentry } from './instrument.js';
import crypto from 'node:crypto';
import { config } from './config.js';
import Fastify from 'fastify';
import FastifySwagger from '@fastify/swagger';
import FastifySwaggerUI from '@fastify/swagger-ui';
import FastifyCors from '@fastify/cors';
import FastifyRateLimit from '@fastify/rate-limit';
import {
  serializerCompiler,
  validatorCompiler,
  jsonSchemaTransform,
  type ZodTypeProvider,
} from 'fastify-type-provider-zod';
import { bot } from './bot.js';
import { logger } from './logger.js';
import { markBotReady } from './services/bot-state.js';
import { registerUpdateHandlers } from './updates.js';
import { registerHealthRoutes } from './routes/health.js';
import { registerWebhookRoutes } from './routes/webhook.js';
import { registerMessagingRoutes } from './routes/messaging.js';
import { registerMediaRoutes } from './routes/media.js';
import { registerMetricsRoutes } from './routes/metrics.js';

function createMixedSchemaTransform() {
  const plainSchemaRoutes = ['/webhook'];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return function mixedTransform(transformObject: any) {
    const { schema, url } = transformObject;
    if (plainSchemaRoutes.includes(url)) return { schema, url };
    return jsonSchemaTransform(transformObject);
  };
}

async function start() {
  if (!config.telegramApiKey) {
    throw new Error(
      'TELEGRAM_API_KEY (or fallback WHATSAPP_API_KEY) environment variable is required'
    );
  }
  if (!config.aiApiKey) {
    throw new Error('AI_API_KEY environment variable is required');
  }
  if (!config.telegram.botToken) {
    throw new Error('TELEGRAM_BOT_TOKEN environment variable is required');
  }

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
  }).withTypeProvider<ZodTypeProvider>();

  app.setValidatorCompiler(validatorCompiler);
  app.setSerializerCompiler(serializerCompiler);

  // CORS
  const corsOrigins = config.corsOrigins
    ? config.corsOrigins
        .split(',')
        .map((o) => o.trim())
        .filter(Boolean)
    : [];
  await app.register(FastifyCors, {
    origin: corsOrigins.length > 0 ? corsOrigins : false,
  });

  // API Key auth — exempts health, metrics, docs, and /webhook (Telegram uses
  // its own secret_token header for webhook verification).
  app.addHook('onRequest', async (request, reply) => {
    if (
      request.url.startsWith('/health') ||
      request.url === '/metrics' ||
      request.url.startsWith('/docs') ||
      request.url.startsWith('/webhook')
    ) {
      return;
    }
    const apiKey = request.headers['x-api-key'];
    const expected = config.telegramApiKey;
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

  await app.register(FastifyRateLimit, {
    max: config.rateLimitGlobal,
    timeWindow: '1 minute',
    allowList: (req) =>
      req.url.startsWith('/health') || req.url === '/metrics' || req.url.startsWith('/webhook'),
  });

  await app.register(FastifySwagger, {
    openapi: {
      openapi: '3.0.3',
      info: {
        title: 'Telegram Bot Client API',
        description: 'REST API for Telegram messaging via grammY + Bot API',
        version: '1.0.0',
      },
      servers: [{ url: `http://localhost:${config.server.port}`, description: 'Development' }],
      tags: [
        { name: 'Health', description: 'Health check endpoints' },
        { name: 'Webhook', description: 'Telegram webhook endpoints' },
        { name: 'Messaging', description: 'Text messaging, reactions, typing' },
        { name: 'Media', description: 'Platform-specific media (location/contact are 501)' },
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

  await app.register(FastifySwaggerUI, {
    routePrefix: '/docs',
    uiConfig: { docExpansion: 'list', deepLinking: false },
  });

  // Register grammY update dispatch BEFORE the webhook route starts accepting
  // updates — otherwise early deliveries have no handlers to run.
  registerUpdateHandlers();

  // bot.init() populates botInfo.id/username so mention detection works.
  app.log.info('Initializing Telegram bot (fetching getMe)...');
  try {
    await bot.init();
    markBotReady();
    app.log.info({ botUsername: bot.botInfo.username, botId: bot.botInfo.id }, 'Bot initialized');
  } catch (err) {
    app.log.error({ err }, 'Failed to initialize Telegram bot — readiness will report not ready');
  }

  await registerHealthRoutes(app);
  await registerWebhookRoutes(app);
  await registerMessagingRoutes(app);
  await registerMediaRoutes(app);
  await registerMetricsRoutes(app);

  if (process.env.SENTRY_DSN_NODE) {
    Sentry.setupFastifyErrorHandler(app);
  }

  await app.listen({ port: config.server.port, host: config.server.host });

  // Register webhook with Telegram only if a public URL is configured.
  // For local development without a tunnel, skip this and use long-polling
  // tooling (ngrok, cloudflared, etc.) separately.
  if (config.telegram.publicWebhookUrl) {
    try {
      await bot.api.setWebhook(config.telegram.publicWebhookUrl, {
        secret_token: config.telegram.webhookSecret || undefined,
        allowed_updates: ['message', 'my_chat_member'],
        drop_pending_updates: config.telegram.dropPendingUpdates,
      });
      app.log.info(
        { url: config.telegram.publicWebhookUrl },
        'Registered Telegram webhook with setWebhook'
      );
    } catch (err) {
      app.log.error({ err }, 'Failed to register Telegram webhook');
    }
  } else {
    app.log.warn(
      'TELEGRAM_PUBLIC_WEBHOOK_URL is not set — skipping setWebhook. ' +
        'Register manually (e.g. via ngrok + curl) for inbound deliveries to work.'
    );
  }

  app.log.info('='.repeat(60));
  app.log.info(`Telegram client listening on http://${config.server.host}:${config.server.port}`);
  app.log.info(`API Docs: http://localhost:${config.server.port}/docs`);
  app.log.info(`Webhook URL: http://localhost:${config.server.port}/webhook`);
  if (config.whitelistPhones.size > 0) {
    app.log.info({ count: config.whitelistPhones.size }, 'User whitelist ENABLED');
  } else {
    app.log.info('User whitelist DISABLED (all users allowed)');
  }
  app.log.info('='.repeat(60));

  // Graceful shutdown — no bot.stop() needed in webhook mode.
  for (const sig of ['SIGINT', 'SIGTERM'] as const) {
    process.on(sig, async () => {
      app.log.info({ signal: sig }, 'Shutting down');
      await app.close();
      await Sentry.close(2000);
      process.exit(0);
    });
  }
}

async function shutdownWithError(err: unknown, message: string): Promise<never> {
  Sentry.captureException(err);
  logger.fatal({ err }, message);
  await Sentry.close(2000);
  process.exit(1);
}

start().catch((error) => {
  void shutdownWithError(error, 'Failed to start server');
});

process.on('unhandledRejection', (reason) => {
  void shutdownWithError(reason, 'Unhandled rejection');
});
