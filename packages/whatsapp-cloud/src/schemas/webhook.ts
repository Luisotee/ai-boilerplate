import { z } from 'zod';

/**
 * Schema for the webhook verification GET request query parameters.
 * Meta sends these when verifying the webhook URL during setup.
 *
 * Example: GET /webhook?hub.mode=subscribe&hub.verify_token=mytoken&hub.challenge=1234567890
 */
export const WebhookVerifySchema = z.object({
  'hub.mode': z.string().describe('Should be "subscribe"'),
  'hub.verify_token': z.string().describe('The verify token configured in Meta dashboard'),
  'hub.challenge': z.string().describe('Challenge string to echo back'),
});

export type WebhookVerifyQuery = z.infer<typeof WebhookVerifySchema>;

/**
 * Basic schema for the webhook POST body.
 * Full type-safe parsing is handled by the types in utils/message.ts;
 * this schema validates the top-level structure for Fastify route validation.
 */
export const WebhookBodySchema = z.object({
  object: z.string().describe('Should be "whatsapp_business_account"'),
  entry: z
    .array(
      z.object({
        id: z.string(),
        changes: z.array(
          z.object({
            value: z.object({
              messaging_product: z.string().optional(),
              metadata: z
                .object({
                  phone_number_id: z.string(),
                  display_phone_number: z.string(),
                })
                .optional(),
              contacts: z
                .array(
                  z.object({
                    profile: z.object({ name: z.string().optional() }).optional(),
                    wa_id: z.string(),
                  })
                )
                .optional(),
              messages: z
                .array(
                  z
                    .object({
                      from: z.string(),
                      id: z.string(),
                      timestamp: z.string(),
                      type: z.string(),
                    })
                    .passthrough()
                )
                .optional(),
              statuses: z
                .array(
                  z.object({
                    id: z.string(),
                    status: z.string(),
                    timestamp: z.string(),
                    recipient_id: z.string(),
                  })
                )
                .optional(),
            }),
            field: z.string().optional(),
          })
        ),
      })
    )
    .describe('Array of webhook entries'),
});

export type WebhookBody = z.infer<typeof WebhookBodySchema>;
