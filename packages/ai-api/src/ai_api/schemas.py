from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ImageInput(BaseModel):
    data: str = Field(..., description="Base64-encoded image data")
    mimetype: str = Field(..., description="Image MIME type (e.g., image/jpeg)")


class ChatRequest(BaseModel):
    whatsapp_jid: str = Field(..., description="User's WhatsApp JID")
    message: str = Field(..., description="User's message text")
    conversation_type: Literal["private", "group"] = Field(
        ..., description="Conversation type (private or group)"
    )
    sender_jid: str | None = Field(None, description="Sender JID in group chats")
    sender_name: str | None = Field(None, description="Sender name in group chats")
    profile_name: str | None = Field(
        None,
        description="Display name of the conversation (contact's pushName, or group subject)",
    )
    whatsapp_message_id: str | None = Field(None, description="WhatsApp message ID for reactions")
    image_data: str | None = Field(None, description="Base64-encoded image data for vision")
    image_mimetype: str | None = Field(None, description="Image MIME type (e.g., image/jpeg)")
    images: list[ImageInput] | None = Field(
        None,
        max_length=10,
        description="Several images sent together (an album), answered in one run; "
        "added after image_data when both are set",
    )
    document_data: str | None = Field(None, description="Base64-encoded PDF document data")
    document_mimetype: str | None = Field(
        None, description="Document MIME type (e.g., application/pdf)"
    )
    document_filename: str | None = Field(None, description="Original document filename")
    is_group_admin: bool | None = Field(
        None, description="Whether the sender is a group admin (groups only)"
    )
    phone: str | None = Field(None, description="E.164 phone number (e.g., +5491126726818)")
    whatsapp_lid: str | None = Field(None, description="WhatsApp LID if known")
    client_id: Literal["baileys", "cloud", "telegram"] | None = Field(
        None,
        description="Chat client identifier for routing callbacks",
    )


class ChatResponse(BaseModel):
    response: str


class SaveMessageRequest(BaseModel):
    """Request to save message without generating AI response"""

    whatsapp_jid: str = Field(..., description="User's WhatsApp JID")
    message: str = Field(..., description="Message text to save")
    conversation_type: Literal["private", "group"] = Field(
        ..., description="Conversation type (private or group)"
    )
    sender_jid: str | None = Field(None, description="Sender JID in group chats")
    sender_name: str | None = Field(None, description="Sender name in group chats")
    profile_name: str | None = Field(
        None,
        description="Display name of the conversation (contact's pushName, or group subject)",
    )
    whatsapp_message_id: str | None = Field(None, description="WhatsApp message ID for reactions")
    phone: str | None = Field(None, description="E.164 phone number (e.g., +5491126726818)")
    whatsapp_lid: str | None = Field(None, description="WhatsApp LID if known")


class RevokeMessageRequest(BaseModel):
    """A message was deleted for everyone on the platform: forget the saved copy."""

    whatsapp_jid: str = Field(..., description="Conversation JID (group or private)")
    whatsapp_message_id: str = Field(..., description="Platform id of the deleted message")
    sender_jid: str | None = Field(
        None, description="Who deleted it (group participant JID, resolved like on save)"
    )
    is_group_admin: bool | None = Field(
        None, description="Whether the deleter is a group admin (may delete others' messages)"
    )
    phone: str | None = Field(None, description="E.164 phone of a private chat, if known")


class EditMessageRequest(BaseModel):
    """A message was edited on the platform: update the saved copy and its embedding."""

    whatsapp_jid: str = Field(..., description="Conversation JID (group or private)")
    whatsapp_message_id: str = Field(..., description="Platform id of the edited message")
    message: str = Field(..., min_length=1, description="The new text")
    sender_jid: str | None = Field(
        None, description="Who edited it (group participant JID, resolved like on save)"
    )
    phone: str | None = Field(None, description="E.164 phone of a private chat, if known")


class MessageEventResponse(BaseModel):
    """How many saved rows a revoke or edit changed (0 = not found or not allowed)."""

    changed: int


class WhitelistResponse(BaseModel):
    """The effective whitelist, unparsed (GET /chat/whitelist)."""

    whitelist_phones: str = Field(
        ..., description="Comma-separated entries, as WHITELIST_PHONES; empty = disabled"
    )


