"""
Core chat processing logic, called by the Redis Streams consumer.
"""

import asyncio

import httpx
from pydantic_ai import UsageLimitExceeded

from ..agent import AgentDeps, format_message_history, get_ai_response
from ..agent.model_chain import MODEL_ERRORS
from ..config import get_whatsapp_api_key, get_whatsapp_client_url, settings
from ..database import SessionLocal, get_conversation_history, save_message
from ..embeddings import create_embedding_service
from ..formatting import markdown_to_whatsapp, render_reply
from ..logger import logger
from ..queue.connection import get_redis_client
from ..queue.utils import delete_job_image, get_job_images, save_job_chunk, set_job_metadata
from ..reply_media import ReplyMedia, collect_reply_media
from ..services.bug_reports import record_auto_report
from ..surfaces import (
    delivers_through_chat_client,
    renders_markdown,
    stores_markdown,
    surface_of,
)
from ..whatsapp import WhatsAppClient, create_whatsapp_client
from .delivery import deliver_if_late
from .manager import enqueue_pdf_processing

#: Reply delivered when the model chain fails. English, like every user-facing
#: string in this template. Deliberately NOT saved to conversation history.
MODEL_ERROR_FALLBACK_TEXT = (
    "Sorry, something went wrong while processing your message. Please try again."
)


