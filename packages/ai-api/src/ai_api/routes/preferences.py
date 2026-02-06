from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db, get_user_preferences
from ..logger import logger
from ..schemas import PreferencesResponse, UpdatePreferencesRequest

router = APIRouter()


@router.get("/preferences/{whatsapp_jid}", response_model=PreferencesResponse, tags=["Preferences"])
async def get_preferences_endpoint(whatsapp_jid: str, db: Session = Depends(get_db)):
    """
    Get user preferences by WhatsApp JID

    Returns the current preferences for a user. Creates default preferences if none exist.

    **Path Parameters:**
    - `whatsapp_jid`: WhatsApp JID (e.g., "1234567890@s.whatsapp.net")

    **Response:**
    - `tts_enabled`: Whether TTS is enabled
    - `tts_language`: TTS language code (e.g., 'en', 'es')
    - `stt_language`: STT language code, null for auto-detect
    """
    logger.info(f"Getting preferences for {whatsapp_jid}")

    try:
        prefs = get_user_preferences(db, whatsapp_jid)
        if not prefs:
            raise HTTPException(status_code=404, detail="User not found")

        return PreferencesResponse(
            tts_enabled=prefs.tts_enabled,
            tts_language=prefs.tts_language,
            stt_language=prefs.stt_language,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting preferences: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.patch(
    "/preferences/{whatsapp_jid}", response_model=PreferencesResponse, tags=["Preferences"]
)
async def update_preferences_endpoint(
    whatsapp_jid: str, request: UpdatePreferencesRequest, db: Session = Depends(get_db)
):
    """
    Update user preferences

    Updates specific preference fields. Only provided fields are updated.

    **Path Parameters:**
    - `whatsapp_jid`: WhatsApp JID

    **Request Body (all fields optional):**
    - `tts_enabled`: Enable/disable TTS
    - `tts_language`: TTS language code (en, es, pt, fr, de)
    - `stt_language`: STT language code, or "auto" for auto-detect

    **Response:**
    - Updated preferences
    """
    logger.info(f"Updating preferences for {whatsapp_jid}")

    try:
        prefs = get_user_preferences(db, whatsapp_jid)
        if not prefs:
            raise HTTPException(status_code=404, detail="User not found")

        # Update only provided fields
        if request.tts_enabled is not None:
            prefs.tts_enabled = request.tts_enabled

        if request.tts_language is not None:
            # Validate language code
            supported = {"en", "es", "pt", "fr", "de"}
            if request.tts_language not in supported:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid TTS language. Supported: {', '.join(sorted(supported))}",
                )
            prefs.tts_language = request.tts_language

        if request.stt_language is not None:
            # Handle "auto" as null
            if request.stt_language.lower() == "auto":
                prefs.stt_language = None
            else:
                supported = {"en", "es", "pt", "fr", "de"}
                if request.stt_language not in supported:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid STT language. Supported: {', '.join(sorted(supported))}, auto",
                    )
                prefs.stt_language = request.stt_language

        db.commit()
        db.refresh(prefs)

        logger.info(f"Preferences updated for {whatsapp_jid}")

        return PreferencesResponse(
            tts_enabled=prefs.tts_enabled,
            tts_language=prefs.tts_language,
            stt_language=prefs.stt_language,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating preferences: {str(e)}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error")
