import { describe, it, expect } from 'vitest';
import {
  validateMediaFile,
  ALLOWED_MIMETYPES,
  MAX_FILE_SIZES,
} from '../../src/utils/file-validation.js';

describe('ALLOWED_MIMETYPES', () => {
  it('should define allowed image types', () => {
    expect(ALLOWED_MIMETYPES.image).toEqual(['image/jpeg', 'image/png', 'image/webp']);
  });

  it('should define allowed document types', () => {
    expect(ALLOWED_MIMETYPES.document).toContain('application/pdf');
    expect(ALLOWED_MIMETYPES.document).toContain('application/msword');
    expect(ALLOWED_MIMETYPES.document).toContain(
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    );
    expect(ALLOWED_MIMETYPES.document).toContain('application/vnd.ms-excel');
    expect(ALLOWED_MIMETYPES.document).toContain(
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    );
    expect(ALLOWED_MIMETYPES.document).toContain('application/vnd.ms-powerpoint');
    expect(ALLOWED_MIMETYPES.document).toContain(
      'application/vnd.openxmlformats-officedocument.presentationml.presentation'
    );
    expect(ALLOWED_MIMETYPES.document).toContain('text/plain');
    expect(ALLOWED_MIMETYPES.document).toContain('application/zip');
  });

  it('should define allowed audio types', () => {
    expect(ALLOWED_MIMETYPES.audio).toEqual(['audio/mpeg', 'audio/ogg', 'audio/mp4', 'audio/aac']);
  });

  it('should define allowed video types', () => {
    expect(ALLOWED_MIMETYPES.video).toEqual(['video/mp4', 'video/3gpp']);
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

describe('validateMediaFile', () => {
  describe('image validation', () => {
    it('should accept valid JPEG image within size limit', () => {
      const result = validateMediaFile('image/jpeg', 1024 * 1024, 'image');
      expect(result).toEqual({ valid: true });
    });

    it('should accept valid PNG image', () => {
      const result = validateMediaFile('image/png', 1024, 'image');
      expect(result).toEqual({ valid: true });
    });

    it('should accept valid WebP image', () => {
      const result = validateMediaFile('image/webp', 500 * 1024, 'image');
      expect(result).toEqual({ valid: true });
    });

    it('should reject image with invalid mimetype', () => {
      const result = validateMediaFile('image/gif', 1024, 'image');

      expect(result.valid).toBe(false);
      expect(result.error).toContain('Invalid file type');
      expect(result.error).toContain('image/jpeg');
      expect(result.error).toContain('image/png');
      expect(result.error).toContain('image/webp');
    });

    it('should reject image exceeding 16MB', () => {
      const size = 16 * 1024 * 1024 + 1; // 16MB + 1 byte
      const result = validateMediaFile('image/jpeg', size, 'image');

      expect(result.valid).toBe(false);
      expect(result.error).toContain('File too large');
      expect(result.error).toContain('16MB');
    });

    it('should accept image at exactly 16MB', () => {
      const size = 16 * 1024 * 1024; // exactly 16MB
      const result = validateMediaFile('image/jpeg', size, 'image');

      expect(result).toEqual({ valid: true });
    });

    it('should accept zero-size image file', () => {
      const result = validateMediaFile('image/jpeg', 0, 'image');
      expect(result).toEqual({ valid: true });
    });
  });

  describe('document validation', () => {
    it('should accept valid PDF document', () => {
      const result = validateMediaFile('application/pdf', 5 * 1024 * 1024, 'document');
      expect(result).toEqual({ valid: true });
    });

    it('should accept valid Word document', () => {
      const result = validateMediaFile('application/msword', 1024 * 1024, 'document');
      expect(result).toEqual({ valid: true });
    });

    it('should accept valid DOCX document', () => {
      const result = validateMediaFile(
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        2 * 1024 * 1024,
        'document'
      );
      expect(result).toEqual({ valid: true });
    });

    it('should accept valid Excel document', () => {
      const result = validateMediaFile('application/vnd.ms-excel', 1024 * 1024, 'document');
      expect(result).toEqual({ valid: true });
    });

    it('should accept valid XLSX document', () => {
      const result = validateMediaFile(
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        1024 * 1024,
        'document'
      );
      expect(result).toEqual({ valid: true });
    });

    it('should accept valid PowerPoint document', () => {
      const result = validateMediaFile('application/vnd.ms-powerpoint', 1024 * 1024, 'document');
      expect(result).toEqual({ valid: true });
    });

    it('should accept valid PPTX document', () => {
      const result = validateMediaFile(
        'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        1024 * 1024,
        'document'
      );
      expect(result).toEqual({ valid: true });
    });

    it('should accept valid plain text document', () => {
      const result = validateMediaFile('text/plain', 1024, 'document');
      expect(result).toEqual({ valid: true });
    });

    it('should accept valid ZIP file', () => {
      const result = validateMediaFile('application/zip', 10 * 1024 * 1024, 'document');
      expect(result).toEqual({ valid: true });
    });

    it('should reject unsupported document type', () => {
      const result = validateMediaFile('application/json', 1024, 'document');

      expect(result.valid).toBe(false);
      expect(result.error).toContain('Invalid file type');
    });

    it('should reject document exceeding 50MB', () => {
      const size = 50 * 1024 * 1024 + 1;
      const result = validateMediaFile('application/pdf', size, 'document');

      expect(result.valid).toBe(false);
      expect(result.error).toContain('File too large');
      expect(result.error).toContain('50MB');
    });

    it('should accept document at exactly 50MB', () => {
      const size = 50 * 1024 * 1024;
      const result = validateMediaFile('application/pdf', size, 'document');

      expect(result).toEqual({ valid: true });
    });
  });

  describe('audio validation', () => {
    it('should accept valid MP3 audio', () => {
      const result = validateMediaFile('audio/mpeg', 5 * 1024 * 1024, 'audio');
      expect(result).toEqual({ valid: true });
    });

    it('should accept valid OGG audio', () => {
      const result = validateMediaFile('audio/ogg', 1024 * 1024, 'audio');
      expect(result).toEqual({ valid: true });
    });

    it('should accept valid MP4 audio', () => {
      const result = validateMediaFile('audio/mp4', 1024 * 1024, 'audio');
      expect(result).toEqual({ valid: true });
    });

    it('should accept valid AAC audio', () => {
      const result = validateMediaFile('audio/aac', 1024 * 1024, 'audio');
      expect(result).toEqual({ valid: true });
    });

    it('should reject unsupported audio type', () => {
      const result = validateMediaFile('audio/wav', 1024, 'audio');

      expect(result.valid).toBe(false);
      expect(result.error).toContain('Invalid file type');
    });

    it('should reject audio exceeding 16MB', () => {
      const size = 16 * 1024 * 1024 + 1;
      const result = validateMediaFile('audio/mpeg', size, 'audio');

      expect(result.valid).toBe(false);
      expect(result.error).toContain('File too large');
      expect(result.error).toContain('16MB');
    });
  });

  describe('video validation', () => {
    it('should accept valid MP4 video', () => {
      const result = validateMediaFile('video/mp4', 20 * 1024 * 1024, 'video');
      expect(result).toEqual({ valid: true });
    });

    it('should accept valid 3GPP video', () => {
      const result = validateMediaFile('video/3gpp', 5 * 1024 * 1024, 'video');
      expect(result).toEqual({ valid: true });
    });

    it('should reject unsupported video type', () => {
      const result = validateMediaFile('video/webm', 1024, 'video');

      expect(result.valid).toBe(false);
      expect(result.error).toContain('Invalid file type');
    });

    it('should reject video exceeding 50MB', () => {
      const size = 50 * 1024 * 1024 + 1;
      const result = validateMediaFile('video/mp4', size, 'video');

      expect(result.valid).toBe(false);
      expect(result.error).toContain('File too large');
      expect(result.error).toContain('50MB');
    });
  });

  describe('error messages', () => {
    it('should list all allowed types in mimetype error', () => {
      const result = validateMediaFile('application/octet-stream', 1024, 'image');

      expect(result.valid).toBe(false);
      expect(result.error).toBe(
        'Invalid file type. Allowed types: image/jpeg, image/png, image/webp'
      );
    });

    it('should include media type name in size error', () => {
      const result = validateMediaFile('video/mp4', 100 * 1024 * 1024, 'video');

      expect(result.valid).toBe(false);
      expect(result.error).toBe('File too large. Maximum size for video: 50MB');
    });

    it('should check mimetype before size', () => {
      // Both invalid mimetype and too large size
      const result = validateMediaFile('image/gif', 100 * 1024 * 1024, 'image');

      // Should return mimetype error, not size error
      expect(result.valid).toBe(false);
      expect(result.error).toContain('Invalid file type');
    });
  });
});
