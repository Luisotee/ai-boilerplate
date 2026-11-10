import { fetchJobMedia, type JobMedia } from '../api-client.js';
import { logger } from '../logger.js';

/**
 * Sends the images of a reply (ai-api `reply_media.py`). The bytes come from
 * ai-api, which downloaded them through its SSRF-hardened fetch: this client
 * never fetches a model-chosen URL itself.
 */
export interface ReplyMediaSenders {
  image: (buffer: Buffer, item: JobMedia) => Promise<unknown>;
  text: (text: string) => Promise<unknown>;
}

/** What the user gets instead of an image that could not be sent. */
export function mediaFallbackText(item: JobMedia): string {
  return item.caption ? `${item.caption}: ${item.url}` : item.url;
}

/**
 * Send each image in order; on any failure send its link as text instead.
 * Never throws, so an image problem can't trigger the failure reaction after
 * the text reply was already delivered. Returns how many images were sent.
 */
export async function sendReplyMedia(
  jobId: string,
  media: JobMedia[],
  senders: ReplyMediaSenders
): Promise<number> {
  let sent = 0;
  for (const item of media) {
    try {
      const buffer = await fetchJobMedia(jobId, item.index);
      await senders.image(buffer, item);
      sent++;
    } catch (error) {
      logger.warn({ error, jobId, index: item.index }, 'Reply image failed; sending its link');
      try {
        await senders.text(mediaFallbackText(item));
      } catch (textError) {
        logger.warn({ error: textError, jobId, index: item.index }, 'Image fallback text failed');
      }
    }
  }
  if (media.length) logger.info({ jobId, sent, total: media.length }, 'Sent reply images');
  return sent;
}
