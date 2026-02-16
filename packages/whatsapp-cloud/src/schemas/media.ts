import { z } from 'zod';

// Location endpoint (JSON body)
export const SendLocationSchema = z.object({
  phoneNumber: z.string().describe('Phone number (e.g., 5511999999999)'),
  latitude: z.number().min(-90).max(90).describe('Latitude coordinate'),
  longitude: z.number().min(-180).max(180).describe('Longitude coordinate'),
  name: z.string().optional().describe('Location name (e.g., "Eiffel Tower")'),
  address: z.string().optional().describe('Location address'),
});

// Contact endpoint (JSON body)
export const SendContactSchema = z.object({
  phoneNumber: z.string().describe('Recipient phone number (e.g., 5511999999999)'),
  contactName: z.string().min(1).describe('Contact display name'),
  contactPhone: z.string().min(1).describe('Contact phone number (with country code)'),
  contactEmail: z.string().email().optional().describe('Contact email address'),
  contactOrg: z.string().optional().describe('Contact organization/company'),
});

// Response schemas
export const MediaResponseSchema = z.object({
  success: z.boolean(),
  message_id: z.string().optional().describe('WhatsApp message ID'),
});

export const ErrorResponseSchema = z.object({
  error: z.string(),
});

// Types
export type SendLocationRequest = z.infer<typeof SendLocationSchema>;
export type SendContactRequest = z.infer<typeof SendContactSchema>;
