/**
 * Fastify test-app builder for whatsapp-client route integration tests.
 *
 * Creates a lightweight Fastify instance with Zod validation, multipart
 * support, and all route plugins registered — but WITHOUT the API key
 * authentication hook, WhatsApp connection, rate limiting, Swagger, or CORS.
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
import { registerMessagingRoutes } from '../../src/routes/messaging.js';
import { registerMediaRoutes } from '../../src/routes/media.js';
import { registerOperationsRoutes } from '../../src/routes/operations.js';
import { registerMetricsRoutes } from '../../src/routes/metrics.js';

/**
 * Build a Fastify instance suitable for route integration tests.
 *
 * - Zod type provider, validator, and serializer are configured.
 * - Multipart plugin is registered (for media routes).
 * - All route plugins (health, messaging, media, operations) are registered.
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

  // Register all route plugins
  await registerHealthRoutes(app);
  await registerMessagingRoutes(app);
  await registerMediaRoutes(app);
  await registerOperationsRoutes(app);
  await registerMetricsRoutes(app);

  // Ensure plugins are loaded
  await app.ready();

  return app;
}