class UploadPDFResponse(BaseModel):
    """Response after uploading a PDF to the knowledge base"""

    document_id: str = Field(..., description="UUID of the uploaded document")
    filename: str = Field(..., description="Original filename of the uploaded PDF")
    status: str = Field(
        ..., description="Processing status (pending, processing, completed, failed)"
    )
    message: str = Field(..., description="Human-readable status message")


class FileUploadResult(BaseModel):
    """Result for a single file in a batch upload"""

    filename: str = Field(..., description="Original filename")
    status: Literal["accepted", "rejected"] = Field(..., description="Upload status")
    document_id: str | None = Field(None, description="UUID if accepted")
    message: str | None = Field(None, description="Success message")
    error: str | None = Field(None, description="Error message if rejected")


class BatchUploadResponse(BaseModel):
    """Response for batch PDF upload"""

    total_files: int = Field(..., description="Total number of files in batch")
    accepted: int = Field(..., description="Number of files accepted for processing")
    rejected: int = Field(..., description="Number of files rejected")
    results: list[FileUploadResult] = Field(..., description="Per-file results")
    message: str = Field(..., description="Overall batch status message")


class TranscribeResponse(BaseModel):
    """Audio transcription response - TEXT ONLY"""

    transcription: str = Field(..., description="Transcribed text from audio")
    message: str = Field(..., description="Status message")
    # NOTE: No ai_response field - client will call /chat/enqueue separately


class TTSRequest(BaseModel):
    """Text-to-speech synthesis request"""

    text: str = Field(..., description="Text to convert to speech")
    whatsapp_jid: str | None = Field(
        None, description="Optional JID to fetch user language preferences"
    )
    format: Literal["ogg", "mp3", "wav", "flac"] = Field(
        "ogg", description="Output audio format (default: ogg for WhatsApp compatibility)"
    )


class CommandResponse(BaseModel):
    """Response for command execution (e.g., /settings, /tts on)"""

    is_command: bool = Field(True, description="Always true for command responses")
    response: str = Field(..., description="Command result message")


class LinkPhoneRequest(BaseModel):
    """Telegram phone-sharing auto-link request (POST /chat/link-phone).

    `contact_user_id` is the `user_id` on the shared `Contact`, and
    `sender_user_id` is `ctx.from.id`. The server requires them to be equal —
    a user can share anyone's contact card, and a card for someone else carries
    that person's id, so this is what shows the phone belongs to the sender.
    """

    whatsapp_jid: str = Field(..., description="Telegram JID of the caller (tg:<chat_id>)")
    phone: str | None = Field(None, description="Phone number from the shared contact")
    contact_user_id: int | None = Field(
        None, description="Telegram user_id carried on the shared contact card"
    )
    sender_user_id: int | None = Field(
        None, description="Telegram user_id of the sender (ctx.from.id)"
    )


class PreferencesResponse(BaseModel):
    """User preferences"""

    tts_enabled: bool = Field(..., description="Whether TTS is enabled")
    tts_language: str = Field(..., description="TTS language code (e.g., 'en', 'es')")
    stt_language: str | None = Field(None, description="STT language code, null for auto-detect")


class UpdatePreferencesRequest(BaseModel):
    """Request to update preferences"""

    tts_enabled: bool | None = Field(None, description="Enable/disable TTS")
    tts_language: str | None = Field(None, description="TTS language code")
    stt_language: str | None = Field(None, description="STT language code, 'auto' converts to null")


# --- Admin (management dashboard) ---


class PromptResponse(BaseModel):
    """The base prompt the model gets: code sections with the fresh overrides applied."""

    content: str = Field(..., description="The base prompt the model gets now")
    is_overridden: bool = Field(
        ..., description="True if a whole-prompt or section override is applied"
    )
    default_length: int = Field(..., description="Character length of the composed code default")
    updated_at: datetime | None = Field(None, description="When an applied override was saved")
    stale: bool = Field(
        False,
        description="A whole-prompt override exists but is ignored: the code default changed",
    )
    sections_overridden: int = Field(0, description="Section overrides currently applied")


class PromptBlock(BaseModel):
    """One block of the instructions, in the order the model gets them."""

    key: str = Field(
        ...,
        description='"section:<key>", "hook:<function>", or "whole_override"',
    )
    kind: Literal["section", "hook"]
    title: str
    content: str = Field(..., description="Exactly this block's text ('' = nothing this chat)")
    source: Literal["code", "override"]
    editable: bool
    stale_override: bool = Field(
        ..., description="An override exists but the code default changed; the code text applies"
    )


