import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from .logger import logger

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://aiagent:changeme@localhost:5432/aiagent')

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class ConversationMessage(Base):
    __tablename__ = 'conversation_messages'

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, index=True, nullable=False)
    role = Column(String, nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)

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

def get_conversation_history(db, phone: str, limit: int = 10):
    """Retrieve recent conversation history for a user"""
    messages = db.query(ConversationMessage)\
        .filter(ConversationMessage.phone == phone)\
        .order_by(ConversationMessage.timestamp.desc())\
        .limit(limit)\
        .all()

    return list(reversed(messages))

def save_message(db, phone: str, role: str, content: str):
    """Save a message to the database"""
    message = ConversationMessage(phone=phone, role=role, content=content)
    db.add(message)
    db.commit()
