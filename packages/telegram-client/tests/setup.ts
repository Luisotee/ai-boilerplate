/**
 * Global test setup: ensures required env vars are present before any
 * production module imports (the bot module instantiates grammY's Bot at
 * import time, which requires a non-empty token).
 */
process.env.TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || 'test-token:dummy';
process.env.TELEGRAM_API_KEY = process.env.TELEGRAM_API_KEY || 'test-telegram-key';
process.env.AI_API_KEY = process.env.AI_API_KEY || 'test-ai-key';
process.env.AI_API_URL = process.env.AI_API_URL || 'http://localhost:8000';