class EffectivePromptResponse(BaseModel):
    """The instructions the model gets, built by the agent's own hooks."""

    content: str = Field(..., description="The whole instructions string the model receives")
    length: int
    chat: Literal["private", "group"]
    surface: Literal["whatsapp", "telegram", "web"]
    user_id: str | None = None
    prompt_code_hash: str = Field(..., description="This api process's prompt-code fingerprint")
    generated_at: datetime
    blocks: list[PromptBlock]


class PromptSectionItem(BaseModel):
    """One editable section of the base prompt."""

    key: str
    title: str
    default: str = Field(..., description="Code default in this deploy")
    default_hash: str = Field(..., description="sha256 hex of default; send it back on PUT")
    override: str | None = Field(None, description="Saved operator text, kept even when stale")
    override_base_hash: str | None = None
    override_updated_at: datetime | None = None
    status: Literal["default", "override", "stale"]
    effective: str = Field(
        ..., description="This section's text now; the model gets it only when applied is true"
    )
    applied: bool = Field(
        ..., description="False while a fresh whole override replaces every section, or orphan"
    )
    orphan: bool = Field(
        False, description="A stored override for a key the code no longer has; never applied"
    )


class WholeOverrideState(BaseModel):
    """The legacy whole-prompt override (PUT /admin/prompt)."""

    active: bool
    stale: bool
    updated_at: datetime | None = None


class PromptSectionsResponse(BaseModel):
    sections: list[PromptSectionItem]
    whole_override: WholeOverrideState


class UpdatePromptSectionRequest(BaseModel):
    """Override one section; base_hash is the default_hash the editor showed."""

    content: str = Field(..., max_length=100_000, description="'' leaves the section out")
    base_hash: str = Field(..., min_length=1, max_length=64)


class UpdatePromptRequest(BaseModel):
    """Request to set the whole-prompt override (applies while the code default is unchanged)."""

    content: str = Field(
        ...,
        min_length=1,
        max_length=100_000,
        description="New system prompt (non-empty; capped at 100 KB)",
    )


class SettingProcessValue(BaseModel):
    process: str
    instance: str
    value: Any
    started_at: datetime


class SettingItem(BaseModel):
    """A single configurable setting with its effective value and metadata."""

    key: str
    value: Any = Field(..., description="Effective value (override if set, else env default)")
    default: Any = Field(..., description="Env/code default value")
    source: Literal["default", "override"] = Field(..., description="Where the value comes from")
    hot: bool = Field(..., description="True if overridable at runtime; False = needs restart")
    category: str
    type: Literal["str", "int", "float", "bool"]
    description: str
    choices: list[str] | None = Field(None, description="Allowed values, if constrained")
    secret: bool = Field(False, description="True if the value is masked")
    processes: list[SettingProcessValue] = Field(
        default_factory=list,
        description="The value each live process uses ([] without heartbeats)",
    )
    restart_pending: bool = Field(
        False,
        description="Restart-only setting whose live processes disagree (the older one is stale)",
    )


class ProcessInfo(BaseModel):
    """A live api/worker process, from its Redis heartbeat."""

    process: str = Field(..., description='"api" or "worker" today; an open string')
    instance: str = Field(..., description="Hostname (the container id in Docker)")
    pid: int
    started_at: datetime
    last_seen_at: datetime
    prompt_code_hash: str


class ProcessesResponse(BaseModel):
    processes: list[ProcessInfo]
    prompt_code_mismatch: bool = Field(..., description="Live processes run different prompt code")


class SettingsResponse(BaseModel):
    """All registered settings."""

    settings: list[SettingItem]


class UpdateSettingsRequest(BaseModel):
    """Request to set one or more runtime-setting overrides."""

    overrides: dict[str, Any] = Field(..., description="Map of setting key → new value")


class UserSummary(BaseModel):
    """A conversation/user row for the dashboard's user list."""

    whatsapp_jid: str
    name: str | None = None
    phone: str | None = None
    whatsapp_lid: str | None = None
    conversation_type: str
    message_count: int
    last_message_at: datetime | None = None


class UsersResponse(BaseModel):
    """Paginated list of users."""

    users: list[UserSummary]
    total: int
    limit: int
    offset: int


class MessageItem(BaseModel):
    """A single conversation message (read-only view)."""

    role: str
    content: str
    sender_name: str | None = None
    timestamp: datetime


