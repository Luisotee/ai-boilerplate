import { describe, it, expect } from 'vitest';
import {
  validateMediaFile,
  ALLOWED_MIMETYPES,
  MAX_FILE_SIZES,
} from '../../src/utils/file-validation.js';

// ---------------------------------------------------------------------------
// Constants verification
// ---------------------------------------------------------------------------

describe('ALLOWED_MIMETYPES', () => {
  it('should include standard image types', () => {
    expect(ALLOWED_MIMETYPES.image).toContain('image/jpeg');
    expect(ALLOWED_MIMETYPES.image).toContain('image/png');
    expect(ALLOWED_MIMETYPES.image).toContain('image/webp');
  });

  it('should include standard audio types', () => {
    expect(ALLOWED_MIMETYPES.audio).toContain('audio/mpeg');
    expect(ALLOWED_MIMETYPES.audio).toContain('audio/ogg');
    expect(ALLOWED_MIMETYPES.audio).toContain('audio/mp4');
    expect(ALLOWED_MIMETYPES.audio).toContain('audio/aac');
  });

  it('should include standard document types', () => {
    expect(ALLOWED_MIMETYPES.document).toContain('application/pdf');
    expect(ALLOWED_MIMETYPES.document).toContain('text/plain');
    expect(ALLOWED_MIMETYPES.document).toContain('application/zip');
  });

  it('should include standard video types', () => {
    expect(ALLOWED_MIMETYPES.video).toContain('video/mp4');
    expect(ALLOWED_MIMETYPES.video).toContain('video/3gpp');
  });
});

describe('MAX_FILE_SIZES', () => {
  it('should set image max size to 16MB', () => {
    expect(MAX_FILE_SIZES.image).toBe(16 * 1024 * 1024);
  });

  it('should set document max size to 50MB', () => {
    expect(MAX_FILE_SIZES.document).toBe(50 * 1024 * 1024);
  });

  it('should set audio max size to 16MB', () => {
    expect(MAX_FILE_SIZES.audio).toBe(16 * 1024 * 1024);
  });

  it('should set video max size to 50MB', () => {
    expect(MAX_FILE_SIZES.video).toBe(50 * 1024 * 1024);
  });
});

// ---------------------------------------------------------------------------
// validateMediaFile
// ---------------------------------------------------------------------------

