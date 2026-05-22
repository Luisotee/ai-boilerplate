/**
 * Unit tests for handlers/photo.ts.
 *
 * extractPhotoData picks the last (highest-resolution) PhotoSize and returns
 * a discriminated PhotoExtraction (`ok | too-large | download-error`) so the
 * caller can render kind-specific user replies.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { GrammyError } from 'grammy';

vi.mock('../../src/services/telegram-api.js', () => ({
  downloadFile: vi.fn(),
}));

import { extractPhotoData } from '../../src/handlers/photo.js';
import * as telegramApi from '../../src/services/telegram-api.js';

const mockDownloadFile = telegramApi.downloadFile as ReturnType<typeof vi.fn>;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function makeCtx(photo: any): any {
  return { msg: { photo } };
}

describe('extractPhotoData', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns download-error when no photo is present', async () => {
    const result = await extractPhotoData({ msg: {} } as never);
    expect(result).toEqual({ kind: 'download-error' });
    expect(mockDownloadFile).not.toHaveBeenCalled();
  });

  it('returns download-error when the photo array is empty', async () => {
    const result = await extractPhotoData(makeCtx([]));
    expect(result).toEqual({ kind: 'download-error' });
  });

  it('picks the highest-resolution (last) PhotoSize and returns base64 JPEG', async () => {
    const buffer = Buffer.from('PHOTO-BYTES');
    mockDownloadFile.mockResolvedValueOnce({ buffer, filePath: 'photos/file_99.jpg' });

    const result = await extractPhotoData(
      makeCtx([
        { file_id: 'thumb', width: 90, height: 90 },
        { file_id: 'medium', width: 320, height: 320 },
        { file_id: 'large', width: 1280, height: 1280 },
      ])
    );

    expect(result).toEqual({
      kind: 'ok',
      data: buffer.toString('base64'),
      mimetype: 'image/jpeg',
    });
    expect(mockDownloadFile).toHaveBeenCalledWith('large');
  });

  it('returns too-large when the Bot API rejects the file as oversize', async () => {
    const tooBig = new GrammyError(
      'Bot API rejected',
      { ok: false, error_code: 400, description: 'Bad Request: file is too big' },
      'getFile',
      {}
    );
    mockDownloadFile.mockRejectedValueOnce(tooBig);

    const result = await extractPhotoData(
      makeCtx([{ file_id: 'huge', width: 4096, height: 4096 }])
    );

    expect(result).toEqual({ kind: 'too-large' });
  });

  it('returns download-error on generic network failure', async () => {
    mockDownloadFile.mockRejectedValueOnce(new Error('ECONNRESET'));

    const result = await extractPhotoData(makeCtx([{ file_id: 'big', width: 1280, height: 1280 }]));

    expect(result).toEqual({ kind: 'download-error' });
  });

  it('returns download-error on auth failure (401) instead of bucketing as ok', async () => {
    const authErr = new GrammyError(
      'Unauthorized',
      { ok: false, error_code: 401, description: 'Unauthorized' },
      'getFile',
      {}
    );
    mockDownloadFile.mockRejectedValueOnce(authErr);

    const result = await extractPhotoData(makeCtx([{ file_id: 'x', width: 800, height: 600 }]));

    expect(result).toEqual({ kind: 'download-error' });
  });
});