class MessagesResponse(BaseModel):
    """Paginated conversation history for one user (newest first)."""

    whatsapp_jid: str
    messages: list[MessageItem]
    total: int
    limit: int
    offset: int


class OverviewResponse(BaseModel):
    """High-level counts for the dashboard landing page."""

    users: int
    messages: int
    knowledge_base_documents: int
    open_bug_reports: int = 0
    disabled_scheduled_tasks: int = 0


class WhatsAppStatusResponse(BaseModel):
    """Baileys WhatsApp link status + pairing QR (proxied from the client)."""

    status: Literal["connecting", "qr", "connected", "disconnected", "unavailable"] = Field(
        ..., description="Link lifecycle; 'unavailable' = no Baileys client reachable"
    )
    connected: bool = Field(..., description="True when the session is linked")
    qr: str | None = Field(None, description="Raw pairing QR payload; present only while 'qr'")
    qr_generated_at: str | None = Field(None, description="ISO timestamp the QR was issued")


class WhatsAppLogoutResponse(BaseModel):
    """Result of forcing a Baileys logout / re-pair."""

    success: bool = Field(..., description="True when the logout + re-init completed")
    detail: str = Field(..., description="Human-readable status message")


# --- Broadcasts (/admin/broadcasts) ---
# The Literal enums below are a FleetView contract: its Zod schemas mirror them
# as closed enums, so adding a value breaks the dashboard. New fields are safe.

BroadcastAudience = Literal["private", "groups", "all"]
BroadcastPlatform = Literal["baileys", "cloud", "telegram"]
BroadcastStatus = Literal["queued", "running", "paused", "completed", "cancelled"]
BroadcastRecipientStatus = Literal["pending", "sent", "failed", "skipped"]
#: Why a running lane is waiting (streams/broadcast_consumer.py ``WAIT_*``).
BroadcastWaitReason = Literal[
    "send_window", "daily_cap", "timelock", "batch_pause", "delay", "disconnected"
]


class BroadcastPreviewRequest(BaseModel):
    """Who a broadcast would reach, without creating it."""

    audience: BroadcastAudience = Field(
        "private", description="'private' (1:1 chats), 'groups', or 'all'"
    )
    platforms: list[BroadcastPlatform] | None = Field(
        None,
        min_length=1,
        description="Platforms to send through; omitted = every reachable chat client",
    )


class BroadcastCreateRequest(BroadcastPreviewRequest):
    """Create (and queue) a broadcast."""

    text: str = Field(
        ...,
        min_length=1,
        max_length=3500,
        description="Message text (with the ≤500-char footer it stays under Telegram's 4096 cap)",
    )
    idempotency_key: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        description="Repeat-safe key: re-sending it returns the existing broadcast",
    )


class BroadcastPlatformPreview(BaseModel):
    platform: BroadcastPlatform
    recipients: int = Field(..., description="Chats that would be sent to on this platform")
    skipped_cloud_window: int = Field(
        0, description="Cloud API chats skipped: last message older than the 24h window"
    )


class BroadcastPreviewResponse(BaseModel):
    audience: BroadcastAudience
    platforms: list[BroadcastPlatform]
    total_recipients: int = Field(..., description="Chats that would be sent to")
    per_platform: list[BroadcastPlatformPreview]
    opted_out: int
    not_whitelisted: int
    no_selected_platform: int = Field(
        ..., description="Chats with no route through the selected platforms"
    )
    skipped_cloud_window: int
    skipped_unknown_client: int = Field(
        0,
        description=(
            "Chats from before client tracking whose WhatsApp client (Baileys or Cloud) is "
            "unknown; skipped while a Cloud client is deployed, until they write again"
        ),
    )
    skipped_duplicate: int = Field(
        0,
        description=(
            "Extra chat rows of a person who already gets the message (e.g. an early "
            "@lid row next to the phone-number row); never sent"
        ),
    )
    estimated_baileys_seconds: int = Field(
        ..., description="Rough time to finish the Baileys lane under the current pacing"
    )


class BroadcastPlatformAvailability(BaseModel):
    platform: BroadcastPlatform
    reachable: bool


class BroadcastPlatformsResponse(BaseModel):
    platforms: list[BroadcastPlatformAvailability]


class BroadcastCounts(BaseModel):
    total: int = 0
    pending: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0


