import * as Sentry from '@sentry/node';

const dsn = process.env.SENTRY_DSN_NODE;

if (dsn) {
  Sentry.init({
    dsn,
    environment: process.env.SENTRY_ENVIRONMENT ?? process.env.NODE_ENV ?? 'development',
    tracesSampleRate: Number(process.env.SENTRY_TRACES_SAMPLE_RATE ?? '0'),
    serverName: 'whatsapp-cloud',
    sendDefaultPii: false,
  });
}

export { Sentry };
