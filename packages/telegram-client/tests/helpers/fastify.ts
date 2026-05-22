/**
 * Fastify test-app builder for telegram-client route integration tests.
 *
 * Builds a Fastify instance with Zod validation and all route plugins
 * registered — but no API-key auth hook, no Bot API calls, no rate limit.
 *
 * Health routes use isBotReady() internally; set that via markBotReady()
 * in individual tests if the route depends on it.
 *
 * For messaging-routes and webhook-routes tests, callers should mock
 * bot-state and/or telegram-api before importing this helper.
 */
import Fastify from 'fastify';
import {
  serializerCompiler,
  validatorCompiler,
  type ZodTypeProvider,
} from 'fastify-type-provider-zod';
import { registerHealthRoutes } from '../../src/routes/health.js';
import { registerMessagingRoutes } from '../../src/routes/messaging.js';
import { registerMediaRoutes } from '../../src/routes/media.js';
import { registerMetricsRoutes } from '../../src/routes/metrics.js';
import { registerWebhookRoutes } from '../../src/routes/webhook.js';

export interface BuildTestAppOptions {
  /** Skip webhook registration (it loads the real grammY bot). Default true. */
  includeWebhook?: boolean;
}

export async function buildTestApp(options: BuildTestAppOptions = {}) {
  const { includeWebhook = false } = options;
  const app = Fastify({ logger: false }).withTypeProvider<ZodTypeProvider>();
  app.setValidatorCompiler(validatorCompiler);
  app.setSerializerCompiler(serializerCompiler);

  await registerHealthRoutes(app);
  await registerMessagingRoutes(app);
  await registerMediaRoutes(app);
  await registerMetricsRoutes(app);
  if (includeWebhook) {
    await registerWebhookRoutes(app);
  }

  await app.ready();
  return app;
}
