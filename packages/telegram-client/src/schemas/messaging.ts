import { z } from 'zod';

// The AI API's WhatsAppClient sends `phoneNumber` as the conversation
// identifier regardless of the underlying platform. For Telegram we accept
// the synthetic "tg:<chat_id>" JID OR a bare numeric chat id string.
export const SendTextSchema = z.object({
  phoneNumber: z.string().describe('Telegram JID ("tg:<chat_id>") or numeric chat id'),
  text: z.string().min(1).describe('Message text'),
  quoted_message_id: z.string().optional().describe('Telegram message_id to reply to'),
});

export const SendReactionSchema = z.object({
  phoneNumber: z.string(),
  message_id: z.string().describe('Telegram message_id to react to'),
  emoji: z.string(),
});

export const TypingIndicatorSchema = z.object({
  phoneNumber: z.string(),
  state: z.enum(['composing', 'paused']),
  message_id: z.string().optional(),
});

export const SendTextResponseSchema = z.object({
  success: z.boolean(),
  message_id: z.string().optional(),
});

export const SuccessResponseSchema = z.object({
  success: z.boolean(),
});

export const ErrorResponseSchema = z.object({
  error: z.string(),
});

export const HealthResponseSchema = z.object({
  status: z.string(),
  telegram_connected: z.boolean(),
});

export const ReadyResponseSchema = z.object({
  status: z.enum(['ready', 'not_ready']),
  checks: z.record(z.string(), z.string()),
});

export type SendTextRequest = z.infer<typeof SendTextSchema>;
export type SendReactionRequest = z.infer<typeof SendReactionSchema>;
export type TypingIndicatorRequest = z.infer<typeof TypingIndicatorSchema>;
