import 'dotenv/config';

export const config = {
  aiApiUrl: process.env.AI_API_URL || 'http://localhost:8000',
  logLevel: process.env.LOG_LEVEL || 'info',
  server: {
    port: parseInt(process.env.WHATSAPP_API_PORT || '3001', 10),
    host: process.env.WHATSAPP_API_HOST || '0.0.0.0',
  },
} as const;
