import { z } from 'zod';

// Request schemas
export const SendTextSchema = z.object({
  phoneNumber: z.string().describe('Phone number (e.g., 5511999999999)'),
  text: z.string().min(1).describe('Message text'),
  quoted_message_id: z.string().optional().describe('Message ID to quote/reply to'),
});

export const SendReactionSchema = z.object({
  phoneNumber: z.string().describe('Phone number (e.g., 5511999999999)'),
  message_id: z.string().describe('Message ID to react to'),
  emoji: z.string().describe('Reaction emoji (e.g., "👍", "❤️")'),
});

export const TypingIndicatorSchema = z.object({
  phoneNumber: z.string().describe('Phone number (e.g., 5511999999999)'),
  state: z.enum(['composing', 'paused']).describe('Typing state'),
});

export const ReadMessagesSchema = z.object({
  phoneNumber: z.string().describe('Phone number (e.g., 5511999999999)'),
  message_ids: z.array(z.string()).describe('Message IDs to mark as read'),
});

export const EditMessageSchema = z.object({
  phoneNumber: z.string().describe('Phone number (e.g., 5511999999999)'),
  message_id: z.string().describe('Message ID to edit'),
  new_text: z.string().min(1).describe('Updated message text'),
});

export const DeleteMessageSchema = z.object({
  phoneNumber: z.string().describe('Phone number (e.g., 5511999999999)'),
  message_id: z.string().describe('Message ID to delete'),
});

// Response schemas
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
  whatsapp_connected: z.boolean(),
});

// Types
export type SendTextRequest = z.infer<typeof SendTextSchema>;
export type SendReactionRequest = z.infer<typeof SendReactionSchema>;
export type TypingIndicatorRequest = z.infer<typeof TypingIndicatorSchema>;
export type ReadMessagesRequest = z.infer<typeof ReadMessagesSchema>;
export type EditMessageRequest = z.infer<typeof EditMessageSchema>;
export type DeleteMessageRequest = z.infer<typeof DeleteMessageSchema>;
