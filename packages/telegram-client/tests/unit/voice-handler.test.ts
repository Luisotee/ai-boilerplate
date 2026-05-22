/**
 * Unit tests for handlers/voice.ts.
 *
 * extractAndTranscribeVoice returns a discriminated VoiceExtraction:
 *   - ok: download + transcription succeeded
 *   - too-large: GrammyError 400 "file is too big" at download
 *   - download-error: any other download failure
 *   - transcription-failed: download ok, transcription returned null or threw
 *   - no-voice: no voice/audio in the message
 *
 * The user-facing replies for each non-ok kind are emitted by updates.ts and
 * tested separately via integration.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { GrammyError } from 'grammy';

vi.mock('../../src/services/telegram-api.js', () => ({
  downloadFile: vi.fn(),
}));
vi.mock('../../src/api-client.js', () => ({
  transcribeAudio: vi.fn(),
}));

import { extractAndTranscribeVoice } from '../../src/handlers/voice.js';
import * as telegramApi from '../../src/services/telegram-api.js';
import * as apiClient from '../../src/api-client.js';

const mockDownloadFile = telegramApi.downloadFile as ReturnType<typeof vi.fn>;
const mockTranscribeAudio = apiClient.transcribeAudio as ReturnType<typeof vi.fn>;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function makeCtx(voice: any, audio?: any): any {
  return { msg: { voice, audio } };
}

describe('extractAndTranscribeVoice', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns { kind: "no-voice" } when no voice/audio is present', async () => {
    const result = await extractAndTranscribeVoice({ msg: {} } as never);
    expect(result).toEqual({ kind: 'no-voice' });
    expect(mockDownloadFile).not.toHaveBeenCalled();
  });

  it('downloads voice message and returns { kind: "ok" } with transcription', async () => {
    mockDownloadFile.mockResolvedValueOnce({
      buffer: Buffer.from('OPUS'),
      filePath: 'voice/file_1.oga',
    });
    mockTranscribeAudio.mockResolvedValueOnce('hello world');

    const result = await extractAndTranscribeVoice(
      makeCtx({ file_id: 'VOICE', mime_type: 'audio/ogg' })
    );

    expect(result).toEqual({ kind: 'ok', transcription: 'hello world' });
    expect(mockDownloadFile).toHaveBeenCalledWith('VOICE');
    expect(mockTranscribeAudio).toHaveBeenCalledWith(
      Buffer.from('OPUS'),
      'audio/ogg',
      expect.stringMatching(/^voice_\d+\.ogg$/)
    );
  });

  it('falls back to audio field when voice is absent', async () => {
    mockDownloadFile.mockResolvedValueOnce({
      buffer: Buffer.from('MP3'),
      filePath: 'audio/file_2.mp3',
    });
    mockTranscribeAudio.mockResolvedValueOnce('audio transcription');

    const result = await extractAndTranscribeVoice(
      makeCtx(undefined, { file_id: 'AUDIO', mime_type: 'audio/mpeg' })
    );

    expect(result).toEqual({ kind: 'ok', transcription: 'audio transcription' });
    expect(mockDownloadFile).toHaveBeenCalledWith('AUDIO');
  });

  it('returns { kind: "too-large" } on GrammyError 400 "file is too big"', async () => {
    const err = new GrammyError(
      'Telegram server error',
      { ok: false, error_code: 400, description: 'Bad Request: file is too big' },
      'getFile',
      {}
    );
    mockDownloadFile.mockRejectedValueOnce(err);

    const result = await extractAndTranscribeVoice(
      makeCtx({ file_id: 'LARGE', mime_type: 'audio/ogg' })
    );

    expect(result).toEqual({ kind: 'too-large' });
    expect(mockTranscribeAudio).not.toHaveBeenCalled();
  });

  it('returns { kind: "download-error" } on other GrammyError codes', async () => {
    const err = new GrammyError(
      'Telegram server error',
      { ok: false, error_code: 500, description: 'Internal Server Error' },
      'getFile',
      {}
    );
    mockDownloadFile.mockRejectedValueOnce(err);

    const result = await extractAndTranscribeVoice(
      makeCtx({ file_id: 'FAIL', mime_type: 'audio/ogg' })
    );

    expect(result).toEqual({ kind: 'download-error' });
  });

  it('returns { kind: "download-error" } on network errors', async () => {
    mockDownloadFile.mockRejectedValueOnce(new Error('ECONNRESET'));

    const result = await extractAndTranscribeVoice(
      makeCtx({ file_id: 'FAIL', mime_type: 'audio/ogg' })
    );

    expect(result).toEqual({ kind: 'download-error' });
    expect(mockTranscribeAudio).not.toHaveBeenCalled();
  });

  it('returns { kind: "transcription-failed" } when transcription returns null', async () => {
    mockDownloadFile.mockResolvedValueOnce({
      buffer: Buffer.from('OPUS'),
      filePath: 'voice/file.oga',
    });
    mockTranscribeAudio.mockResolvedValueOnce(null);

    const result = await extractAndTranscribeVoice(
      makeCtx({ file_id: 'VOICE', mime_type: 'audio/ogg' })
    );

    expect(result).toEqual({ kind: 'transcription-failed' });
  });

  it('returns { kind: "transcription-failed" } when transcribe throws', async () => {
    mockDownloadFile.mockResolvedValueOnce({
      buffer: Buffer.from('OPUS'),
      filePath: 'voice/file.oga',
    });
    mockTranscribeAudio.mockRejectedValueOnce(new Error('AI API down'));

    const result = await extractAndTranscribeVoice(
      makeCtx({ file_id: 'VOICE', mime_type: 'audio/ogg' })
    );

    expect(result).toEqual({ kind: 'transcription-failed' });
  });

  it('defaults mimetype to audio/ogg when not provided', async () => {
    mockDownloadFile.mockResolvedValueOnce({
      buffer: Buffer.from('x'),
      filePath: 'voice/f.oga',
    });
    mockTranscribeAudio.mockResolvedValueOnce('ok');

    await extractAndTranscribeVoice(makeCtx({ file_id: 'NOMIME' }));

    expect(mockTranscribeAudio).toHaveBeenCalledWith(
      expect.any(Buffer),
      'audio/ogg',
      expect.any(String)
    );
  });
});
