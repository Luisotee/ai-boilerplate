/**
 * Unit tests for handlers/voice.ts.
 *
 * extractAndTranscribeVoice downloads the file and forwards it to the AI API
 * /transcribe endpoint, returning the text or null on failure. The
 * user-facing "try again" reply on null is emitted by updates.ts — tested
 * separately via integration.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

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

  it('returns null when no voice/audio is present', async () => {
    const result = await extractAndTranscribeVoice({ msg: {} } as never);
    expect(result).toBeNull();
    expect(mockDownloadFile).not.toHaveBeenCalled();
  });

  it('downloads voice message and returns transcription', async () => {
    mockDownloadFile.mockResolvedValueOnce({
      buffer: Buffer.from('OPUS'),
      filePath: 'voice/file_1.oga',
    });
    mockTranscribeAudio.mockResolvedValueOnce('hello world');

    const result = await extractAndTranscribeVoice(
      makeCtx({ file_id: 'VOICE', mime_type: 'audio/ogg' })
    );

    expect(result).toBe('hello world');
    expect(mockDownloadFile).toHaveBeenCalledWith('VOICE');
    expect(mockTranscribeAudio).toHaveBeenCalledWith(
      Buffer.from('OPUS'),
      'audio/ogg',
      expect.stringMatching(/^voice_\d+\.oga$/)
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

    expect(result).toBe('audio transcription');
    expect(mockDownloadFile).toHaveBeenCalledWith('AUDIO');
  });

  it('returns null when download fails', async () => {
    mockDownloadFile.mockRejectedValueOnce(new Error('network'));

    const result = await extractAndTranscribeVoice(
      makeCtx({ file_id: 'FAIL', mime_type: 'audio/ogg' })
    );

    expect(result).toBeNull();
    expect(mockTranscribeAudio).not.toHaveBeenCalled();
  });

  it('returns null when transcription returns null', async () => {
    mockDownloadFile.mockResolvedValueOnce({
      buffer: Buffer.from('OPUS'),
      filePath: 'voice/file.oga',
    });
    mockTranscribeAudio.mockResolvedValueOnce(null);

    const result = await extractAndTranscribeVoice(
      makeCtx({ file_id: 'VOICE', mime_type: 'audio/ogg' })
    );

    expect(result).toBeNull();
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
