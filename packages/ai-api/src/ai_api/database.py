import uuid
from datetime import UTC, datetime
from enum import Enum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    String,
    Text,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker

from .config import settings
from .logger import logger


class ConversationType(str, Enum):
    PRIVATE = "private"
    GROUP = "group"


engine = create_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_recycle=settings.db_pool_recycle,
    pool_pre_ping=settings.db_pool_pre_ping,
    echo_pool=settings.db_echo_pool,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    whatsapp_jid = Column(String, unique=True, index=True, nullable=False)
    whatsapp_lid = Column(String, unique=True, index=True, nullable=True)
    phone = Column(String, index=True, nullable=True)
    name = Column(String, nullable=True)
    conversation_type = Column(String, nullable=False, index=True)  # 'private' or 'group'
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    messages = relationship(
        "ConversationMessage",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    preferences = relationship(
        "ConversationPreferences",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    core_memory = relationship(
        "CoreMemory",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String, nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)

    # Group context (nullable for backward compatibility)
    sender_jid = Column(String, nullable=True, index=True)  # Participant JID in groups
    sender_name = Column(String, nullable=True)  # Participant name in groups

    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Embeddings for semantic search (nullable)
    embedding = Column(Vector(3072), nullable=True)  # Google gemini-embedding-001
    embedding_generated_at = Column(DateTime, nullable=True)

    # Relationship
    user = relationship("User", back_populates="messages")


class ConversationPreferences(Base):
    """Per-conversation preferences for TTS and STT settings."""

    __tablename__ = "conversation_preferences"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False, index=True
    )

    # TTS Settings
    tts_enabled = Column(Boolean, default=False, nullable=False)
    tts_language = Column(String, default="en", nullable=False)

    # STT Settings
    stt_language = Column(String, nullable=True)  # null = auto-detect

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationship
    user = relationship("User", back_populates="preferences")


class CoreMemory(Base):
    """Persistent markdown document with AI's notes about a user."""

    __tablename__ = "core_memories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False, index=True
    )
    content = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationship
    user = relationship("User", back_populates="core_memory")


def init_db():
    """Initialize database tables"""
    logger.info("Initializing database...")

    # Enable pgvector extension (required for VECTOR column type)
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    logger.info("pgvector extension enabled")

    # Create all tables
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized successfully")


def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def phone_from_jid(jid: str) -> str | None:
    """Extract E.164 phone number from a phone-based JID."""
    if jid.endswith("@s.whatsapp.net"):
        return f"+{jid.split('@')[0]}"
    return None


def get_or_create_user(
    db,
    whatsapp_jid: str,
    conversation_type: str,
    name: str = None,
    phone: str = None,
    whatsapp_lid: str = None,
):
    """Get existing user or create new one, resolving JID/LID/phone identity."""
    # Auto-extract phone from phone-based JID if not provided
    if not phone:
        phone = phone_from_jid(whatsapp_jid)

    # Step 1: Direct lookup by primary JID (fast path)
    user = db.query(User).filter(User.whatsapp_jid == whatsapp_jid).first()

    # Step 2: Check if this JID matches a known LID
    if not user:
        user = db.query(User).filter(User.whatsapp_lid == whatsapp_jid).first()

    # Step 3: Check if we know this phone number under a different JID
    if not user and phone:
        user = db.query(User).filter(User.phone == phone).first()
        if user and user.whatsapp_jid != whatsapp_jid:
            old_jid = user.whatsapp_jid
            # Preserve the old JID as LID if it was a LID, or store it if no LID exists
            if old_jid.endswith("@lid"):
                user.whatsapp_lid = old_jid
            elif not user.whatsapp_lid:
                user.whatsapp_lid = old_jid
            user.whatsapp_jid = whatsapp_jid
            logger.info(f"Merged user identity: {old_jid} -> {whatsapp_jid} (phone: {phone})")

    # Step 4: Create new user if not found
    if not user:
        user = User(
            whatsapp_jid=whatsapp_jid,
            whatsapp_lid=whatsapp_lid,
            phone=phone,
            name=name,
            conversation_type=conversation_type,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(f"Created new user: {whatsapp_jid} (phone: {phone}, type: {conversation_type})")
        return user

    # Enrich existing user with new info
    changed = False
    if name and user.name != name:
        user.name = name
        changed = True
    if phone and not user.phone:
        user.phone = phone
        changed = True
    if whatsapp_lid and not user.whatsapp_lid:
        user.whatsapp_lid = whatsapp_lid
        changed = True
    if changed:
        db.commit()

    return user


def get_conversation_history(db, whatsapp_jid: str, conversation_type: str, limit: int = None):
    """Retrieve recent conversation history for a user by WhatsApp JID"""
    user = get_or_create_user(db, whatsapp_jid, conversation_type)

    # Load limit from settings if not explicitly provided
    if limit is None:
        if user.conversation_type == ConversationType.GROUP:
            limit = settings.history_limit_group
        else:  # private
            limit = settings.history_limit_private

        logger.info(f"Using history limit {limit} for {user.conversation_type} conversation")

    messages = (
        db.query(ConversationMessage)
        .filter(ConversationMessage.user_id == user.id)
        .order_by(ConversationMessage.timestamp.desc())
        .limit(limit)
        .all()
    )

    return list(reversed(messages))


def save_message(
    db,
    whatsapp_jid: str,
    role: str,
    content: str,
    conversation_type: str,
    sender_jid: str = None,
    sender_name: str = None,
    embedding: list = None,
    phone: str = None,
    whatsapp_lid: str = None,
):
    """Save a message to the database with optional group context and embedding"""
    user = get_or_create_user(
        db, whatsapp_jid, conversation_type, phone=phone, whatsapp_lid=whatsapp_lid
    )
    message = ConversationMessage(
        user_id=user.id,
        role=role,
        content=content,
        sender_jid=sender_jid,
        sender_name=sender_name,
        embedding=embedding,
        embedding_generated_at=datetime.now(UTC) if embedding else None,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    logger.info(
        f"Saved {role} message for user {whatsapp_jid} (embedding: {embedding is not None})"
    )
    return message


def get_or_create_preferences(db, user_id: str) -> ConversationPreferences:
    """Get existing preferences or create with defaults."""
    prefs = (
        db.query(ConversationPreferences).filter(ConversationPreferences.user_id == user_id).first()
    )

    if not prefs:
        prefs = ConversationPreferences(user_id=user_id)
        db.add(prefs)
        db.commit()
        db.refresh(prefs)
        logger.info(f"Created default preferences for user {user_id}")

    return prefs


def get_user_preferences(db, whatsapp_jid: str) -> ConversationPreferences | None:
    """Get preferences by WhatsApp JID (convenience function)."""
    user = db.query(User).filter(User.whatsapp_jid == whatsapp_jid).first()
    if not user:
        return None
    return get_or_create_preferences(db, str(user.id))


def get_or_create_core_memory(db, user_id: str) -> CoreMemory:
    """Get existing core memory or create empty one."""
    mem = db.query(CoreMemory).filter(CoreMemory.user_id == user_id).first()
    if not mem:
        mem = CoreMemory(user_id=user_id, content="")
        db.add(mem)
        db.commit()
        db.refresh(mem)
        logger.info(f"Created empty core memory for user {user_id}")
    return mem
