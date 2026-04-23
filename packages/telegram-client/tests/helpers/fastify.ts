/**
 * Fastify test-app builder for telegram-client route integration tests.
 *
 * Builds a Fastify instance with Zod validation and all route plugins
 * registered — but no API-key auth hook, no Bot API calls, no rate limit.
 *
 * Health routes use isBotReady() internally; set that via markBotReady()
 * in individual tests if the route depends on it.
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

export async function buildTestApp() {
  const app = Fastify({ logger: false }).withTypeProvider<ZodTypeProvider>();
  app.setValidatorCompiler(validatorCompiler);
  app.setSerializerCompiler(serializerCompiler);

  await registerHealthRoutes(app);
  await registerMessagingRoutes(app);
  await registerMediaRoutes(app);
  await registerMetricsRoutes(app);

  await app.ready();
  return app;
}
