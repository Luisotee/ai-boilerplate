"""Admin endpoints for the management dashboard.

All routes here are protected by the standard ``X-API-Key`` middleware (the
``/admin`` prefix is intentionally NOT in ``_AUTH_EXEMPT_PREFIXES``). They expose
the bot's system prompt, runtime settings, and a read-only conversation viewer.

The dashboard reads the prompt/settings authoritatively from the request's DB
session; writes additionally call ``runtime_config.invalidate()`` so the agent
(and stream worker) pick up changes promptly.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, nullslast, or_
from sqlalchemy.orm import Session

from ..agent.core import DEFAULT_SYSTEM_PROMPT
from ..config import settings
from ..database import (
    ConversationMessage,
    User,
    clear_active_prompt,
    delete_setting_override,
    get_bot_prompt_row,
    get_db,
    get_setting_overrides,
    is_telegram_jid,
    set_active_prompt,
    set_setting_override,
)
from ..kb_models import KnowledgeBaseDocument
from ..logger import logger
from ..runtime_config import REGISTRY, REGISTRY_BY_KEY, coerce_value, runtime_config
from ..schemas import (
    MessageItem,
    MessagesResponse,
    OverviewResponse,
    PromptResponse,
    SettingItem,
    SettingsResponse,
    UpdatePromptRequest,
    UpdateSettingsRequest,
    UsersResponse,
    UserSummary,
)

router = APIRouter(prefix="/admin", tags=["Admin"])

_MASK = "********"


# --- System prompt ---


def _prompt_payload(db: Session) -> PromptResponse:
    row = get_bot_prompt_row(db)
    is_overridden = bool(row and row.content)
    return PromptResponse(
        content=row.content if is_overridden else DEFAULT_SYSTEM_PROMPT,
        is_overridden=is_overridden,
        default_length=len(DEFAULT_SYSTEM_PROMPT),
        updated_at=row.updated_at if row else None,
    )


@router.get("/prompt", response_model=PromptResponse)
async def get_prompt(db: Session = Depends(get_db)):
    """Return the active system prompt (override if set, else the default)."""
    return _prompt_payload(db)


@router.put("/prompt", response_model=PromptResponse)
async def put_prompt(request: UpdatePromptRequest, db: Session = Depends(get_db)):
    """Set the active system-prompt override. Takes effect on the next message."""
    if not request.content.strip():
        raise HTTPException(status_code=400, detail="Prompt content cannot be blank")
    try:
        set_active_prompt(db, request.content)
    except Exception as e:
        logger.error(f"Error saving prompt: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error") from e
    logger.info("System prompt override updated (%d chars)", len(request.content))
    return _prompt_payload(db)


@router.delete("/prompt", response_model=PromptResponse)
async def delete_prompt(db: Session = Depends(get_db)):
    """Remove the override, reverting the agent to its hardcoded default."""
    try:
        clear_active_prompt(db)
    except Exception as e:
        logger.error(f"Error clearing prompt: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error") from e
    logger.info("System prompt override cleared (reverted to default)")
    return _prompt_payload(db)


# --- Runtime settings ---


def _settings_payload(db: Session) -> SettingsResponse:
    overrides = get_setting_overrides(db)  # key -> JSON string
    items: list[SettingItem] = []
    for spec in REGISTRY:
        default = getattr(settings, spec.key)
        has_override = spec.hot and spec.key in overrides
        if has_override:
            try:
                value = json.loads(overrides[spec.key])
            except (ValueError, TypeError):
                logger.warning("Malformed stored override for '%s'; showing default", spec.key)
                value = default
                has_override = False
        else:
            value = default
        if spec.secret:
            value = _MASK if value else None
            default = _MASK if default else None
        items.append(
            SettingItem(
                key=spec.key,
                value=value,
                default=default,
                source="override" if has_override else "default",
                hot=spec.hot,
                category=spec.category,
                type=spec.type,
                description=spec.description,
                choices=list(spec.choices) if spec.choices else None,
                secret=spec.secret,
            )
        )
    return SettingsResponse(settings=items)


def _validate_cross_constraints(coerced: dict[str, object]) -> None:
    """Reject overrides that would violate invariants Settings checks at boot.

    Validated against the *resulting* config: a value being set in this same
    request wins over the currently-effective one, so e.g. enabling self-hosted
    Whisper and pointing at its URL in a single PATCH is accepted.
    """

    def effective(key: str) -> object:
        return coerced[key] if key in coerced else runtime_config.get(key)

    if (
        "llamaparse_timeout_seconds" in coerced
        and coerced["llamaparse_timeout_seconds"] >= settings.kb_processing_timeout_seconds
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "llamaparse_timeout_seconds must be below kb_processing_timeout_seconds "
                f"({settings.kb_processing_timeout_seconds})"
            ),
        )
    if "stt_provider" in coerced:
        provider = coerced["stt_provider"]
        if provider == "groq" and not settings.groq_api_key:
            raise HTTPException(
                status_code=400,
                detail="Cannot set stt_provider=groq: GROQ_API_KEY is not configured",
            )
        if provider == "whisper" and not effective("whisper_base_url"):
            raise HTTPException(
                status_code=400,
                detail="Cannot set stt_provider=whisper: whisper_base_url is not set",
            )


@router.get("/settings", response_model=SettingsResponse)
async def get_settings(db: Session = Depends(get_db)):
    """List every registered setting with its effective value and metadata."""
    return _settings_payload(db)


@router.patch("/settings", response_model=SettingsResponse)
async def patch_settings(request: UpdateSettingsRequest, db: Session = Depends(get_db)):
    """Override one or more hot settings. Rejects unknown or restart-only keys."""
    if not request.overrides:
        raise HTTPException(status_code=400, detail="No overrides provided")

    # Validate everything before writing anything.
    coerced: dict[str, object] = {}
    for key, raw in request.overrides.items():
        spec = REGISTRY_BY_KEY.get(key)
        if spec is None:
            raise HTTPException(status_code=400, detail=f"Unknown setting: '{key}'")
        if not spec.hot:
            raise HTTPException(
                status_code=400,
                detail=f"Setting '{key}' requires a restart and cannot be changed at runtime",
            )
        try:
            value = coerce_value(spec, raw)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        coerced[key] = value

    # Validate cross-key invariants against the merged result, then write.
    _validate_cross_constraints(coerced)

    try:
        for key, value in coerced.items():
            set_setting_override(db, key, json.dumps(value))
    except Exception as e:
        logger.error(f"Error saving settings: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error") from e

    runtime_config.invalidate()
    logger.info("Runtime settings updated: %s", ", ".join(coerced))
    return _settings_payload(db)


@router.delete("/settings/{key}", response_model=SettingsResponse)
async def delete_setting(key: str, db: Session = Depends(get_db)):
    """Remove an override, reverting the setting to its env default."""
    if key not in REGISTRY_BY_KEY:
        raise HTTPException(status_code=404, detail=f"Unknown setting: '{key}'")
    try:
        delete_setting_override(db, key)
    except Exception as e:
        logger.error(f"Error deleting setting override: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error") from e
    runtime_config.invalidate()
    logger.info("Runtime setting override removed: %s", key)
    return _settings_payload(db)


# --- Conversation viewer (read-only) ---


def _resolve_user(db: Session, jid: str) -> User | None:
    """Look up a user by any identity column without creating one."""
    if is_telegram_jid(jid):
        user = db.query(User).filter(User.telegram_jid == jid).first()
        if user:
            return user
    return db.query(User).filter(or_(User.whatsapp_jid == jid, User.whatsapp_lid == jid)).first()


@router.get("/users", response_model=UsersResponse)
async def list_users(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List users with message counts, ordered by most recent activity."""
    total = db.query(User).count()
    rows = (
        db.query(
            User,
            func.count(ConversationMessage.id).label("message_count"),
            func.max(ConversationMessage.timestamp).label("last_message_at"),
        )
        .outerjoin(ConversationMessage, ConversationMessage.user_id == User.id)
        .group_by(User.id)
        .order_by(nullslast(func.max(ConversationMessage.timestamp).desc()))
        .limit(limit)
        .offset(offset)
        .all()
    )
    users = [
        UserSummary(
            whatsapp_jid=user.whatsapp_jid,
            name=user.name,
            conversation_type=user.conversation_type,
            message_count=message_count,
            last_message_at=last_message_at,
        )
        for user, message_count, last_message_at in rows
    ]
    return UsersResponse(users=users, total=total, limit=limit, offset=offset)


@router.get("/users/{whatsapp_jid}/messages", response_model=MessagesResponse)
async def get_user_messages(
    whatsapp_jid: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Return a user's conversation history (newest first), paginated."""
    user = _resolve_user(db, whatsapp_jid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    total = db.query(ConversationMessage).filter(ConversationMessage.user_id == user.id).count()
    messages = (
        db.query(ConversationMessage)
        .filter(ConversationMessage.user_id == user.id)
        .order_by(ConversationMessage.timestamp.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    items = [
        MessageItem(
            role=m.role,
            content=m.content,
            sender_name=m.sender_name,
            timestamp=m.timestamp,
        )
        for m in messages
    ]
    return MessagesResponse(
        whatsapp_jid=whatsapp_jid, messages=items, total=total, limit=limit, offset=offset
    )


@router.get("/overview", response_model=OverviewResponse)
async def overview(db: Session = Depends(get_db)):
    """Return high-level counts for the dashboard landing page."""
    return OverviewResponse(
        users=db.query(User).count(),
        messages=db.query(ConversationMessage).count(),
        knowledge_base_documents=db.query(KnowledgeBaseDocument).count(),
    )
