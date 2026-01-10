import uuid
from datetime import datetime
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


engine = create_engine(settings.database_url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    whatsapp_jid = Column(String, unique=True, index=True, nullable=False)
    phone = Column(String, nullable=True)
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


def get_or_create_user(db, whatsapp_jid: str, conversation_type: str, name: str = None):
    """Get existing user or create new one by WhatsApp JID"""
    user = db.query(User).filter(User.whatsapp_jid == whatsapp_jid).first()
    if not user:
        user = User(
            whatsapp_jid=whatsapp_jid,
            name=name,
            conversation_type=conversation_type,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(f"Created new user: {whatsapp_jid} (type: {conversation_type})")
    elif name and user.name != name:
        # Update name if provided and changed
        user.name = name
        db.commit()
        logger.info(f"Updated user name: {whatsapp_jid} -> {name}")
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
):
    """Save a message to the database with optional group context and embedding"""
    user = get_or_create_user(db, whatsapp_jid, conversation_type)
    message = ConversationMessage(
        user_id=user.id,
        role=role,
        content=content,
        sender_jid=sender_jid,
        sender_name=sender_name,
        embedding=embedding,
        embedding_generated_at=datetime.utcnow() if embedding else None,
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
