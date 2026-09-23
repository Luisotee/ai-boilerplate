import { z } from 'zod';

// Request: at least one identifier for the requesting user must be provided.
// The caller (AI API) supplies these from the user's DB row, never from
// user-typed text — this is what enforces the privacy boundary.
export const SharedGroupsRequestSchema = z
  .object({
    jid: z.string().optional().describe('Requester JID (e.g. 5511…@s.whatsapp.net or …@lid)'),
    lid: z.string().optional().describe('Requester LID (e.g. 12345@lid)'),
    phone: z.string().optional().describe('Requester phone number (E.164 or digits)'),
  })
  .refine((b) => Boolean(b.jid || b.lid || b.phone), {
    message: 'At least one of jid, lid, or phone is required',
  });

export const SharedGroupSchema = z.object({
  groupJid: z.string().describe('Group JID (…@g.us)'),
  subject: z.string().describe('Group name/subject'),
});

export const SharedGroupsResponseSchema = z.object({
  groups: z.array(SharedGroupSchema),
});

export type SharedGroupsRequest = z.infer<typeof SharedGroupsRequestSchema>;
export type SharedGroup = z.infer<typeof SharedGroupSchema>;
