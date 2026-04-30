"""
Mock factories for ai-api integration tests.

Provides factory functions that return mock objects matching the shapes of
production SQLAlchemy models (ConversationMessage) and httpx responses,
without requiring a real database or HTTP connection.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock


def make_conversation_message(
    role: str,
    content: str,
    jid: str | None = None,
    *,
    sender_jid: str | None = None,
    sender_name: str | None = None,
    message_id: str | None = None,
    user_id: str | None = None,
    timestamp: datetime | None = None,
    embedding: list | None = None,
) -> MagicMock:
    """
    Build a mock that looks like a ``ConversationMessage`` ORM instance.

    The returned MagicMock has the same attribute names and types as the
    real SQLAlchemy model defined in ``ai_api.database``, so it can be used
    wherever handler / service code reads message attributes.

    Parameters
    ----------
    role:
        Message role -- typically ``"user"`` or ``"assistant"``.
    content:
        The text content of the message.
    jid:
        Optional WhatsApp JID.  Used to derive a ``user_id`` if none is
        provided (purely for realistic-looking test data).
    sender_jid:
        Participant JID in group conversations (nullable).
    sender_name:
        Display name of the sender in group conversations (nullable).
    message_id:
        Explicit UUID string for the message ``id``.  Auto-generated if
        omitted.
    user_id:
        Explicit UUID string for the owning user.  Auto-generated if omitted.
    timestamp:
        Message timestamp.  Defaults to ``datetime.now(UTC)``.
    embedding:
        Optional embedding vector (list of floats).

    Returns
    -------
    MagicMock
        A mock object with attributes matching ``ConversationMessage``.
    """
    mock = MagicMock()
    mock.id = uuid.UUID(message_id) if message_id else uuid.uuid4()
    mock.user_id = uuid.UUID(user_id) if user_id else uuid.uuid4()
    mock.role = role
    mock.content = content
    mock.sender_jid = sender_jid
    mock.sender_name = sender_name
    mock.timestamp = timestamp or datetime.now(UTC)
    mock.embedding = embedding
    mock.embedding_generated_at = datetime.now(UTC) if embedding else None

    # Allow dict-like access patterns that some serialization paths use
    mock.configure_mock(
        **{
            "__getitem__": lambda self, key: getattr(self, key),
        }
    )

    return mock


def make_http_response(
    status: int,
    json_data: dict | list | None = None,
    *,
    text: str | None = None,
    headers: dict | None = None,
) -> MagicMock:
    """
    Build a mock that looks like an ``httpx.Response``.

    Parameters
    ----------
    status:
        HTTP status code (e.g. ``200``, ``404``, ``500``).
    json_data:
        JSON-serialisable payload returned by ``.json()``.  If ``None``,
        calling ``.json()`` will raise a ``ValueError`` (mimicking a
        non-JSON response).
    text:
        Plain text body returned by ``.text``.  Defaults to ``""`` if
        ``json_data`` is ``None``.
    headers:
        Response headers dict.  Defaults to a minimal
        ``{"content-type": "application/json"}`` when ``json_data`` is
        provided.

    Returns
    -------
    MagicMock
        A mock object whose interface matches the subset of
        ``httpx.Response`` commonly used in the codebase.
    """
    mock = MagicMock()
    mock.status_code = status
    mock.is_success = 200 <= status < 300
    mock.is_error = status >= 400
    mock.is_client_error = 400 <= status < 500
    mock.is_server_error = status >= 500

    if json_data is not None:
        mock.json.return_value = json_data
        mock.text = text if text is not None else str(json_data)
        mock.headers = headers or {"content-type": "application/json"}
    else:
        mock.json.side_effect = ValueError("No JSON body")
        mock.text = text if text is not None else ""
        mock.headers = headers or {"content-type": "text/plain"}

    # httpx.Response supports raise_for_status()
    if status >= 400:
        mock.raise_for_status.side_effect = Exception(f"HTTP {status} error")
    else:
        mock.raise_for_status.return_value = None

    return mock


def make_user(
    whatsapp_jid: str | None = None,
    *,
    conversation_type: str = "private",
    name: str | None = "Test User",
    phone: str | None = None,
    whatsapp_lid: str | None = None,
    telegram_jid: str | None = None,
    user_id: str | None = None,
) -> MagicMock:
    """
    Build a mock that looks like a ``User`` ORM instance.

    Parameters
    ----------
    whatsapp_jid:
        The user's primary WhatsApp JID.  Defaults to a sample private JID.
    conversation_type:
        ``"private"`` or ``"group"``.
    name:
        Display name.
    phone:
        E.164 phone number.
    whatsapp_lid:
        WhatsApp LID (nullable).
    user_id:
        Explicit UUID string.  Auto-generated if omitted.

    Returns
    -------
    MagicMock
        A mock object with attributes matching ``User``.
    """
    mock = MagicMock()
    mock.id = uuid.UUID(user_id) if user_id else uuid.uuid4()
    mock.whatsapp_jid = whatsapp_jid or "5511999999999@s.whatsapp.net"
    mock.whatsapp_lid = whatsapp_lid
    mock.telegram_jid = telegram_jid
    mock.phone = phone
    mock.name = name
    mock.conversation_type = conversation_type
    mock.created_at = datetime.now(UTC)

    # Relationships (empty by default -- tests can populate as needed)
    mock.messages = []
    mock.preferences = None
    mock.core_memory = None

    return mock


def make_conversation_preferences(
    *,
    tts_enabled: bool = False,
    tts_language: str = "en",
    stt_language: str | None = None,
    user_id: str | None = None,
) -> MagicMock:
    """
    Build a mock that looks like a ``ConversationPreferences`` ORM instance.

    Parameters
    ----------
    tts_enabled:
        Whether TTS is enabled.
    tts_language:
        TTS language code.
    stt_language:
        STT language code (``None`` = auto-detect).
    user_id:
        Explicit UUID string.  Auto-generated if omitted.

    Returns
    -------
    MagicMock
        A mock object with attributes matching ``ConversationPreferences``.
    """
    mock = MagicMock()
    mock.id = uuid.uuid4()
    mock.user_id = uuid.UUID(user_id) if user_id else uuid.uuid4()
    mock.tts_enabled = tts_enabled
    mock.tts_language = tts_language
    mock.stt_language = stt_language
    mock.created_at = datetime.now(UTC)
    mock.updated_at = datetime.now(UTC)
    return mock
