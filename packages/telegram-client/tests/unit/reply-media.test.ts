import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../src/api-client.js', () => ({
  fetchJobMedia: vi.fn(),
}));

vi.mock('../../src/logger.js', () => ({
  logger: { info: vi.fn(), warn: vi.fn(), error: vi.fn(), debug: vi.fn() },
}));

import { fetchJobMedia, type JobMedia } from '../../src/api-client.js';
import { mediaFallbackText, sendReplyMedia } from '../../src/utils/reply-media.js';

const mockFetch = fetchJobMedia as unknown as ReturnType<typeof vi.fn>;

const item = (index: number, caption = `img ${index}`): JobMedia => ({
  index,
  caption,
  mimetype: 'image/png',
  url: `https://maps.example.com/${index}.png`,
});

describe('sendReplyMedia', () => {
  beforeEach(() => vi.clearAllMocks());

  it('fetches each image from ai-api and sends it in order', async () => {
    mockFetch.mockImplementation(async (_job: string, index: number) => Buffer.from(`b${index}`));
    const image = vi.fn().mockResolvedValue(undefined);
    const text = vi.fn();

    const sent = await sendReplyMedia('job-1', [item(0), item(1)], { image, text });

    expect(sent).toBe(2);
    expect(mockFetch.mock.calls).toEqual([
      ['job-1', 0],
      ['job-1', 1],
    ]);
    expect(image.mock.calls.map(([buf, it]) => [buf.toString(), it.index])).toEqual([
      ['b0', 0],
      ['b1', 1],
    ]);
    expect(text).not.toHaveBeenCalled();
  });

  it('sends the link as text when the bytes cannot be fetched', async () => {
    mockFetch.mockRejectedValueOnce(new Error('Job media failed: 404'));
    const image = vi.fn();
    const text = vi.fn().mockResolvedValue(undefined);

    const sent = await sendReplyMedia('job-1', [item(0, 'Fire map')], { image, text });

    expect(sent).toBe(0);
    expect(image).not.toHaveBeenCalled();
    expect(text).toHaveBeenCalledWith('Fire map: https://maps.example.com/0.png');
  });

  it('sends the link as text when the platform rejects the image, then goes on', async () => {
    mockFetch.mockResolvedValue(Buffer.from('x'));
    const image = vi
      .fn()
      .mockRejectedValueOnce(new Error('upload failed'))
      .mockResolvedValueOnce(undefined);
    const text = vi.fn().mockResolvedValue(undefined);

    const sent = await sendReplyMedia('job-1', [item(0, ''), item(1)], { image, text });

    expect(sent).toBe(1);
    expect(text).toHaveBeenCalledWith('https://maps.example.com/0.png');
    expect(image).toHaveBeenCalledTimes(2);
  });

  it('never throws, even when the fallback text fails too', async () => {
    mockFetch.mockRejectedValue(new Error('down'));
    const text = vi.fn().mockRejectedValue(new Error('also down'));

    await expect(sendReplyMedia('job-1', [item(0)], { image: vi.fn(), text })).resolves.toBe(0);
  });

  it('formats the fallback with or without a caption', () => {
    expect(mediaFallbackText(item(3, 'Map'))).toBe('Map: https://maps.example.com/3.png');
    expect(mediaFallbackText(item(3, ''))).toBe('https://maps.example.com/3.png');
  });
});
