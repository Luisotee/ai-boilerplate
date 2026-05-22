/**
 * Unit tests for handlers/document.ts.
 *
 * Verifies the discriminated DocExtraction result:
 *   - wrong-type: non-PDF mimetype rejected without download
 *   - too-large: GrammyError 400 "file is too big" → { kind: 'too-large' }
 *   - download-error: any other download failure
 *   - ok: PDF downloaded successfully, base64-encoded
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { GrammyError } from 'grammy';

vi.mock('../../src/services/telegram-api.js', () => ({
  downloadFile: vi.fn(),
}));

import { extractDocumentData } from '../../src/handlers/document.js';
import * as telegramApi from '../../src/services/telegram-api.js';

const mockDownloadFile = telegramApi.downloadFile as ReturnType<typeof vi.fn>;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function makeCtx(doc: any): any {
  return { msg: { document: doc } };
}

describe('extractDocumentData', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns { kind: "download-error" } when no document is present', async () => {
    const result = await extractDocumentData({ msg: {} } as never);
    expect(result).toEqual({ kind: 'download-error' });
    expect(mockDownloadFile).not.toHaveBeenCalled();
  });

  it('returns { kind: "wrong-type" } for non-PDF mimetype without downloading', async () => {
    const result = await extractDocumentData(
      makeCtx({
        file_id: 'FID',
        mime_type: 'application/msword',
        file_name: 'doc.docx',
      })
    );

    expect(result).toMatchObject({ kind: 'wrong-type', mimetype: 'application/msword' });
    expect(mockDownloadFile).not.toHaveBeenCalled();
  });

  it('returns { kind: "too-large" } on GrammyError 400 "file is too big"', async () => {
    const err = new GrammyError(
      'Telegram server error',
      { ok: false, error_code: 400, description: 'Bad Request: file is too big' },
      'getFile',
      {}
    );
    mockDownloadFile.mockRejectedValueOnce(err);

    const result = await extractDocumentData(
      makeCtx({
        file_id: 'LARGE',
        mime_type: 'application/pdf',
        file_name: 'huge.pdf',
      })
    );

    expect(result).toEqual({ kind: 'too-large' });
  });

  it('returns { kind: "download-error" } on other GrammyError codes', async () => {
    const err = new GrammyError(
      'Telegram server error',
      { ok: false, error_code: 500, description: 'Internal Server Error' },
      'getFile',
      {}
    );
    mockDownloadFile.mockRejectedValueOnce(err);

    const result = await extractDocumentData(
      makeCtx({
        file_id: 'FAIL',
        mime_type: 'application/pdf',
        file_name: 'doc.pdf',
      })
    );

    expect(result).toEqual({ kind: 'download-error' });
  });

  it('returns { kind: "download-error" } on network errors', async () => {
    mockDownloadFile.mockRejectedValueOnce(new Error('ECONNRESET'));

    const result = await extractDocumentData(
      makeCtx({
        file_id: 'FAIL',
        mime_type: 'application/pdf',
        file_name: 'doc.pdf',
      })
    );

    expect(result).toEqual({ kind: 'download-error' });
  });

  it('returns { kind: "ok" } with base64 data on success', async () => {
    const buffer = Buffer.from('PDF-CONTENT');
    mockDownloadFile.mockResolvedValueOnce({ buffer, filePath: 'documents/file_0.pdf' });

    const result = await extractDocumentData(
      makeCtx({
        file_id: 'OK',
        mime_type: 'application/pdf',
        file_name: 'report.pdf',
      })
    );

    expect(result).toEqual({
      kind: 'ok',
      data: buffer.toString('base64'),
      mimetype: 'application/pdf',
      filename: 'report.pdf',
    });
  });

  it('falls back to defaults when mime_type / file_name are missing', async () => {
    // No mime_type → defaults to application/octet-stream → wrong-type.
    const result = await extractDocumentData(makeCtx({ file_id: 'FID' }));
    expect(result).toMatchObject({
      kind: 'wrong-type',
      mimetype: 'application/octet-stream',
    });
  });
});
