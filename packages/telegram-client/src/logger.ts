import pino from 'pino';
import { config } from './config.js';

const isDev = process.env.NODE_ENV !== 'production';

export const logger = pino({
  level: config.logLevel,
  ...(isDev && {
    transport: {
      target: 'pino-pretty',
      options: {
        colorize: true,
        translateTime: 'SYS:standard',
        ignore: 'pid,hostname',
      },
    },
  }),
  serializers: {
    error: pino.stdSerializers.err,
  },
});
