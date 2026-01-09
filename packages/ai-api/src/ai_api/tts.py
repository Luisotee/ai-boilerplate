"""
Text-to-Speech synthesis service using Gemini's TTS API.

Follows functional programming style with pure functions for:
- Text validation (length, content)
- Gemini API client creation
- Speech synthesis execution
- PCM to OGG/Opus conversion (for WhatsApp voice notes)

Pattern mirrors transcription.py for consistency.
"""

import io

from google import genai
from google.genai import types
from pydub import AudioSegment

from .config import settings
from .logger import logger

# Voice mappings by language code
TTS_VOICES = {
    "en": "Kore",  # English
    "es": "Aoede",  # Spanish
    "pt": "Puck",  # Portuguese
    "fr": "Charon",  # French
    "de": "Fenrir",  # German
}


def get_voice_for_language(language: str) -> str:
    """
    Get the appropriate TTS voice for a language code.

    Args:
        language: ISO-639-1 language code (e.g., 'en', 'es')

    Returns:
        Voice name for Gemini TTS
    """
    return TTS_VOICES.get(language, settings.tts_default_voice)


def validate_text_input(text: str) -> tuple[bool, str | None]:
    """
    Validate text input for TTS synthesis.

    Pure function with no side effects.

    Args:
        text: Text to convert to speech

    Returns:
        Tuple of (is_valid, error_message)
        - is_valid: True if text passes validation
        - error_message: Human-readable error if invalid, None otherwise
    """
    if not text:
        return False, "Text is empty"

    if not text.strip():
        return False, "Text contains only whitespace"

    if len(text) > settings.tts_max_text_length:
        return (
            False,
            f"Text too long ({len(text)} chars). Maximum: {settings.tts_max_text_length} chars",
        )

    logger.debug(f"Text validated for TTS: {len(text)} characters")
    return True, None


def create_genai_client(api_key: str | None) -> genai.Client | None:
    """
    Create Gemini client from API key.

    Factory function for initializing the service with proper error handling.
    Follows the same pattern as create_groq_client() from transcription.py.

    Args:
        api_key: Gemini API key

    Returns:
        Gemini client instance or None if API key not provided
    """
    if not api_key:
        logger.warning("GEMINI_API_KEY not set - text-to-speech will be disabled")
        return None

    try:
        client = genai.Client(api_key=api_key)
        logger.info(f"Gemini TTS client initialized (model: {settings.tts_model})")
        return client
    except Exception as e:
        logger.error(f"Failed to create Gemini client: {str(e)}", exc_info=True)
        return None


async def synthesize_speech(
    client: genai.Client, text: str, voice: str | None = None
) -> tuple[bytes | None, str | None]:
    """
    Synthesize speech from text using Gemini's TTS API.

    Pure async function that calls external API and returns results.

    Args:
        client: Authenticated Gemini client
        text: Text to convert to speech
        voice: Voice name to use (default: from settings)

    Returns:
        Tuple of (pcm_audio_bytes, error_message)
        - pcm_audio_bytes: Raw PCM audio data if successful, None otherwise
        - error_message: Human-readable error if failed, None otherwise
    """
    try:
        voice_name = voice or settings.tts_default_voice
        logger.info(
            f"Synthesizing speech with {settings.tts_model} ({len(text)} chars, voice: {voice_name})"
        )

        # Prepend TTS instruction to make intent clear to the model
        tts_prompt = f"Say the following text: {text}"

        response = client.models.generate_content(
            model=settings.tts_model,
            contents=tts_prompt,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=voice_name,
                        )
                    )
                ),
            ),
        )

        # Extract audio data from response
        if not response.candidates:
            return None, "TTS response contained no candidates"

        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            return None, "TTS response contained no content parts"

        part = candidate.content.parts[0]
        if not hasattr(part, "inline_data") or not part.inline_data:
            return None, "TTS response contained no audio data"

        pcm_data = part.inline_data.data
        if not pcm_data:
            return None, "TTS produced empty audio data"

        logger.info(f"Speech synthesis successful ({len(pcm_data)} bytes PCM)")
        return pcm_data, None

    except Exception as e:
        error_msg = f"Speech synthesis failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return None, error_msg


# Format configuration: maps format to (pydub_format, codec, mimetype)
AUDIO_FORMATS = {
    "ogg": ("ogg", "libopus", "audio/ogg"),
    "mp3": ("mp3", None, "audio/mpeg"),
    "wav": ("wav", None, "audio/wav"),
    "flac": ("flac", None, "audio/flac"),
}


def pcm_to_audio(
    pcm_data: bytes,
    output_format: str = "ogg",
    channels: int = 1,
    rate: int = 24000,
    sample_width: int = 2,
) -> bytes:
    """
    Convert raw PCM audio data to the specified format.

    Pure function that converts PCM using pydub (requires ffmpeg).

    Args:
        pcm_data: Raw PCM audio bytes
        output_format: Target format ('ogg', 'mp3', 'wav', 'flac')
        channels: Number of audio channels (default: 1 for mono)
        rate: Sample rate in Hz (default: 24000 for Gemini TTS)
        sample_width: Sample width in bytes (default: 2 for 16-bit)

    Returns:
        Audio bytes in the specified format
    """
    audio = AudioSegment(
        data=pcm_data,
        sample_width=sample_width,
        frame_rate=rate,
        channels=channels,
    )

    format_config = AUDIO_FORMATS.get(output_format, AUDIO_FORMATS["ogg"])
    pydub_format, codec, _ = format_config

    buffer = io.BytesIO()
    export_params = {"format": pydub_format}
    if codec:
        export_params["codec"] = codec

    audio.export(buffer, **export_params)
    return buffer.getvalue()


def get_audio_mimetype(output_format: str) -> str:
    """Get the MIME type for an audio format."""
    format_config = AUDIO_FORMATS.get(output_format, AUDIO_FORMATS["ogg"])
    return format_config[2]
