import './instrument.js';
import { Sentry } from './instrument.js';
import { config } from './config.js';
import { hasValidApiKey } from './utils/api-key.js';
import { validateRequiredEnv } from './config-validation.js';
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
import { markBotDisconnected, markBotReady } from './services/bot-state.js';
import { startLongPolling, stopLongPolling } from './polling.js';
import type { RunnerHandle } from '@grammyjs/runner';
import { registerUpdateHandlers } from './updates.js';
import { registerHealthRoutes } from './routes/health.js';
import { registerWebhookRoutes } from './routes/webhook.js';
import { registerMessagingRoutes } from './routes/messaging.js';
import { registerMediaRoutes } from './routes/media.js';

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
  validateRequiredEnv(config);
  const polling = config.telegram.mode === 'polling';

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

  // API Key auth — exempts health, docs, and /webhook (Telegram uses
  // its own secret_token header for webhook verification). In polling mode the
  // /webhook route is never registered, so the exemption only yields a 404.
  app.addHook('onRequest', async (request, reply) => {
    if (
      request.url.startsWith('/health') ||
      request.url.startsWith('/docs') ||
      request.url.startsWith('/webhook')
    ) {
      return;
    }
    if (!hasValidApiKey(request.headers['x-api-key'], config.telegramApiKey)) {
      app.log.warn({ url: request.url, ip: request.ip }, 'Unauthorized request');
      return reply.code(401).send({ error: 'Invalid or missing API key' });
    }
  });

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
      req.url.startsWith('/webhook') ||
      hasValidApiKey(req.headers['x-api-key'], config.telegramApiKey),
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

  // Register grammY update dispatch BEFORE the webhook route (or the poll loop)
  // starts accepting updates — otherwise early deliveries have no handlers.
  registerUpdateHandlers();

  // bot.init() populates botInfo.id/username so mention detection works — in
  // BOTH modes: a bot that polls before init is silently deaf to every group
  // @-mention while still reporting healthy. If this fails we fail fast — a half-initialized bot with undefined botInfo
  // would silently drop every group @-mention, so it's safer to crash and let
  // Docker restart than to serve webhooks in a broken state.
  app.log.info('Initializing Telegram bot (fetching getMe)...');
  await bot.init();
  markBotReady();
  app.log.info({ botUsername: bot.botInfo.username, botId: bot.botInfo.id }, 'Bot initialized');

  await registerHealthRoutes(app);
  // Polling mode registers no /webhook route: it is exempt from API-key auth
  // and, without a public URL, would have no secret token to verify either.
  if (!polling) await registerWebhookRoutes(app);
  await registerMessagingRoutes(app);
  await registerMediaRoutes(app);

  if (process.env.SENTRY_DSN_NODE) {
    Sentry.setupFastifyErrorHandler(app);
  }

  await app.listen({ port: config.server.port, host: config.server.host });

  let runner: RunnerHandle | undefined;
  if (polling) {
    runner = await startLongPolling();
    // If polling dies the process is useless: flip health so probes stop
    // reporting OK, then exit and let Docker restart it with backoff.
    void runner.task()?.catch((err) => {
      markBotDisconnected();
      void shutdownWithError(err, 'Telegram long polling stopped unexpectedly');
    });
  } else if (config.telegram.publicWebhookUrl) {
    // Register webhook with Telegram only if a public URL is configured.
    try {
      await bot.api.setWebhook(config.telegram.publicWebhookUrl, {
        secret_token: config.telegram.webhookSecret || undefined,
        allowed_updates: ['message'],
        drop_pending_updates: config.telegram.dropPendingUpdates,
      });
      app.log.info(
        { url: config.telegram.publicWebhookUrl },
        'Registered Telegram webhook with setWebhook'
      );
    } catch (err) {
      app.log.error({ err }, 'Failed to register Telegram webhook');
      // Fail fast — same rationale as bot.init() above. A bot that can't
      // receive deliveries should not pass health checks; let Docker restart
      // with backoff rather than serve traffic in a half-initialized state.
      throw err;
    }
  } else {
    app.log.warn(
      'TELEGRAM_PUBLIC_WEBHOOK_URL is not set — skipping setWebhook. Register it ' +
        'manually, or set TELEGRAM_MODE=polling to receive updates without a public URL.'
    );
  }

  app.log.info('='.repeat(60));
  app.log.info(`Telegram client listening on http://${config.server.host}:${config.server.port}`);
  app.log.info(`API Docs: http://localhost:${config.server.port}/docs`);
  if (polling) {
    app.log.info(
      { concurrency: config.telegram.concurrency },
      'Long polling started (@grammyjs/runner) — no public URL needed'
    );
  } else {
    app.log.info(`Webhook URL: http://localhost:${config.server.port}/webhook`);
  }
  if (config.whitelistPhones.size > 0) {
    app.log.info({ count: config.whitelistPhones.size }, 'User whitelist ENABLED');
  } else {
    app.log.info('User whitelist DISABLED (all users allowed)');
  }
  app.log.info('='.repeat(60));

  // Graceful shutdown. Webhook mode just closes the server. Polling mode first
  // stops pulling updates and drains the handlers already running (runner.stop()
  // alone does not wait for them — see polling.ts), and only then closes the
  // HTTP server, which the AI API calls back into while those handlers run.
  for (const sig of ['SIGINT', 'SIGTERM'] as const) {
    process.on(sig, async () => {
      app.log.info({ signal: sig }, 'Shutting down');
      try {
        if (runner) await stopLongPolling(runner, app.log);
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
  await Sentry.close(2000);
  process.exit(1);
}

start().catch((error) => {
  void shutdownWithError(error, 'Failed to start server');
});

process.on('unhandledRejection', (reason) => {
  void shutdownWithError(reason, 'Unhandled rejection');
});