describe('validateMediaFile', () => {
  // ---- Image ----

  describe('image validation', () => {
    it('should accept valid JPEG image within size limit', () => {
      const result = validateMediaFile('image/jpeg', 1024 * 1024, 'image');
      expect(result.valid).toBe(true);
      expect(result.error).toBeUndefined();
    });

    it('should accept valid PNG image', () => {
      const result = validateMediaFile('image/png', 5 * 1024 * 1024, 'image');
      expect(result.valid).toBe(true);
    });

    it('should accept valid WebP image', () => {
      const result = validateMediaFile('image/webp', 1024, 'image');
      expect(result.valid).toBe(true);
    });

    it('should reject invalid image mimetype', () => {
      const result = validateMediaFile('image/gif', 1024, 'image');
      expect(result.valid).toBe(false);
      expect(result.error).toContain('Invalid file type');
      expect(result.error).toContain('image/jpeg');
    });

    it('should reject image exceeding 16MB', () => {
      const result = validateMediaFile('image/jpeg', 17 * 1024 * 1024, 'image');
      expect(result.valid).toBe(false);
      expect(result.error).toContain('File too large');
      expect(result.error).toContain('16MB');
    });

    it('should accept image at exactly 16MB', () => {
      const result = validateMediaFile('image/jpeg', 16 * 1024 * 1024, 'image');
      expect(result.valid).toBe(true);
    });

    it('should reject image at one byte over 16MB', () => {
      const result = validateMediaFile('image/jpeg', 16 * 1024 * 1024 + 1, 'image');
      expect(result.valid).toBe(false);
      expect(result.error).toContain('File too large');
    });
  });

  // ---- Document ----

  describe('document validation', () => {
    it('should accept valid PDF', () => {
      const result = validateMediaFile('application/pdf', 10 * 1024 * 1024, 'document');
      expect(result.valid).toBe(true);
    });

    it('should accept plain text file', () => {
      const result = validateMediaFile('text/plain', 1024, 'document');
      expect(result.valid).toBe(true);
    });

    it('should accept MS Word document', () => {
      const result = validateMediaFile('application/msword', 5 * 1024 * 1024, 'document');
      expect(result.valid).toBe(true);
    });

    it('should accept DOCX (OpenXML)', () => {
      const result = validateMediaFile(
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        5 * 1024 * 1024,
        'document'
      );
      expect(result.valid).toBe(true);
    });

    it('should accept Excel spreadsheet', () => {
      const result = validateMediaFile('application/vnd.ms-excel', 5 * 1024 * 1024, 'document');
      expect(result.valid).toBe(true);
    });

    it('should accept XLSX (OpenXML)', () => {
      const result = validateMediaFile(
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        5 * 1024 * 1024,
        'document'
      );
      expect(result.valid).toBe(true);
    });

    it('should accept PowerPoint presentation', () => {
      const result = validateMediaFile(
        'application/vnd.ms-powerpoint',
        5 * 1024 * 1024,
        'document'
      );
      expect(result.valid).toBe(true);
    });

    it('should accept PPTX (OpenXML)', () => {
      const result = validateMediaFile(
        'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        5 * 1024 * 1024,
        'document'
      );
      expect(result.valid).toBe(true);
    });

    it('should accept ZIP archive', () => {
      const result = validateMediaFile('application/zip', 30 * 1024 * 1024, 'document');
      expect(result.valid).toBe(true);
    });

    it('should reject invalid document mimetype', () => {
      const result = validateMediaFile('application/json', 1024, 'document');
      expect(result.valid).toBe(false);
      expect(result.error).toContain('Invalid file type');
    });

    it('should reject document exceeding 50MB', () => {
      const result = validateMediaFile('application/pdf', 51 * 1024 * 1024, 'document');
      expect(result.valid).toBe(false);
      expect(result.error).toContain('File too large');
      expect(result.error).toContain('50MB');
    });
  });

  // ---- Audio ----

  describe('audio validation', () => {
    it('should accept valid MP3 audio', () => {
      const result = validateMediaFile('audio/mpeg', 5 * 1024 * 1024, 'audio');
      expect(result.valid).toBe(true);
    });

    it('should accept OGG audio', () => {
      const result = validateMediaFile('audio/ogg', 1024 * 1024, 'audio');
      expect(result.valid).toBe(true);
    });

    it('should accept MP4 audio', () => {
      const result = validateMediaFile('audio/mp4', 1024 * 1024, 'audio');
      expect(result.valid).toBe(true);
    });

    it('should accept AAC audio', () => {
      const result = validateMediaFile('audio/aac', 1024 * 1024, 'audio');
      expect(result.valid).toBe(true);
    });

    it('should reject invalid audio mimetype', () => {
      const result = validateMediaFile('audio/wav', 1024 * 1024, 'audio');
      expect(result.valid).toBe(false);
      expect(result.error).toContain('Invalid file type');
    });

    it('should reject audio exceeding 16MB', () => {
      const result = validateMediaFile('audio/mpeg', 17 * 1024 * 1024, 'audio');
      expect(result.valid).toBe(false);
      expect(result.error).toContain('File too large');
      expect(result.error).toContain('16MB');
    });
  });

  // ---- Video ----

  describe('video validation', () => {
    it('should accept valid MP4 video', () => {
      const result = validateMediaFile('video/mp4', 30 * 1024 * 1024, 'video');
      expect(result.valid).toBe(true);
    });

    it('should accept 3GPP video', () => {
      const result = validateMediaFile('video/3gpp', 10 * 1024 * 1024, 'video');
      expect(result.valid).toBe(true);
    });

    it('should reject invalid video mimetype', () => {
      const result = validateMediaFile('video/webm', 1024, 'video');
      expect(result.valid).toBe(false);
      expect(result.error).toContain('Invalid file type');
    });

    it('should reject video exceeding 50MB', () => {
      const result = validateMediaFile('video/mp4', 51 * 1024 * 1024, 'video');
      expect(result.valid).toBe(false);
      expect(result.error).toContain('File too large');
      expect(result.error).toContain('50MB');
    });
  });

  // ---- Edge cases ----

  describe('edge cases', () => {
    it('should accept zero-byte file with valid mimetype', () => {
      const result = validateMediaFile('image/jpeg', 0, 'image');
      expect(result.valid).toBe(true);
    });

    it('should reject wrong media category for mimetype', () => {
      // Valid mimetype but wrong category
      const result = validateMediaFile('image/jpeg', 1024, 'audio');
      expect(result.valid).toBe(false);
      expect(result.error).toContain('Invalid file type');
    });

    it('should check mimetype before size', () => {
      // Both invalid mimetype AND too large -- error message should be about type
      const result = validateMediaFile('image/gif', 100 * 1024 * 1024, 'image');
      expect(result.valid).toBe(false);
      expect(result.error).toContain('Invalid file type');
    });
  });
});
