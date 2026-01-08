import type { WAMessage, WAMessageKey } from '@whiskeysockets/baileys';

export interface ChatMessage {
  phone: string; // User's phone number (e.g., "1234567890@s.whatsapp.net")
  message: string; // User's message text
  timestamp: Date;
}

export interface AIResponse {
  response: string;
}

// Queue system types
export interface QueuedMessage {
  msg: WAMessage; // Original Baileys message
  messageKey: WAMessageKey; // For sending reactions
  messageText: string; // Extracted text
  whatsappJid: string; // Conversation identifier
  isGroup: boolean; // Group vs private chat
}

export interface MessageQueue {
  messages: QueuedMessage[]; // FIFO queue
  isProcessing: boolean; // Lock to prevent concurrent processing
}
