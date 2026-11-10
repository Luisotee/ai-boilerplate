import { describe, it, expect, vi, beforeEach } from 'vitest';

const polling = vi.hoisted(() => ({ intervalMs: 0, maxIterations: 5, maxDurationMs: 60_000 }));

vi.mock('../../src/config.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/config.js')>();
  return { config: { ...actual.config, polling } };
});

vi.mock('../../src/utils/fetch.js', () => ({ fetchWithTimeout: vi.fn() }));

vi.mock('../../src/logger.js', () => ({
  logger: { info: vi.fn(), warn: vi.fn(), error: vi.fn(), debug: vi.fn() },
}));

import { sendMessageToAI, PollTimeoutError } from '../../src/api-client.js';
import { fetchWithTimeout } from '../../src/utils/fetch.js';

const mockFetch = fetchWithTimeout as ReturnType<typeof vi.fn>;

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, statusText: 'x', json: async () => body };
}

const opts = { conversationType: 'private' as const, messageId: 'M1' };

describe('sendMessageToAI — replies delivered elsewhere', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    polling.maxIterations = 5;
  });

  it('returns null for a duplicate (redelivered) message without polling', async () => {
    mockFetch.mockResolvedValueOnce(json({ duplicate: true, is_command: false }));

    await expect(sendMessageToAI('jid', 'hi', opts)).resolves.toBeNull();
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('returns null for a job finished silently (pushed late or merged)', async () => {
    mockFetch
      .mockResolvedValueOnce(json({ job_id: 'j1', status: 'queued' }))
      .mockResolvedValueOnce(
        json({ job_id: 'j1', status: 'complete', complete: true, silent: true })
      );

    await expect(sendMessageToAI('jid', 'hi', opts)).resolves.toBeNull();
  });

  it('still returns the reply of a normal job', async () => {
    mockFetch
      .mockResolvedValueOnce(json({ job_id: 'j1', status: 'queued' }))
      .mockResolvedValueOnce(
        json({ job_id: 'j1', status: 'complete', complete: true, full_response: 'hello' })
      );

    await expect(sendMessageToAI('jid', 'hi', opts)).resolves.toBe('hello');
  });

  it('throws PollTimeoutError when the reply is not ready in time', async () => {
    polling.maxIterations = 2;
    mockFetch
      .mockResolvedValueOnce(json({ job_id: 'j1', status: 'queued' }))
      .mockResolvedValue(json({ job_id: 'j1', status: 'queued', complete: false }));

    await expect(sendMessageToAI('jid', 'hi', opts)).rejects.toBeInstanceOf(PollTimeoutError);
  });
});