class BroadcastPlatformCounts(BroadcastCounts):
    platform: BroadcastPlatform
    next_send_at: datetime | None = Field(
        None,
        description=(
            "UTC time the lane's next message is due while it waits; null while it sends, "
            "once it finished, or when the broadcast isn't running"
        ),
    )
    waiting_for: BroadcastWaitReason | None = Field(
        None,
        description=(
            "Why the lane waits: 'send_window', 'daily_cap', 'timelock' (WhatsApp's limit on "
            "new contacts), 'batch_pause', 'delay' (gap between messages), 'disconnected'"
        ),
    )


class BroadcastResponse(BaseModel):
    id: str
    scheduled_send_id: str | None = Field(
        None, description="The scheduled send whose run this broadcast is; null = manual"
    )
    scheduled_send_run_id: str | None = None
    text: str
    footer: str
    audience: BroadcastAudience
    platforms: list[BroadcastPlatform]
    status: BroadcastStatus
    pause_reason: str | None = Field(
        None,
        description="Why it paused: 'manual', 'consecutive_failures', 'client_disconnected'",
    )
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    counts: BroadcastCounts
    per_platform: list[BroadcastPlatformCounts]


class BroadcastsResponse(BaseModel):
    broadcasts: list[BroadcastResponse]
    total: int
    limit: int
    offset: int


class BroadcastRecipientItem(BaseModel):
    address: str
    name: str | None = None
    platform: BroadcastPlatform
    status: BroadcastRecipientStatus
    error_code: str | None = Field(
        None,
        description=(
            "Failure/skip reason, e.g. not_on_whatsapp, blocked, invalid_address, "
            "rejected, unknown (may have been delivered; never retried), http_429, "
            "transport_error, opted_out, cloud_window, user_deleted, unknown_client, "
            "not_whitelisted"
        ),
    )
    attempts: int
    sent_at: datetime | None = None


class BroadcastRecipientsResponse(BaseModel):
    recipients: list[BroadcastRecipientItem]
    total: int
    limit: int
    offset: int


# --- Scheduled sends (/admin/scheduled-sends, "Envios programados") ---
# A FleetView contract like the broadcasts above: these Literals are closed
# enums in its Zod schemas. New fields are safe; new enum values are not.

ScheduledSendContentType = Literal["text", "prompt"]
ScheduledSendAudience = Literal["private", "groups", "all", "chats"]
ScheduledSendStatus = Literal["active", "paused", "finished", "cancelled"]
ScheduledSendScheduleKind = Literal["once", "daily", "weekly", "monthly"]
ScheduledSendRunStatus = Literal[
    "preparing", "delivering", "completed", "failed", "missed", "cancelled"
]
ScheduledSendRunTrigger = Literal["schedule", "manual"]
ScheduledSendChatType = Literal["group", "private"]


class ScheduledSendSchedule(BaseModel):
    """When it fires, in the send's time zone. Fields by kind:

    ``once``: ``run_at`` (naive = the bot's time zone; future, ≤ 1 year) ·
    ``daily``: ``time`` · ``weekly``: ``time`` + ``weekdays`` (0 = Sunday) ·
    ``monthly``: ``time`` + ``day_of_month`` (1-28).
    """

    kind: ScheduledSendScheduleKind
    run_at: datetime | None = None
    time: str | None = Field(None, description="HH:MM, 24 h")
    weekdays: list[int] | None = Field(None, description="0-6, cron numbering (0 = Sunday)")
    day_of_month: int | None = Field(None, ge=1, le=28)


class ScheduledSendFields(BaseModel):
    """The editable fields (PATCH takes any subset)."""

    name: str | None = Field(None, min_length=1, max_length=120)
    content_type: ScheduledSendContentType | None = None
    content: str | None = Field(
        None, min_length=1, max_length=3500, description="Text (≤ 3500) or prompt (≤ 2000)"
    )
    audience: ScheduledSendAudience | None = None
    chat_jids: list[str] | None = Field(
        None, max_length=500, description="Audience 'chats' only: the chats to send to"
    )
    regions: list[str] | None = Field(
        None,
        max_length=50,
        description="Not supported by this bot (no regions): null or [] only, else 422",
    )
    platforms: list[BroadcastPlatform] | None = Field(
        None, min_length=1, description="null = every chat client reachable at run time"
    )
    schedule: ScheduledSendSchedule | None = None
    status: Literal["active", "paused"] | None = None
    created_by: str | None = Field(None, max_length=120)


