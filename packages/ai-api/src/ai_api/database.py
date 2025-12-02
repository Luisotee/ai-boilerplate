import os
import uuid
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Text, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from .logger import logger

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://aiagent:changeme@localhost:5432/aiagent')

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    whatsapp_jid = Column(String, unique=True, index=True, nullable=False)
    phone = Column(String, nullable=True)
    name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationship
    messages = relationship('ConversationMessage', back_populates='user', cascade='all, delete-orphan')

class ConversationMessage(Base):
    __tablename__ = 'conversation_messages'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=False, index=True)
    role = Column(String, nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)

    # Group context (nullable for backward compatibility)
    sender_jid = Column(String, nullable=True, index=True)  # Participant JID in groups
    sender_name = Column(String, nullable=True)  # Participant name in groups

    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationship
    user = relationship('User', back_populates='messages')

def init_db():
    """Initialize database tables"""
    logger.info('Initializing database...')
    Base.metadata.create_all(bind=engine)
    logger.info('Database initialized successfully')

def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_or_create_user(db, whatsapp_jid: str, name: str = None):
    """Get existing user or create new one by WhatsApp JID"""
    user = db.query(User).filter(User.whatsapp_jid == whatsapp_jid).first()
    if not user:
        user = User(whatsapp_jid=whatsapp_jid, name=name)
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(f'Created new user: {whatsapp_jid}')
    return user

def get_conversation_history(db, whatsapp_jid: str, limit: int = 10):
    """Retrieve recent conversation history for a user by WhatsApp JID"""
    user = get_or_create_user(db, whatsapp_jid)
    messages = db.query(ConversationMessage)\
        .filter(ConversationMessage.user_id == user.id)\
        .order_by(ConversationMessage.timestamp.desc())\
        .limit(limit)\
        .all()

    return list(reversed(messages))

def save_message(db, whatsapp_jid: str, role: str, content: str,
                 sender_jid: str = None, sender_name: str = None):
    """Save a message to the database with optional group context"""
    user = get_or_create_user(db, whatsapp_jid)
    message = ConversationMessage(
        user_id=user.id,
        role=role,
        content=content,
        sender_jid=sender_jid,
        sender_name=sender_name
    )
    db.add(message)
    db.commit()
