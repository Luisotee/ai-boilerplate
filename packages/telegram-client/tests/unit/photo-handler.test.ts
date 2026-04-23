/**
 * Unit tests for handlers/photo.ts.
 *
 * extractPhotoData picks the last (highest-resolution) PhotoSize and returns
 * it as base64, or null on download errors / missing photos.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

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

  it('returns null when no photo is present', async () => {
    const result = await extractPhotoData({ msg: {} } as never);
    expect(result).toBeNull();
    expect(mockDownloadFile).not.toHaveBeenCalled();
  });

  it('returns null when the photo array is empty', async () => {
    const result = await extractPhotoData(makeCtx([]));
    expect(result).toBeNull();
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
      data: buffer.toString('base64'),
      mimetype: 'image/jpeg',
    });
    expect(mockDownloadFile).toHaveBeenCalledWith('large');
  });

  it('returns null on download failure', async () => {
    mockDownloadFile.mockRejectedValueOnce(new Error('ECONNRESET'));

    const result = await extractPhotoData(makeCtx([{ file_id: 'big', width: 1280, height: 1280 }]));

    expect(result).toBeNull();
  });
});