class ScheduledSendCreateRequest(ScheduledSendFields):
    name: str = Field(..., min_length=1, max_length=120)
    content_type: ScheduledSendContentType
    content: str = Field(..., min_length=1, max_length=3500)
    audience: ScheduledSendAudience = "private"
    schedule: ScheduledSendSchedule
    idempotency_key: str | None = Field(None, min_length=1, max_length=128)


class ScheduledSendRunItem(BaseModel):
    id: str
    scheduled_send_id: str
    scheduled_for: datetime
    trigger: ScheduledSendRunTrigger
    status: ScheduledSendRunStatus
    error: str | None = Field(
        None,
        description=(
            "Open string: generation_failed, no_recipients, expired, missed, "
            "clients_unreachable, send_cancelled, prepare_error"
        ),
    )
    broadcast_id: str | None = None
    counts: BroadcastCounts
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ScheduledSendRunText(BaseModel):
    region: str | None = Field(None, description="Always null: this bot has no regions")
    chat_type: ScheduledSendChatType
    text: str | None = None
    error: str | None = None


class ScheduledSendRunDetail(ScheduledSendRunItem):
    texts: list[ScheduledSendRunText]


class ScheduledSendResponse(BaseModel):
    id: str
    number: int = Field(..., description="Stable code people use in /broadcast off <number>")
    name: str
    content_type: ScheduledSendContentType
    content: str
    audience: ScheduledSendAudience
    chat_jids: list[str] | None = None
    regions: list[str] | None = Field(None, description="Always null: this bot has no regions")
    platforms: list[BroadcastPlatform] | None = None
    schedule: ScheduledSendSchedule
    timezone: str
    schedule_description: str = Field(..., description="e.g. 'every Monday at 08:00'")
    status: ScheduledSendStatus
    next_run_at: datetime | None = None
    last_run: ScheduledSendRunItem | None = None
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


class WeeklyRecapSummary(BaseModel):
    """FleetView's recap card. This bot has no weekly recap: always a disabled summary."""

    enabled: bool
    weekday: int
    hour: int
    minute: int
    timezone: str
    default_prompt: str
    groups: int
    subscribers: int


class ScheduledSendsResponse(BaseModel):
    scheduled_sends: list[ScheduledSendResponse]
    total: int
    limit: int
    offset: int
    weekly_recap: WeeklyRecapSummary
    regions: list[str] = Field(
        default_factory=list, description="Region names a send accepts: none on this bot"
    )
    region_filter: bool = Field(
        False, description="Whether sends can filter by region (false: this bot has no regions)"
    )
    timezone: str = Field(..., description="The zone a new send is created in (bot timezone)")


class ScheduledSendRunsResponse(BaseModel):
    runs: list[ScheduledSendRunItem]
    total: int
    limit: int
    offset: int


class ScheduledSendPreviewRequest(ScheduledSendFields):
    """Either ``scheduled_send_id`` (a saved send, fields here override it) or a full draft."""

    scheduled_send_id: str | None = None
    generate: bool = Field(False, description="Run the agent once for a sample (prompt sends)")
    sample_region: str | None = Field(None, description="Ignored: this bot has no regions")
    sample_chat_type: ScheduledSendChatType | None = None


class ScheduledSendSample(BaseModel):
    region: str | None = None
    chat_type: ScheduledSendChatType
    text: str | None = None
    error: str | None = None
    prompt: str


class ScheduledSendPreviewResponse(BaseModel):
    audience: ScheduledSendAudience
    platforms: list[BroadcastPlatform]
    total_recipients: int
    per_platform: list[BroadcastPlatformPreview]
    opted_out: int
    not_whitelisted: int
    no_selected_platform: int
    skipped_cloud_window: int
    skipped_unknown_client: int = 0
    skipped_duplicate: int = 0
    estimated_baileys_seconds: int
    unknown_chats: int = Field(0, description="chat_jids with no chat row")
    opted_out_campaign: int = Field(0, description="Chats that stopped this send")
    estimated_agent_runs: int = 0
    agent_runs_upper_bound: bool = False
    next_runs: list[datetime] = Field(default_factory=list)
    warnings: list[str] = Field(
        default_factory=list,
        description="Open strings: cloud_recurring, exceeds_period, exceeds_daily_cap",
    )
    sample: ScheduledSendSample | None = None


