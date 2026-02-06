from io import BytesIO

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy.orm import Session
from starlette.requests import Request

from ..config import settings
from ..database import get_db, get_user_preferences
from ..deps import limiter
from ..logger import logger
from ..schemas import TranscribeResponse, TTSRequest
from ..transcription import create_groq_client, transcribe_audio, validate_audio_file
from ..tts import (
    create_genai_client,
    get_audio_mimetype,
    get_voice_for_language,
    pcm_to_audio,
    synthesize_speech,
    validate_text_input,
)

router = APIRouter()


@router.post("/transcribe", response_model=TranscribeResponse, tags=["Speech-to-Text"])
@limiter.limit(f"{settings.rate_limit_expensive}/minute")
async def transcribe_audio_endpoint(
    request: Request,
    file: UploadFile = File(...),
    language: str | None = Form(None),
    whatsapp_jid: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """
    Transcribe audio to text using Groq Whisper API

    This endpoint ONLY does audio-to-text transcription. It does NOT:
    - Save messages to database
    - Call the AI agent
    - Generate embeddings
    - Process conversation history

    The client should call /chat/enqueue with the transcribed text for AI processing.

    **Request (multipart/form-data):**
    - `file`: Audio file (mp3, wav, ogg, m4a, webm, flac, etc.)
    - `language`: (Optional) ISO-639-1 language code (e.g., 'en', 'es') for better accuracy
    - `whatsapp_jid`: (Optional) JID to fetch user's language preferences

    **Response:**
    - `transcription`: Transcribed text
    - `message`: Status message

    **Supported Formats:** mp3, mp4, mpeg, mpga, m4a, wav, webm, ogg, flac
    **Maximum File Size:** 25 MB

    **Example:**
    ```bash
    curl -X POST http://localhost:8000/transcribe \\
      -F "file=@audio.mp3" \\
      -F "language=en"
    ```
    """
    logger.info("Received audio transcription request")

    try:
        # Step 1: Read and validate audio file
        audio_content = await file.read()
        file_size = len(audio_content)

        is_valid, error_msg, file_format = validate_audio_file(
            file.filename or "unknown", file.content_type, file_size
        )

        if not is_valid:
            logger.warning(f"Invalid audio file: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)

        logger.info(
            f"Audio validated: {file.filename} ({file_size / 1024:.1f} KB, format: {file_format})"
        )

        # Step 2: Determine language from preferences if not provided
        effective_language = language
        if whatsapp_jid and not language:
            prefs = get_user_preferences(db, whatsapp_jid)
            if prefs and prefs.stt_language:
                effective_language = prefs.stt_language
                logger.info(f"Using STT language from preferences: {effective_language}")

        # Step 3: Create Groq client
        groq_client = create_groq_client(settings.groq_api_key)
        if not groq_client:
            raise HTTPException(
                status_code=503,
                detail="Speech-to-text service not configured. Please set GROQ_API_KEY environment variable.",
            )

        # Step 4: Transcribe audio
        audio_file_obj = BytesIO(audio_content)
        transcription_text, transcription_error = await transcribe_audio(
            groq_client,
            audio_file_obj,
            file.filename or f"audio.{file_format}",
            language=effective_language,
        )

        if transcription_error:
            raise HTTPException(status_code=500, detail=transcription_error)

        logger.info(
            f'Transcription successful: "{transcription_text[:100]}..." ({len(transcription_text)} chars)'
        )

        # Step 5: Return transcription ONLY
        return TranscribeResponse(
            transcription=transcription_text, message="Audio transcribed successfully"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing audio transcription: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/tts", tags=["Text-to-Speech"])
@limiter.limit(f"{settings.rate_limit_expensive}/minute")
async def text_to_speech_endpoint(
    request: Request, tts_request: TTSRequest, db: Session = Depends(get_db)
):
    """
    Convert text to speech using Gemini TTS API

    This endpoint converts text to audio using Google's Gemini TTS model.
    Returns audio in the requested format (default: OGG/Opus for WhatsApp compatibility).

    **Request Body:**
    - `text`: Text to convert to speech (max 5000 characters)
    - `whatsapp_jid`: (Optional) JID to fetch user's language preferences
    - `format`: Output format - 'ogg' (default), 'mp3', 'wav', or 'flac'

    **Response:**
    - Audio file in requested format

    **Configuration:**
    - Voice: Based on user's language preference (default: Kore/English)
    - Format: Configurable (OGG/Opus default for WhatsApp voice notes)

    **Example:**
    ```bash
    curl -X POST http://localhost:8000/tts \\
      -H "Content-Type: application/json" \\
      -d '{"text": "Hello!", "format": "mp3"}' \\
      --output speech.mp3
    ```
    """
    logger.info("Received TTS request")

    try:
        # Step 1: Validate text input
        is_valid, error_msg = validate_text_input(tts_request.text)
        if not is_valid:
            logger.warning(f"Invalid TTS input: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)

        logger.info(f"Text validated: {len(tts_request.text)} characters")

        # Step 2: Determine voice based on user preferences
        voice = settings.tts_default_voice
        if tts_request.whatsapp_jid:
            prefs = get_user_preferences(db, tts_request.whatsapp_jid)
            if prefs:
                voice = get_voice_for_language(prefs.tts_language)
                logger.info(f"Using voice '{voice}' for language '{prefs.tts_language}'")

        # Step 3: Create Gemini client
        genai_client = create_genai_client(settings.gemini_api_key)
        if not genai_client:
            raise HTTPException(
                status_code=503,
                detail="Text-to-speech service not configured. Please set GEMINI_API_KEY environment variable.",
            )

        # Step 4: Synthesize speech with selected voice
        pcm_data, synthesis_error = await synthesize_speech(genai_client, tts_request.text, voice)

        if synthesis_error:
            raise HTTPException(status_code=500, detail=synthesis_error)

        # Step 5: Convert PCM to requested format
        output_format = tts_request.format
        audio_data = pcm_to_audio(pcm_data, output_format)
        mimetype = get_audio_mimetype(output_format)

        logger.info(
            f"TTS successful: {len(audio_data)} bytes {output_format.upper()} audio generated"
        )

        # Step 6: Return audio file
        return Response(
            content=audio_data,
            media_type=mimetype,
            headers={"Content-Disposition": f"attachment; filename=speech.{output_format}"},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing TTS request: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
