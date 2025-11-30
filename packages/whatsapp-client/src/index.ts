import dotenv from 'dotenv';
import { logger } from './logger.js';
import { startWhatsAppClient } from './whatsapp.js';

dotenv.config();

async function main() {
  logger.info('Starting WhatsApp AI Agent Client...');

  // Validate environment variables
  if (!process.env.AI_API_URL) {
    logger.warn('AI_API_URL not set, using default: http://localhost:8000');
  }

  await startWhatsAppClient();
}

main().catch((error) => {
  logger.error({ error }, 'Fatal error in main');
  process.exit(1);
});
