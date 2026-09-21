"""
PDF processing consumer for the shared ``stream:pdf_processing`` Redis Stream.

Runs in the stream worker next to the chat consumer. Every PDF — knowledge-base
uploads and documents attached in chat — is parsed here, never in the API
process.

Semantics:

- **Concurrency**: at most ``KB_MAX_CONCURRENT_PROCESSING`` jobs per worker
  process. A slot is acquired BEFORE a job is read, so waiting jobs stay in the
  stream (visible to other workers) instead of piling up in this process.
- **Ack**: a job is acknowledged (and deleted from the stream) once its outcome
  is decided — success, scheduled retry, or dead letter. A job whose worker dies
  mid-parse stays pending and is reclaimed by the periodic ``XAUTOCLAIM`` sweep.
- **Retries**: retriable failures (``processing.is_retriable_error``: timeouts,
  network, HTTP 429/5xx) and reclaimed jobs are retried up to
  ``KB_MAX_PDF_RETRIES`` times with exponential backoff
  (``KB_RETRY_BASE_DELAY_SECONDS * 4**attempt``). The delay is parked in a Redis
  sorted set, so it survives a worker restart.
- **Dead letter**: non-retriable failures, exhausted retries and malformed jobs
  are copied to ``stream:pdf_processing:dead`` with the reason.
- **Chat PDFs**: the user's message gets a ✅ reaction when the document is
  searchable, ❌ when it permanently failed (the ⏳ was sent when the chat job
  enqueued it).

Parsing itself is ``processing.process_pdf_document`` — the same parser
selection (``PDF_PARSER``, LlamaParse → Docling fallback) and timeouts as before.
"""

import asyncio
import time

import httpx
from redis.asyncio import Redis

from ..config import get_whatsapp_api_key, get_whatsapp_client_url, settings
from ..database import SessionLocal
from ..kb_models import KnowledgeBaseDocument
from ..logger import logger
from ..processing import is_retriable_error, process_pdf_document
from ..whatsapp import create_whatsapp_client
from .manager import (
    acknowledge_pdf_message,
    claim_stale_pdf_messages,
    dead_letter_pdf_job,
    ensure_pdf_consumer_group,
    promote_due_pdf_retries,
    read_pdf_stream_messages,
    schedule_pdf_retry,
)

# How often the loop promotes due retries and reclaims abandoned jobs.
MAINTENANCE_INTERVAL_SECONDS = 15.0

# Error text stored on the document when its worker died mid-parse too often.
INTERRUPTED_ERROR = "Processing was interrupted repeatedly (worker restarted or ran out of memory)."


def _reclaim_min_idle_ms() -> int:
    """Idle time after which a pending job is considered abandoned.

    A live worker holds a job for at most KB_PROCESSING_TIMEOUT_SECONDS (the
    outer ``wait_for``), so twice that plus a minute never steals a slow parse.
    """
    return (settings.kb_processing_timeout_seconds * 2 + 60) * 1000


def _decode(value: bytes | str | None, default: str | None = None) -> str | None:
    """Safely decode a stream field."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    try:
        return value.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return default


def _decode_fields(data: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in data.items():
        k = _decode(key)
        v = _decode(value)
        if k is not None and v is not None:
            out[k] = v
    return out


async def _send_reaction(fields: dict[str, str], emoji: str) -> None:
    """React to the chat message a PDF arrived with (no-op for KB uploads)."""
    whatsapp_jid = fields.get("whatsapp_jid")
    whatsapp_message_id = fields.get("whatsapp_message_id")
    if not whatsapp_jid or not whatsapp_message_id:
        return

    client_id = fields.get("client_id")
    try:
        async with httpx.AsyncClient(timeout=settings.whatsapp_client_timeout) as http_client:
            wa_client = create_whatsapp_client(
                http_client=http_client,
                base_url=get_whatsapp_client_url(client_id),
                api_key=get_whatsapp_api_key(client_id),
            )
            await wa_client.send_reaction(whatsapp_jid, whatsapp_message_id, emoji)
    except Exception:
        logger.warning(f"[PDF] Failed to send {emoji} reaction", exc_info=True)


def _update_document_status(
    document_id: str, status: str, error_message: str | None = None
) -> None:
    """Set a document's status (synchronous; call via ``asyncio.to_thread``)."""
    db = SessionLocal()
    try:
        doc = db.query(KnowledgeBaseDocument).filter_by(id=document_id).first()
        if doc:
            doc.status = status
            if error_message is not None:
                doc.error_message = error_message
            db.commit()
    except Exception:
        logger.error(
            f"[PDF] Failed to set document {document_id} status to {status}", exc_info=True
        )
        db.rollback()
    finally:
        db.close()


async def _handle_failure(
    redis: Redis,
    message_id: str,
    fields: dict[str, str],
    reason: str,
    retriable: bool,
    *,
    error_message: str | None = None,
) -> None:
    """Retry with backoff, or dead-letter; then acknowledge the job."""
    document_id = fields.get("document_id", "?")
    attempt = int(fields.get("retry_count") or 0)

    if retriable and attempt < settings.kb_max_pdf_retries:
        delay = settings.kb_retry_base_delay_seconds * (4**attempt)
        logger.info(
            f"[PDF] Scheduling retry {attempt + 1}/{settings.kb_max_pdf_retries} "
            f"for document {document_id} in {delay}s ({reason})"
        )
        await schedule_pdf_retry(
            redis, {**fields, "retry_count": str(attempt + 1)}, due_at=time.time() + delay
        )
        await asyncio.to_thread(_update_document_status, document_id, "queued")
    else:
        why = "max retries exhausted" if retriable else "non-retriable error"
        logger.warning(f"[PDF] Document {document_id} permanently failed ({why}): {reason}")
        await dead_letter_pdf_job(redis, fields, f"{why}: {reason}")
        if error_message is not None:
            await asyncio.to_thread(_update_document_status, document_id, "failed", error_message)
        await _send_reaction(fields, "❌")

    await acknowledge_pdf_message(redis, message_id)


