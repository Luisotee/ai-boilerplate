"""
Speech-to-Text transcription service using Groq's Whisper API.

Follows functional programming style with pure functions for:
- File validation (format, size, mimetype)
- Groq API client creation
- Transcription execution
- Error handling

Pattern mirrors embeddings.py for consistency.
"""

from typing import BinaryIO

from groq import Groq

from .config import settings
from .logger import logger

# Derived constants from settings
MAX_FILE_SIZE_BYTES = settings.stt_max_file_size_mb * 1024 * 1024
SUPPORTED_FORMATS = settings.stt_supported_formats.split(",")

# MIME type mappings for validation
AUDIO_MIME_TYPES = {
    "mp3": ["audio/mpeg", "audio/mp3"],
    "mp4": ["audio/mp4", "audio/x-m4a"],
    "mpeg": ["audio/mpeg"],
    "mpga": ["audio/mpeg"],
    "m4a": ["audio/mp4", "audio/x-m4a", "audio/m4a"],
    "wav": ["audio/wav", "audio/x-wav", "audio/wave"],
    "webm": ["audio/webm"],
    "ogg": ["audio/ogg", "audio/opus"],
    "flac": ["audio/flac", "audio/x-flac"],
}


def validate_audio_file(
    filename: str, content_type: str | None, file_size: int
) -> tuple[bool, str | None, str | None]:
    """
    Validate audio file format, size, and MIME type.

    Pure function with no side effects.

    Args:
        filename: Original filename (e.g., "recording.mp3")
        content_type: MIME type from upload (e.g., "audio/mpeg")
        file_size: File size in bytes

    Returns:
        Tuple of (is_valid, error_message, file_format)
        - is_valid: True if file passes all validation
        - error_message: Human-readable error if invalid, None otherwise
        - file_format: Detected format extension (e.g., "mp3"), None if invalid
    """
    # Check file size
    if file_size == 0:
        return False, "Audio file is empty", None

    if file_size > MAX_FILE_SIZE_BYTES:
        size_mb = file_size / (1024 * 1024)
        return (
            False,
            f"File too large ({size_mb:.1f} MB). Maximum: {settings.stt_max_file_size_mb} MB",
            None,
        )

    # Extract format from filename
    file_format = None
    if "." in filename:
        extension = filename.rsplit(".", 1)[1].lower()
        if extension in SUPPORTED_FORMATS:
            file_format = extension

    # Validate format
    if not file_format:
        return (
            False,
            f"Unsupported or missing file extension. Supported: {', '.join(SUPPORTED_FORMATS)}",
            None,
        )

    # Validate MIME type if provided
    if content_type:
        # Normalize MIME type (remove parameters like "; codecs=opus")
        normalized_mime = content_type.split(";")[0].strip().lower()

        # Check if MIME type matches the file extension
        expected_mimes = AUDIO_MIME_TYPES.get(file_format, [])
        if normalized_mime not in expected_mimes:
            logger.warning(
                f"MIME type mismatch: file '{filename}' has type '{normalized_mime}', "
                f"expected one of {expected_mimes}. Proceeding anyway."
            )

    logger.debug(f"Audio file validated: {filename} ({file_size} bytes, format: {file_format})")
    return True, None, file_format


def create_groq_client(api_key: str | None) -> Groq | None:
    """
    Create Groq client from API key.

    Factory function for initializing the service with proper error handling.
    Follows the same pattern as create_embedding_service() from embeddings.py.

    Args:
        api_key: Groq API key

    Returns:
        Groq client instance or None if API key not provided
    """
    if not api_key:
        logger.warning("GROQ_API_KEY not set - speech-to-text will be disabled")
        return None

    try:
        client = Groq(api_key=api_key)
        logger.info(f"Groq client initialized (model: {settings.stt_model})")
        return client
    except Exception as e:
        logger.error(f"Failed to create Groq client: {str(e)}", exc_info=True)
        return None


async def transcribe_audio(
    client: Groq, audio_file: BinaryIO, filename: str, language: str | None = None
) -> tuple[str | None, str | None]:
    """
    Transcribe audio file using Groq's Whisper API.

    Pure async function that calls external API and returns results.

    Args:
        client: Authenticated Groq client
        audio_file: Audio file buffer (BinaryIO)
        filename: Original filename (used for format detection)
        language: Optional ISO-639-1 language code (e.g., 'en', 'es')
                 Providing language improves accuracy and latency

    Returns:
        Tuple of (transcription_text, error_message)
        - transcription_text: Transcribed text if successful, None otherwise
        - error_message: Human-readable error if failed, None otherwise
    """
    try:
        # Prepare transcription request
        # Read file content into memory for API call
        audio_content = audio_file.read()

        # Build parameters
        params = {
            "file": (filename, audio_content),
            "model": settings.stt_model,
            "response_format": "json",  # Simple JSON with just text
            "temperature": 0.0,  # Deterministic output
        }

        # Add language if provided (improves accuracy)
        if language:
            params["language"] = language
            logger.debug(f"Transcribing with language hint: {language}")

        # Call Groq Whisper API
        logger.info(
            f"Transcribing audio with {settings.stt_model} (size: {len(audio_content)} bytes)"
        )
        transcription = client.audio.transcriptions.create(**params)

        # Extract text from response
        transcription_text = transcription.text.strip()

        if not transcription_text:
            logger.warning("Transcription returned empty text")
            return (
                None,
                "Transcription produced no text (audio may be silent or unclear)",
            )

        logger.info(f"Transcription successful ({len(transcription_text)} characters)")
        return transcription_text, None

    except Exception as e:
        error_msg = f"Transcription failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return None, error_msg
