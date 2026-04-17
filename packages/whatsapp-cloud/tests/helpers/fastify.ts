/**
 * Fastify test-app builder for whatsapp-cloud route integration tests.
 *
 * Creates a lightweight Fastify instance with Zod validation, multipart
 * support, and all route plugins registered — but WITHOUT the API key
 * authentication hook, Cloud API credential validation, rate limiting,
 * Swagger, or CORS.
 *
 * IMPORTANT: The raw-body JSON parser used in production (for HMAC
 * verification) is NOT installed here. Webhook tests that need signature
 * verification should add it separately or test at a higher level.
 *
 * Usage:
 *   const app = await buildTestApp();
 *   const res = await app.inject({ method: 'POST', url: '/whatsapp/send-text', ... });
 *   // assertions on res
 *   await app.close();
 */

import Fastify from 'fastify';
import FastifyMultipart, { ajvFilePlugin } from '@fastify/multipart';
import {
  serializerCompiler,
  validatorCompiler,
  type ZodTypeProvider,
} from 'fastify-type-provider-zod';
import { registerHealthRoutes } from '../../src/routes/health.js';
import { registerWebhookRoutes } from '../../src/routes/webhook.js';
import { registerMessagingRoutes } from '../../src/routes/messaging.js';
import { registerMediaRoutes } from '../../src/routes/media.js';
import { registerOperationsRoutes } from '../../src/routes/operations.js';
import { registerMetricsRoutes } from '../../src/routes/metrics.js';

/**
 * Build a Fastify instance suitable for route integration tests.
 *
 * - Zod type provider, validator, and serializer are configured.
 * - Multipart plugin is registered (for media routes).
 * - All route plugins (health, webhook, messaging, media, operations) are registered.
 * - Auth hook is NOT installed — requests do not need an X-API-Key header.
 * - Logging is silenced to keep test output clean.
 *
 * @returns A ready Fastify instance (not yet listening on a port).
 */
export async function buildTestApp() {
  const app = Fastify({
    logger: false,
    ajv: {
      plugins: [ajvFilePlugin],
    },
  }).withTypeProvider<ZodTypeProvider>();

  // Zod validators/serializers
  app.setValidatorCompiler(validatorCompiler);
  app.setSerializerCompiler(serializerCompiler);

  // Multipart support for media routes
  await app.register(FastifyMultipart, {
    attachFieldsToBody: true,
    limits: {
      fileSize: 50 * 1024 * 1024,
    },
  });

  // Register all route plugins (same order as production main.ts)
  await registerHealthRoutes(app);
  await registerWebhookRoutes(app);
  await registerMessagingRoutes(app);
  await registerMediaRoutes(app);
  await registerOperationsRoutes(app);
  await registerMetricsRoutes(app);

  // Ensure plugins are loaded
  await app.ready();

  return app;
}
