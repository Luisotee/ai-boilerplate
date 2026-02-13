export const ALLOWED_MIMETYPES = {
  image: ['image/jpeg', 'image/png', 'image/webp'],
  document: [
    'application/pdf',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-powerpoint',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'text/plain',
    'application/zip',
  ],
  audio: ['audio/mpeg', 'audio/ogg', 'audio/mp4', 'audio/aac'],
  video: ['video/mp4', 'video/3gpp'],
};

export const MAX_FILE_SIZES = {
  image: 16 * 1024 * 1024, // 16MB
  document: 50 * 1024 * 1024, // 50MB
  audio: 16 * 1024 * 1024, // 16MB
  video: 50 * 1024 * 1024, // 50MB
};

export interface FileValidationResult {
  valid: boolean;
  error?: string;
}

export function validateMediaFile(
  mimetype: string,
  size: number,
  mediaType: 'image' | 'document' | 'audio' | 'video'
): FileValidationResult {
  // Validate type
  if (!ALLOWED_MIMETYPES[mediaType].includes(mimetype)) {
    return {
      valid: false,
      error: `Invalid file type. Allowed types: ${ALLOWED_MIMETYPES[mediaType].join(', ')}`,
    };
  }

  // Validate size
  if (size > MAX_FILE_SIZES[mediaType]) {
    const maxMB = (MAX_FILE_SIZES[mediaType] / (1024 * 1024)).toFixed(0);
    return {
      valid: false,
      error: `File too large. Maximum size for ${mediaType}: ${maxMB}MB`,
    };
  }

  return { valid: true };
}
