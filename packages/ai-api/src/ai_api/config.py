import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def get_env_files() -> tuple[Path, ...]:
    """Return env files: root .env first, then local .env.local for overrides."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "docker-compose.yml").exists():
            root_env = parent / ".env"
            break
    else:
        root_env = Path.cwd() / ".env"

    local_env = Path(__file__).resolve().parent.parent.parent.parent / ".env.local"

    files = [f for f in [root_env, local_env] if f.exists()]
    return tuple(files) if files else (".env",)


class Settings(BaseSettings):
    # Required
    database_url: str
    gemini_api_key: str

    # API Authentication
    ai_api_key: str  # Required — app fails to start if not set
    whatsapp_api_key: str  # Required — used to authenticate calls to WhatsApp client
    whatsapp_cloud_api_key: str | None = None  # Falls back to whatsapp_api_key
    telegram_api_key: str | None = None  # Falls back to whatsapp_api_key

    # Optional with defaults
    groq_api_key: str | None = None
    log_level: str = "INFO"

    # User Whitelist
    whitelist_phones: str = ""  # Comma-separated phone numbers/group JIDs (empty = all allowed)

    # CORS
    cors_origins: str = ""  # Comma-separated allowed origins

    # Rate Limiting
    rate_limit_global: int = 30  # requests per minute
    rate_limit_expensive: int = 5  # requests per minute for expensive ops

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None

    # Queue
    arq_max_jobs: int = 50
    arq_job_timeout: int = 120
    arq_poll_delay: float = 0.1
    arq_keep_result: int = 3600
    queue_chunk_ttl: int = 3600
    queue_per_user_max_jobs: int = 1

    # History
    history_limit_private: int = 20
    history_limit_group: int = 30

    # Token Management
    max_context_tokens: int = 50000
    min_recent_messages: int = 5

    # Semantic Search
    semantic_search_limit: int = 5
    semantic_similarity_threshold: float = 0.7
    semantic_context_window: int = 3

    # Knowledge Base
    kb_upload_dir: str = "/tmp/knowledge_base"
    kb_max_file_size_mb: int = 50
    kb_max_batch_size_mb: int = 500
    kb_search_limit: int = 5
    kb_similarity_threshold: float = 0.7
    kb_max_chunk_tokens: int = 512

    # PDF Processing Timeouts
    # Outer wrapper for the entire pipeline. Must be > llamaparse_timeout_seconds
    # so the inner parser timeout produces its user-friendly error first.
    kb_processing_timeout_seconds: int = 360
    # Local Docling parsing timeout. Accepts the legacy KB_DOCLING_TIMEOUT_SECONDS
    # env name with a one-time deprecation warning.
    kb_parse_timeout_seconds: int = Field(
        default=180,
        validation_alias=AliasChoices("kb_parse_timeout_seconds", "kb_docling_timeout_seconds"),
    )
    kb_embedding_timeout_seconds: int = 10  # Max time per embedding API call (10 seconds)
    kb_embedding_batch_timeout_seconds: int = 240  # Max time for all embeddings (4 minutes)

    # PDF Parser Selection
    # auto: prefer LlamaParse when LLAMA_CLOUD_API_KEY is set, fall back to Docling
    # llamaparse: always use LlamaParse (no fallback)
    # docling: always use Docling (requires `uv sync --extra docling`)
    pdf_parser: Literal["auto", "llamaparse", "docling"] = "auto"

    # LlamaParse (hosted PDF parser — https://cloud.llamaindex.ai)
    llama_cloud_api_key: str | None = None
    llamaparse_tier: Literal["fast", "cost_effective", "agentic", "agentic_plus"] = "cost_effective"
    llamaparse_timeout_seconds: int = 240

    # Conversation-scoped documents
    conversation_pdf_ttl_hours: int = 24
    cleanup_interval_minutes: int = 15

    # Core Memory
    core_memory_max_length: int = 2000  # Max characters for the entire core memory document

    # Speech-to-Text
    # Provider selection: auto | groq | whisper
    #   auto    - prefer Groq when GROQ_API_KEY is set; fall back to self-hosted
    #             Whisper (WHISPER_BASE_URL) on recoverable errors. Also uses
    #             self-hosted alone when only WHISPER_BASE_URL is set.
    #   groq    - always use Groq (no fallback)
    #   whisper - always use self-hosted Whisper (no fallback)
    stt_provider: Literal["auto", "groq", "whisper"] = "auto"
    # Groq-specific Whisper model. Paired with `whisper_model` (self-hosted) for symmetry.
    groq_stt_model: str = "whisper-large-v3"
    stt_max_file_size_mb: int = 25
    stt_supported_formats: str = "mp3,mp4,mpeg,mpga,m4a,wav,webm,ogg,flac"

    # Self-hosted Whisper (optional; opt-in via `docker compose --profile whisper up -d`)
    # Any server exposing OpenAI-compatible POST /v1/audio/transcriptions works.
    # Default container: ghcr.io/speaches-ai/speaches:latest-cpu
    whisper_base_url: str | None = None
    whisper_model: str = "Systran/faster-distil-whisper-large-v3"
    whisper_timeout_seconds: int = 120

    # Text-to-Speech
    tts_model: str = "gemini-2.5-flash-preview-tts"
    tts_default_voice: str = "Kore"
    tts_max_text_length: int = 5000

    # Chat Clients
    whatsapp_client_url: str = "http://localhost:3001"
    whatsapp_cloud_client_url: str = "http://localhost:3002"
    telegram_client_url: str = "http://localhost:3003"
    whatsapp_client_timeout: int = 30

    # External APIs
    jina_api_key: str | None = None  # Optional, for higher rate limits (500 vs 20 RPM)

    # Database Connection Pooling
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    db_pool_recycle: int = 3600
    db_pool_pre_ping: bool = True
    db_echo_pool: bool = False

    model_config = SettingsConfigDict(
        env_file=get_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _check_timeout_ordering(self) -> "Settings":
        if self.llamaparse_timeout_seconds >= self.kb_processing_timeout_seconds:
            raise ValueError(
                "LLAMAPARSE_TIMEOUT_SECONDS must be strictly less than "
                "KB_PROCESSING_TIMEOUT_SECONDS so the inner parser timeout fires "
                "before the outer wrapper. Got "
                f"llamaparse={self.llamaparse_timeout_seconds}, "
                f"processing={self.kb_processing_timeout_seconds}."
            )
        if "KB_DOCLING_TIMEOUT_SECONDS" in os.environ:
            logging.getLogger("ai-api").warning(
                "KB_DOCLING_TIMEOUT_SECONDS is deprecated; rename to "
                "KB_PARSE_TIMEOUT_SECONDS. The legacy name still works for now."
            )
        return self

    @model_validator(mode="after")
    def _check_stt_provider_config(self) -> "Settings":
        if self.stt_provider == "groq" and not self.groq_api_key:
            raise ValueError(
                "STT_PROVIDER=groq but GROQ_API_KEY is not set. Set the key or "
                "use STT_PROVIDER=auto (falls back) or STT_PROVIDER=whisper."
            )
        if self.stt_provider == "whisper" and not self.whisper_base_url:
            raise ValueError(
                "STT_PROVIDER=whisper but WHISPER_BASE_URL is not set. Start the "
                "self-hosted container (`docker compose --profile whisper up -d`) "
                "and set WHISPER_BASE_URL."
            )
        return self


settings = Settings()


def get_whatsapp_client_url(client_id: str | None) -> str:
    """Resolve client_id to a pre-configured chat client URL."""
    if client_id == "cloud":
        return settings.whatsapp_cloud_client_url
    if client_id == "telegram":
        return settings.telegram_client_url
    return settings.whatsapp_client_url


def get_whatsapp_api_key(client_id: str | None) -> str:
    """Resolve client_id to the appropriate chat client API key."""
    if client_id == "cloud" and settings.whatsapp_cloud_api_key:
        return settings.whatsapp_cloud_api_key
    if client_id == "telegram" and settings.telegram_api_key:
        return settings.telegram_api_key
    return settings.whatsapp_api_key


whitelist_set: set[str] = (
    {p.strip() for p in settings.whitelist_phones.split(",") if p.strip()}
    if settings.whitelist_phones
    else set()
)
