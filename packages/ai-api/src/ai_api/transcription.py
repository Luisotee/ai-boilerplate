"""
Speech-to-Text transcription service.

Supports two backends:
- Groq's Whisper cloud API (primary when GROQ_API_KEY is set)
- Self-hosted Whisper via any OpenAI-compatible server exposing
  POST /v1/audio/transcriptions (e.g. speaches)

The public dispatcher `transcribe_audio_dispatcher` picks a backend based on
`settings.stt_provider` and falls back on recoverable errors when `auto`.
"""

from io import BytesIO
from typing import BinaryIO

import groq
import httpx
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

# Errors the auto-mode dispatcher treats as transient — they trigger fallback
# from Groq to self-hosted. Mirrors `_RECOVERABLE_PARSER_ERRORS` in processing.py.
#
# Deliberately excluded:
# - Programming errors (TypeError, AttributeError, ImportError) so SDK signature
#   drift surfaces as a real bug instead of silently rerouting.
# - groq.APIStatusError as a base — it also covers 4xx client errors (e.g.
#   BadRequestError for invalid audio), which would pointlessly cascade to the
#   self-hosted backend. Only its retryable subclasses are listed.
#
# Note: httpx.HTTPError covers httpx.TimeoutException and httpx.HTTPStatusError
# via the standard httpx exception hierarchy.
RECOVERABLE_STT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    ConnectionError,
    TimeoutError,
    groq.APIConnectionError,
    groq.APITimeoutError,
    groq.InternalServerError,
    groq.RateLimitError,
)


class SttNotConfiguredError(RuntimeError):
    """Raised when no STT provider is configured for the requested mode."""


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

    except RECOVERABLE_STT_ERRORS:
        # Let the dispatcher decide whether to fall back — don't swallow here.
        raise
    except Exception as e:
        error_msg = f"Transcription failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return None, error_msg


async def transcribe_audio_via_whisper(
    base_url: str,
    audio_bytes: bytes,
    filename: str,
    language: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Transcribe audio via a self-hosted OpenAI-compatible Whisper server.

    Posts multipart/form-data to `{base_url}/v1/audio/transcriptions`. Matches
    the OpenAI / Groq request+response shape — response JSON contains `text`.

    Args:
        base_url: Root URL of the self-hosted server (e.g. http://whisper:8000)
        audio_bytes: Raw audio content
        filename: Original filename, used for MIME inference server-side
        language: Optional ISO-639-1 language code

    Returns:
        Tuple of (transcription_text, error_message).

    Raises:
        `RECOVERABLE_STT_ERRORS`: surfaced to the dispatcher so explicit-mode
        callers see real errors and auto-mode can stop falling back further.
    """
    url = f"{base_url.rstrip('/')}/v1/audio/transcriptions"

    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "mp3"
    mime = (AUDIO_MIME_TYPES.get(extension) or ["application/octet-stream"])[0]

    files = {"file": (filename, audio_bytes, mime)}
    data: dict[str, str] = {
        "model": settings.whisper_model,
        "response_format": "json",
        "temperature": "0.0",
    }
    if language:
        data["language"] = language
        logger.debug(f"Transcribing via self-hosted Whisper with language hint: {language}")

    logger.info(
        f"Transcribing audio via self-hosted Whisper at {base_url} "
        f"(model: {settings.whisper_model}, size: {len(audio_bytes)} bytes)"
    )

    try:
        async with httpx.AsyncClient(timeout=settings.whisper_timeout_seconds) as client:
            resp = await client.post(url, files=files, data=data)
            resp.raise_for_status()
            payload = resp.json()
    except RECOVERABLE_STT_ERRORS:
        raise
    except Exception as e:
        error_msg = f"Self-hosted Whisper request failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return None, error_msg

    text = (payload.get("text") or "").strip()
    if not text:
        logger.warning("Self-hosted Whisper returned empty text")
        return (
            None,
            "Transcription produced no text (audio may be silent or unclear)",
        )

    logger.info(f"Self-hosted transcription successful ({len(text)} characters)")
    return text, None


async def transcribe_audio_dispatcher(
    audio_bytes: bytes,
    filename: str,
    language: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Select an STT backend per `settings.stt_provider` and transcribe.

    Decision tree:
      - "groq":    require GROQ_API_KEY; call Groq only (no fallback).
      - "whisper": require WHISPER_BASE_URL; call self-hosted only (no fallback).
      - "auto":    try Groq if key is set; on `RECOVERABLE_STT_ERRORS` fall
                   back to self-hosted if `WHISPER_BASE_URL` is set. Otherwise
                   use self-hosted directly. Raises SttNotConfiguredError if
                   neither is available.

    Returns `(text, error_message)` — same contract as `transcribe_audio`.

    Raises:
        SttNotConfiguredError: no usable provider is configured for the mode.
    """
    choice = settings.stt_provider
    has_groq = bool(settings.groq_api_key)
    has_whisper = bool(settings.whisper_base_url)

    async def _via_groq() -> tuple[str | None, str | None]:
        client = create_groq_client(settings.groq_api_key)
        if client is None:
            # Factory logged the reason. Treat as a hard config failure so
            # auto-mode doesn't loop trying to re-create a client that can't
            # exist.
            raise SttNotConfiguredError(
                "Groq client could not be initialized (check GROQ_API_KEY)."
            )
        return await transcribe_audio(client, BytesIO(audio_bytes), filename, language=language)

    async def _via_whisper() -> tuple[str | None, str | None]:
        if settings.whisper_base_url is None:
            # Should be unreachable — callers must check has_whisper first.
            raise SttNotConfiguredError(
                "WHISPER_BASE_URL is not set; cannot call self-hosted Whisper."
            )
        return await transcribe_audio_via_whisper(
            settings.whisper_base_url, audio_bytes, filename, language=language
        )

    if choice == "groq":
        if not has_groq:
            raise SttNotConfiguredError("STT_PROVIDER=groq but GROQ_API_KEY is not set.")
        return await _via_groq()

    if choice == "whisper":
        if not has_whisper:
            raise SttNotConfiguredError("STT_PROVIDER=whisper but WHISPER_BASE_URL is not set.")
        return await _via_whisper()

    # auto
    if has_groq:
        try:
            return await _via_groq()
        except RECOVERABLE_STT_ERRORS:
            if has_whisper:
                logger.warning(
                    "Groq STT failed; falling back to self-hosted whisper.", exc_info=True
                )
                return await _via_whisper()
            logger.error("Groq STT failed and WHISPER_BASE_URL is not set.", exc_info=True)
            raise

    if has_whisper:
        return await _via_whisper()

    raise SttNotConfiguredError("No STT provider configured. Set GROQ_API_KEY or WHISPER_BASE_URL.")