async def process_chat_job_direct(
    user_id: str,
    whatsapp_jid: str,
    message: str,
    conversation_type: str,
    user_message_id: str,
    job_id: str,
    whatsapp_message_id: str | None = None,
    image_mimetype: str | None = None,
    has_image: bool = False,
    has_document: bool = False,
    document_id: str | None = None,
    document_path: str | None = None,
    document_filename: str | None = None,
    sender_name: str | None = None,
    client_id: str | None = None,
    is_group_admin: bool | None = None,
    run_user_message_ids: list[str] | None = None,
    enqueued_at_ms: int | None = None,
    image_count: int = 1,
    image_sources: list[tuple[str, str | None, int]] | None = None,
    absorbed_job_ids: list[str] | None = None,
) -> dict:
    """
    Process a chat message asynchronously (one Redis Streams job).

    This function:
    1. Retrieves conversation history from PostgreSQL
    2. Initializes embedding service and RAG instances
    3. Streams tokens from Pydantic AI agent (with optional image for vision)
    4. Saves each token chunk to Redis for real-time client polling
    5. Generates embedding for complete response
    6. Saves final assistant message to PostgreSQL
    7. Stores job metadata in Redis

    Args:
        user_id: User's UUID
        whatsapp_jid: WhatsApp JID (conversation identifier)
        message: User's raw message text
        conversation_type: 'private' or 'group'
        user_message_id: UUID of saved user message in PostgreSQL
        job_id: Unique job identifier
        whatsapp_message_id: WhatsApp message ID for reactions (optional)
        image_mimetype: Image MIME type if image is attached (optional)
        has_image: Whether this message has an attached image
        has_document: Whether this message has an attached document (PDF)
        document_id: UUID of the document in knowledge base (optional)
        document_path: Path to the document file (optional)
        document_filename: Original filename of the document (optional)
        sender_name: Sender's display name for group messages (optional)
        run_user_message_ids: Every saved user message this run answers, oldest
            first (several for a merged burst); defaults to [user_message_id]
        enqueued_at_ms: When the job entered the stream (unix ms). A reply ready
            after the clients' polling window is pushed (streams/delivery.py)
        image_count: Images stored for this job (more than 1 for an album)
        image_sources: (job_id, mimetype, count) of every job whose images this
            run sees (a merged burst); defaults to this job's own images
        absorbed_job_ids: Earlier jobs of a burst merged into this run. They are
            closed as merged (their clients send nothing) when the run starts

    Returns:
        Dict with processing result including success status, job_id, chunk count

    Raises:
        Exception: Any error during processing
    """
    logger.info(f"[Job {job_id}] Starting chat processing for user {user_id}")
    logger.info(f"[Job {job_id}] WhatsApp JID: {whatsapp_jid}")
    logger.info(f"[Job {job_id}] Conversation type: {conversation_type}")
    logger.info(f"[Job {job_id}] WhatsApp message ID: {whatsapp_message_id}")
    logger.info(f"[Job {job_id}] Has image: {has_image}")
    logger.info(f"[Job {job_id}] Has document: {has_document}")

    if image_sources is None:
        image_sources = [(job_id, image_mimetype, image_count)] if has_image else []

    # Get Redis client with automatic cleanup
    async with get_redis_client() as redis:
        db = SessionLocal()
        chunk_index = 0
        full_response = ""
        http_client: httpx.AsyncClient | None = None
        whatsapp_client: WhatsAppClient | None = None
        push_client: WhatsAppClient | None = None  # whatsapp_client on a chat surface
        chat_surface = delivers_through_chat_client(client_id, whatsapp_jid)

        try:
            # Close the burst's absorbed jobs at once, so their pollers stop
            # (silently) instead of each sending a late notice. Recovery still
            # re-runs the burst after a crash: consumer._already_finished
            # counts a merged job as finished only once its carrier is.
            await _close_absorbed_jobs(redis, absorbed_job_ids or [], job_id)

            # Step 1: Get conversation history from PostgreSQL
            logger.info(f"[Job {job_id}] Fetching conversation history...")
            # The messages being answered were saved by /chat/enqueue before the
            # job ran; they go to the model as the prompt, so keep them (and
            # anything saved after them) out of the history.
            answered_ids = run_user_message_ids or [user_message_id]
            history = get_conversation_history(
                db,
                whatsapp_jid,
                conversation_type,
                before_message_id=answered_ids[0],
                exclude_ids=answered_ids,
            )
            message_history = format_message_history(history) if history else None
            logger.info(
                f"[Job {job_id}] Retrieved {len(history) if history else 0} messages from history"
            )

            # Step 2: Initialize embedding service
            logger.info(f"[Job {job_id}] Initializing embedding service...")
            embedding_service = create_embedding_service(settings.gemini_api_key)

            # Step 2.5: Initialize HTTP client and WhatsApp client
            # Resolve client_id to pre-configured URL
            whatsapp_base_url = get_whatsapp_client_url(client_id)
            http_client = httpx.AsyncClient(timeout=settings.whatsapp_client_timeout)
            whatsapp_client = create_whatsapp_client(
                http_client=http_client,
                base_url=whatsapp_base_url,
                api_key=get_whatsapp_api_key(client_id),
            )
            logger.info(f"[Job {job_id}] WhatsApp client initialized")
            # An app-surface job (/v1, terminal chat) has no chat client: its
            # reply is only read by its poller, never pushed late to WhatsApp
            # (a key linked to a WhatsApp user runs on that user's JID).
            push_client = whatsapp_client if chat_surface else None

            # Step 2.6: Hand an attached PDF to the PDF stream. Parsing happens in
            # the PDF consumer (streams/pdf_consumer.py), not inline: a Docling
            # parse can take minutes and would block this user's whole stream.
            # The user sees ⏳ now and ✅/❌ on the same message once it's done.
            if has_document and document_id and document_path:
                logger.info(f"[Job {job_id}] Enqueuing document {document_id} for processing")

                if whatsapp_message_id:
                    try:
                        await whatsapp_client.send_reaction(whatsapp_jid, whatsapp_message_id, "⏳")
                        logger.info(f"[Job {job_id}] Sent processing reaction ⏳")
                    except Exception as e:
                        logger.warning(f"[Job {job_id}] Failed to send processing reaction: {e}")

                try:
                    await enqueue_pdf_processing(
                        redis=redis,
                        document_id=document_id,
                        file_path=document_path,
                        whatsapp_jid=whatsapp_jid,
                        job_id=job_id,
                        whatsapp_message_id=whatsapp_message_id,
                        client_id=client_id,
                    )
                    logger.info(f"[Job {job_id}] Document enqueued for processing")
                except Exception as e:
                    logger.error(f"[Job {job_id}] Failed to enqueue document: {e}", exc_info=True)

                    if whatsapp_message_id:
                        try:
                            await whatsapp_client.send_reaction(
                                whatsapp_jid, whatsapp_message_id, "❌"
                            )
                        except Exception as reaction_error:
                            logger.warning(
                                f"[Job {job_id}] Failed to send failure reaction: {reaction_error}"
                            )

                    full_response = f"Sorry, I couldn't process your document '{document_filename}'. Please try uploading it again."

                    # Save response and return early
                    await save_job_chunk(redis, job_id, 0, full_response)
                    save_message(db, whatsapp_jid, "assistant", full_response, conversation_type)
                    await set_job_metadata(
                        redis,
                        job_id,
                        {
                            "user_id": user_id,
                            "whatsapp_jid": whatsapp_jid,
                            "message": message,
                            "conversation_type": conversation_type,
                            "total_chunks": 1,
                            "user_message_id": user_message_id,
                            "error": "document_enqueue_failed",
                        },
                    )
                    await deliver_if_late(
                        redis, push_client, job_id, whatsapp_jid, full_response, enqueued_at_ms
                    )

                    return {
                        "success": False,
                        "job_id": job_id,
                        "error": "document_enqueue_failed",
                    }

                # The document is parsed asynchronously, so the agent cannot read
                # it yet: say so, instead of asking it to analyze content it can't
                # see (which invites a made-up summary).
                message = (
                    f"I have uploaded a document called '{document_filename}'. It is being "
                    "processed in the background and is not searchable yet. Acknowledge it "
                    "briefly: I'll see a ✅ reaction on my message when it's ready (❌ if it "
                    "fails), and then I can ask questions about it."
                )

            # Step 3: Prepare agent dependencies
            agent_deps = AgentDeps(
                db=db,
                user_id=user_id,
                whatsapp_jid=whatsapp_jid,
                # Also the answered messages, so a search never returns them.
                recent_message_ids=[str(msg.id) for msg in history or []] + list(answered_ids),
                embedding_service=embedding_service,
                http_client=http_client,
                whatsapp_client=whatsapp_client,
                current_message_id=whatsapp_message_id,
                client_id=client_id,
                is_group_admin=is_group_admin,
                job_id=job_id,
                user_message_id=user_message_id,
            )

            # Step 4: Format message with sender name for group context
            ai_message = f"{sender_name}: {message}" if sender_name else message

            # Step 5: Get AI response (with optional image for vision)
            logger.info(f"[Job {job_id}] Getting AI response...")

            # Retrieve image data if present (several for an album or a burst)
            images: list[tuple[str, str]] = []
            for source_job_id, source_mimetype, source_count in image_sources:
                images.extend(
                    await get_job_images(redis, source_job_id, source_mimetype, source_count)
                )
            if images:
                logger.info(f"[Job {job_id}] Retrieved {len(images)} image(s) for vision")
            elif image_sources:
                logger.warning(f"[Job {job_id}] Image flag set but no image data found in Redis")

            try:
                async for token in get_ai_response(
                    ai_message,
                    message_history,
                    agent_deps=agent_deps,
                    images=images,
                ):
                    full_response += token
            except (*MODEL_ERRORS, UsageLimitExceeded) as model_error:
                # UsageLimitExceeded: the run hit AGENT_REQUEST_LIMIT (a model
                # looping on tools). Same user-facing fallback as a model error.
                logger.error(f"[Job {job_id}] AI model error: {model_error}", exc_info=True)
                # Deliver the fallback text as a normal COMPLETED job (no "status":
                # "failed"): the clients treat "failed" as an exception and answer
                # it with their OWN error text + ❌, so publishing this text AND
                # failing the job would double both. Here the ❌ comes from us
                # (once) and the client simply delivers the text. Nothing partial
                # has reached the user (chunks are published only on completion).
                # The text is NOT saved to history, so it never replays to the model
                # as a prior assistant turn.
                if whatsapp_message_id:
                    try:
                        await whatsapp_client.send_reaction(whatsapp_jid, whatsapp_message_id, "❌")
                    except Exception as reaction_error:
                        logger.warning(
                            f"[Job {job_id}] Failed to send error reaction: {reaction_error}",
                            exc_info=True,
                        )

                await _delete_images(redis, image_sources)

                await save_job_chunk(redis, job_id, 0, MODEL_ERROR_FALLBACK_TEXT)
                chunk_index = 1
                await set_job_metadata(
                    redis,
                    job_id,
                    {
                        "user_id": user_id,
                        "whatsapp_jid": whatsapp_jid,
                        "message": message,
                        "conversation_type": conversation_type,
                        "total_chunks": chunk_index,
                        "db_message_id": None,
                        "user_message_id": user_message_id,
                        "model_error": True,
                    },
                )
                await deliver_if_late(
                    redis,
                    push_client,
                    job_id,
                    whatsapp_jid,
                    MODEL_ERROR_FALLBACK_TEXT,
                    enqueued_at_ms,
                )
                # After the user-facing steps, and off the event loop: it opens
                # its own DB connection, which can stall while the DB is in trouble.
                await asyncio.to_thread(
                    record_auto_report,
                    "model_error",
                    title=(
                        "Agent hit the request limit"
                        if isinstance(model_error, UsageLimitExceeded)
                        else "AI model failed to answer"
                    ),
                    exc=model_error,
                    user_id=user_id,
                    whatsapp_jid=whatsapp_jid,
                    client_id=client_id,
                    conversation_type=conversation_type,
                    job_id=job_id,
                )

                return {
                    "success": True,
                    "job_id": job_id,
                    "total_chunks": chunk_index,
                    "response_length": len(MODEL_ERROR_FALLBACK_TEXT),
                    "db_message_id": None,
                    "model_error": True,
                }

            # Clean up image data from Redis after processing
            await _delete_images(redis, image_sources)

            # Images first: `![caption](https://…)` directives (and any image a
            # tool queued) are fetched through the SSRF-hardened helper and the
            # delivered ones removed from the text, BEFORE markdown_to_whatsapp
            # would flatten them into links. Never fails the reply.
            # Only on the chat-client path: a web reply keeps its image
            # directives inline as Markdown and its job stores no media.
            reply_media = ReplyMedia(text=full_response, media=[], history_lines=[])
            if chat_surface:
                try:
                    reply_media = await collect_reply_media(
                        redis, job_id, full_response, agent_deps.reply_media
                    )
                except Exception:
                    logger.error(f"[Job {job_id}] Reply image handling failed", exc_info=True)
            full_response = reply_media.text

            # Models answer in Markdown despite the prompt. Convert before the
            # reply is delivered, embedded or saved — saved Markdown would be
            # replayed as history and reinforce the habit. `---` burst lines are
            # left untouched, so the clients still split on them.
            # The web surface (/v1) renders Markdown, so it gets the reply as
            # written (surfaces.py). History gets the converted text, so it stays
            # uniform for a user linked across WhatsApp and the web; only a
            # web-only `web:` user keeps Markdown (`stores_markdown`), or its
            # replayed history would teach the model WhatsApp's `*bold*`.
            delivered = render_reply(
                full_response, renders_markdown(surface_of(client_id, whatsapp_jid))
            )
            if not stores_markdown(whatsapp_jid):
                full_response = markdown_to_whatsapp(full_response)

            # Save complete response as single chunk
            await save_job_chunk(redis, job_id, 0, delivered)
            chunk_index = 1

            logger.info(f"[Job {job_id}] AI response completed.")
            logger.info(f"[Job {job_id}] Full response length: {len(full_response)} characters")

            # History records which images went out, so the model knows later.
            history_text = full_response
            if reply_media.history_lines:
                history_text = "\n".join([full_response, *reply_media.history_lines]).strip()

            # Step 5: Generate embedding for complete assistant response
            assistant_embedding = None
            if embedding_service:
                try:
                    logger.info(f"[Job {job_id}] Generating embedding for assistant response...")
                    assistant_embedding = await embedding_service.generate(history_text)
                    logger.info(f"[Job {job_id}] Embedding generated successfully")
                except Exception as e:
                    logger.error(f"[Job {job_id}] Error generating assistant embedding: {e}")
                    # Continue without embedding - not critical

            # Step 6: Save complete assistant response to PostgreSQL
            logger.info(f"[Job {job_id}] Saving assistant message to database...")
            assistant_msg = save_message(
                db,
                whatsapp_jid,
                "assistant",
                history_text,
                conversation_type,
                embedding=assistant_embedding,
            )
            logger.info(f"[Job {job_id}] Assistant message saved with ID: {assistant_msg.id}")

            # Step 7: Save job metadata to Redis
            await set_job_metadata(
                redis,
                job_id,
                {
                    "user_id": user_id,
                    "whatsapp_jid": whatsapp_jid,
                    "message": message,
                    "conversation_type": conversation_type,
                    "total_chunks": chunk_index,
                    "db_message_id": str(assistant_msg.id),
                    "user_message_id": user_message_id,
                    "media": reply_media.media,
                },
            )
            await deliver_if_late(
                redis, push_client, job_id, whatsapp_jid, full_response, enqueued_at_ms
            )

            logger.info(f"[Job {job_id}] ✅ Completed successfully")

            return {
                "success": True,
                "job_id": job_id,
                "total_chunks": chunk_index,
                "response_length": len(full_response),
                "db_message_id": str(assistant_msg.id),
            }

        except Exception as e:
            logger.error(f"[Job {job_id}] ❌ Error processing chat: {e}", exc_info=True)

            # Save partial response if any
            if full_response:
                logger.info(f"[Job {job_id}] Saving partial response ({len(full_response)} chars)")
                try:
                    save_message(
                        db,
                        whatsapp_jid,
                        "assistant",
                        # Converted here too: an unexpected error can skip the
                        # happy-path conversion, and this row is replayed as history.
                        "[Partial - Error] "
                        + (
                            full_response
                            if stores_markdown(whatsapp_jid)
                            else markdown_to_whatsapp(full_response)
                        ),
                        conversation_type,
                        embedding=None,
                    )
                except Exception as save_error:
                    logger.error(f"[Job {job_id}] Failed to save partial response: {save_error}")

            # Write terminal "failed" metadata so pollers stop waiting immediately
            # instead of timing out at config.polling.maxDurationMs (~120s).
            # Wrapped so a Redis hiccup never shadows the original exception.
            try:
                await set_job_metadata(
                    redis,
                    job_id,
                    {
                        "user_id": user_id,
                        "whatsapp_jid": whatsapp_jid,
                        "message": message,
                        "conversation_type": conversation_type,
                        "total_chunks": chunk_index,
                        "user_message_id": user_message_id,
                        "status": "failed",
                        "error": str(e),
                    },
                )
                logger.info(f"[Job {job_id}] Wrote failed-status metadata")
                # Nobody polls a job this old any more: tell the user it failed.
                await deliver_if_late(
                    redis,
                    push_client,
                    job_id,
                    whatsapp_jid,
                    MODEL_ERROR_FALLBACK_TEXT,
                    enqueued_at_ms,
                )
            except Exception as meta_err:
                logger.error(f"[Job {job_id}] Failed to write failure metadata: {meta_err}")

            # After the failed status is published, off the event loop (see above).
            await asyncio.to_thread(
                record_auto_report,
                "job_crash",
                title="Chat job crashed",
                exc=e,
                user_id=user_id,
                whatsapp_jid=whatsapp_jid,
                client_id=client_id,
                conversation_type=conversation_type,
                job_id=job_id,
            )

            # Re-raise exception
            raise

        finally:
            # Close HTTP client
            if http_client:
                await http_client.aclose()
                logger.info(f"[Job {job_id}] HTTP client closed")
            db.close()
            logger.info(f"[Job {job_id}] Database session closed")


async def _delete_images(redis, image_sources: list[tuple[str, str | None, int]]) -> None:
    for source_job_id, _mimetype, _count in image_sources:
        await delete_job_image(redis, source_job_id)


async def _close_absorbed_jobs(redis, absorbed_job_ids: list[str], carrier_job_id: str) -> None:
    """Mark the jobs merged into this run as finished with nothing to send:
    GET /chat/job answers them `silent`, so their clients stay quiet."""
    for absorbed in absorbed_job_ids:
        try:
            await set_job_metadata(
                redis,
                absorbed,
                {"status": "complete", "merged_into": carrier_job_id, "total_chunks": 0},
            )
        except Exception:
            logger.error(
                f"[Job {carrier_job_id}] Failed to close merged job {absorbed}", exc_info=True
            )