async def process_pdf_job(redis: Redis, message_id: str, data: dict) -> None:
    """Process one PDF job and settle it (ack + retry / dead-letter / reaction)."""
    fields = _decode_fields(data)
    document_id = fields.get("document_id")
    file_path = fields.get("file_path")

    if not document_id or not file_path:
        logger.error(f"[PDF] Job {message_id} missing document_id/file_path; dead-lettering")
        await dead_letter_pdf_job(redis, fields, "missing required fields")
        await acknowledge_pdf_message(redis, message_id)
        return

    attempt = int(fields.get("retry_count") or 0)
    logger.info(
        f"[PDF] Processing document {document_id} "
        f"(attempt {attempt + 1}/{settings.kb_max_pdf_retries + 1})"
    )

    try:
        status = await process_pdf_document(
            document_id=document_id,
            file_path=file_path,
            whatsapp_jid=fields.get("whatsapp_jid"),
            raise_on_failure=True,
        )
    except Exception as e:
        # The document row already records the failure (status + error_message).
        await _handle_failure(
            redis, message_id, fields, f"{type(e).__name__}: {e}", is_retriable_error(e)
        )
        return

    if status is None:
        logger.info(f"[PDF] Document {document_id} no longer exists; dropping job")
        await acknowledge_pdf_message(redis, message_id)
    elif status == "completed":
        logger.info(f"[PDF] Document {document_id} processed successfully")
        await acknowledge_pdf_message(redis, message_id)
        await _send_reaction(fields, "✅")
    elif status == "failed":
        # No exception but zero chunks stored: every embedding call failed or
        # timed out — almost always a transient embedding-API problem.
        await _handle_failure(
            redis, message_id, fields, "no chunk could be embedded", retriable=True
        )
    else:
        # "partial": some chunks stored, but search only reads completed
        # documents, so tell the chat user it did not work.
        logger.warning(f"[PDF] Document {document_id} finished with status {status!r}")
        await acknowledge_pdf_message(redis, message_id)
        await _send_reaction(fields, "❌")


async def _handle_reclaimed(redis: Redis, message_id: str, data: dict) -> None:
    """A job abandoned by a dead worker: count it as an attempt and retry it."""
    fields = _decode_fields(data)
    logger.warning(
        f"[PDF] Reclaimed abandoned job {message_id} for document {fields.get('document_id')}"
    )
    if not fields.get("document_id") or not fields.get("file_path"):
        await dead_letter_pdf_job(redis, fields, "missing required fields")
        await acknowledge_pdf_message(redis, message_id)
        return
    await _handle_failure(
        redis,
        message_id,
        fields,
        "worker stopped while processing (restart or out of memory)",
        retriable=True,
        error_message=INTERRUPTED_ERROR,
    )


async def _maintenance(redis: Redis) -> None:
    """Promote due retries and reclaim jobs abandoned by dead workers."""
    await promote_due_pdf_retries(redis, time.time())
    for message_id, data in await claim_stale_pdf_messages(redis, _reclaim_min_idle_ms()):
        try:
            await _handle_reclaimed(redis, message_id, data)
        except Exception:
            logger.error(f"[PDF] Failed to settle reclaimed job {message_id}", exc_info=True)


async def run_pdf_consumer(redis: Redis) -> None:
    """
    Main PDF consumer loop (runs until cancelled).

    Holds at most ``KB_MAX_CONCURRENT_PROCESSING`` jobs at once; on cancellation
    in-flight jobs are cancelled too and stay pending in Redis, to be reclaimed
    and retried after the worker comes back.
    """
    concurrency = settings.kb_max_concurrent_processing
    semaphore = asyncio.Semaphore(concurrency)
    in_flight: set[asyncio.Task] = set()
    logger.info(
        f"Starting PDF consumer (concurrency={concurrency}, "
        f"max_retries={settings.kb_max_pdf_retries})"
    )

    await ensure_pdf_consumer_group(redis)
    last_maintenance = 0.0

    async def _run(message_id: str, data: dict) -> None:
        try:
            await process_pdf_job(redis, message_id, data)
        except Exception:
            # Settling failed (e.g. Redis down): leave the job pending so it is
            # reclaimed and retried later instead of being lost.
            logger.error(f"[PDF] Unhandled error in job {message_id}", exc_info=True)
        finally:
            semaphore.release()

    try:
        while True:
            try:
                if time.monotonic() - last_maintenance >= MAINTENANCE_INTERVAL_SECONDS:
                    last_maintenance = time.monotonic()
                    await _maintenance(redis)

                await semaphore.acquire()
                try:
                    messages = await read_pdf_stream_messages(redis, count=1, block=5000)
                except BaseException:
                    semaphore.release()
                    raise

                jobs = [
                    (_decode(message_id), data)
                    for _stream, message_list in messages or []
                    for message_id, data in message_list
                ]
                if not jobs:
                    semaphore.release()
                    continue

                # count=1, so exactly one job; the slot is released by _run.
                message_id, data = jobs[0]
                task = asyncio.create_task(_run(message_id, data))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("[PDF] Consumer loop error", exc_info=True)
                await asyncio.sleep(5)
    finally:
        for task in list(in_flight):
            task.cancel()
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)