# --- Bug reports (/admin/bug-reports) ---
# BugReportSource and BugReportStatus are a FleetView contract (closed enums in
# its Zod schemas): adding a value breaks the dashboard. `category` is open.

BugReportSource = Literal["user", "agent", "model_error", "job_crash", "pdf_failure"]
BugReportStatus = Literal["open", "resolved", "ignored"]


class BugReportSummary(BaseModel):
    id: str
    source: BugReportSource
    status: BugReportStatus
    category: str | None = Field(
        None,
        description="Open string: user_request, wrong_answer, user_complaint, tool_failure, other",
    )
    title: str
    whatsapp_jid: str | None = None
    user_name: str | None = None
    client_id: str | None = None
    conversation_type: str | None = None
    error_type: str | None = None
    occurrences: int
    created_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None = None


class BugReportContextMessage(BaseModel):
    role: str
    sender: str | None = None
    content: str
    at: str | None = Field(None, description="Naive-UTC ISO timestamp")


class BugReportDetail(BugReportSummary):
    description: str
    job_id: str | None = None
    document_id: str | None = None
    models: str | None = Field(None, description="Model chain configured when it was filed")
    error_detail: str | None = Field(None, description="Exception and traceback (operator-only)")
    context: list[BugReportContextMessage] | None = Field(
        None, description="The chat's last messages when filed; null after /clean"
    )
    resolution_note: str | None = None


class BugReportsResponse(BaseModel):
    reports: list[BugReportSummary]
    total: int
    limit: int
    offset: int
    counts_by_status: dict[BugReportStatus, int]


class BugReportUpdateRequest(BaseModel):
    status: BugReportStatus
    resolution_note: str | None = Field(None, max_length=4000)


# --- Scheduled tasks (/admin/scheduled-tasks) ---
# ScheduledTaskStatus, ScheduledTaskKind and TaskRunStatus become a FleetView
# contract once it mirrors them (closed enums): adding a value would break it.
# last_error / error_code / disabled_reason are open strings.

ScheduledTaskStatus = Literal["active", "completed", "cancelled", "disabled"]
ScheduledTaskKind = Literal["reminder", "agent"]
TaskRunStatus = Literal[
    "dispatched",
    "running",
    "ready",
    "sending",
    "sent",
    "silent",
    "skipped",
    "failed",
    "unknown",
]


class ScheduledTaskSummary(BaseModel):
    id: str
    status: ScheduledTaskStatus
    kind: ScheduledTaskKind
    text: str
    schedule: str = Field(
        ..., description="Normalised human description, e.g. 'every day at 09:00'"
    )
    timezone: str
    whatsapp_jid: str = Field(..., description="The chat it delivers to")
    user_name: str | None = None
    client_id: str | None = None
    conversation_type: str | None = None
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: TaskRunStatus | None = None
    last_error: str | None = None
    disabled_reason: str | None = None
    consecutive_failures: int
    run_count: int
    created_at: datetime


class TaskRunItem(BaseModel):
    id: str
    scheduled_for: datetime
    status: TaskRunStatus
    error_code: str | None = None
    late_seconds: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ScheduledTaskDetail(ScheduledTaskSummary):
    cron_expr: str | None = None
    interval_seconds: int | None = None
    run_at: datetime | None = None
    runs: list[TaskRunItem] = Field(default_factory=list, description="Latest runs, newest first")


class ScheduledTasksResponse(BaseModel):
    tasks: list[ScheduledTaskSummary]
    total: int
    limit: int
    offset: int
    counts_by_status: dict[ScheduledTaskStatus, int]


class ScheduledTaskUpdateRequest(BaseModel):
    status: Literal["active", "cancelled"] = Field(
        ..., description="active re-enables a disabled task; cancelled stops it"
    )


# --- Public Bot Contact ---


class WhatsAppContact(BaseModel):
    number: str = Field(..., description="E.164 digits without '+' (e.g. 5561999999999)")
    url: str = Field(..., description="Direct chat URL (e.g. https://wa.me/5561999999999)")


class TelegramContact(BaseModel):
    username: str = Field(..., description="Telegram username without '@' (e.g. curupira_bot)")
    url: str = Field(..., description="Direct chat URL (e.g. https://t.me/curupira_bot)")


class ContactResponse(BaseModel):
    whatsapp: WhatsAppContact | None = None
    telegram: TelegramContact | None = None
