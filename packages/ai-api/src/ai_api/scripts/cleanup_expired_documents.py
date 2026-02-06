"""
Cleanup expired conversation-scoped documents.

Deletes documents that have exceeded their TTL (expires_at < NOW()),
removing both database records (KnowledgeBaseDocument + cascaded chunks)
and PDF files from disk.

Called periodically by the background task in main.py.
"""

from datetime import datetime
from pathlib import Path

from ..config import settings
from ..database import SessionLocal
from ..kb_models import KnowledgeBaseDocument
from ..logger import logger

# Configure upload directory
UPLOAD_DIR = Path(settings.kb_upload_dir)


async def cleanup_expired_documents():
    """
    Delete all documents where expires_at < NOW().

    This function:
    1. Queries for expired documents
    2. Deletes PDF files from disk
    3. Deletes database records (cascades to chunks)
    4. Logs cleanup statistics
    """
    db = SessionLocal()

    try:
        logger.info("Starting expired document cleanup...")

        # Find expired documents
        now = datetime.utcnow()
        expired_docs = (
            db.query(KnowledgeBaseDocument)
            .filter(
                KnowledgeBaseDocument.expires_at.isnot(None),
                KnowledgeBaseDocument.expires_at < now,
            )
            .all()
        )

        if not expired_docs:
            logger.info("No expired documents found.")
            return {"deleted_count": 0, "errors": []}

        logger.info(f"Found {len(expired_docs)} expired documents to delete")

        deleted_count = 0
        errors = []

        for doc in expired_docs:
            try:
                # Delete file from disk
                file_path = UPLOAD_DIR / doc.filename
                if file_path.exists():
                    file_path.unlink()
                    logger.debug(f"Deleted file: {file_path}")
                else:
                    logger.warning(f"File not found (already deleted?): {file_path}")

                # Delete from database (cascades to chunks)
                db.delete(doc)
                db.commit()

                logger.info(
                    f"Deleted expired document: {doc.original_filename} "
                    f"(ID: {doc.id}, expired: {doc.expires_at})"
                )
                deleted_count += 1

            except Exception as e:
                error_msg = f"Error deleting document {doc.id}: {str(e)}"
                logger.error(error_msg, exc_info=True)
                errors.append(error_msg)
                db.rollback()

        logger.info(f"Cleanup complete: {deleted_count} documents deleted, {len(errors)} errors")

        return {"deleted_count": deleted_count, "errors": errors}

    except Exception as e:
        logger.error(f"Error during cleanup: {str(e)}", exc_info=True)
        raise

    finally:
        db.close()
