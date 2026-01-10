from pathlib import Path

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

    # Optional with defaults
    groq_api_key: str | None = None
    log_level: str = "INFO"

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

    # Conversation-scoped documents
    conversation_pdf_ttl_hours: int = 24

    # Speech-to-Text
    stt_model: str = "whisper-large-v3"
    stt_max_file_size_mb: int = 25
    stt_supported_formats: str = "mp3,mp4,mpeg,mpga,m4a,wav,webm,ogg,flac"

    # Text-to-Speech
    tts_model: str = "gemini-2.5-flash-preview-tts"
    tts_default_voice: str = "Kore"
    tts_max_text_length: int = 5000

    # WhatsApp Client
    whatsapp_client_url: str = "http://localhost:3001"
    whatsapp_client_timeout: int = 30

    # External APIs
    jina_api_key: str | None = None  # Optional, for higher rate limits (500 vs 20 RPM)

    model_config = SettingsConfigDict(
        env_file=get_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
