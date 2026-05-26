"""Runtime-overridable settings layer.

A curated subset of settings can be overridden at runtime via the
``runtime_settings`` DB table (written through the ``/admin`` API). Behavioural
code reads these through ``runtime_config.get(key)``, which returns the DB
override when one exists and the key is *hot*, otherwise the env/default value
from :data:`config.settings`.

Overrides are cached in-process for ``_CACHE_TTL_SECONDS`` to bound DB reads.
The cache is busted immediately after a write in the same process (via
``invalidate()``); other processes (e.g. the stream worker) pick up changes
within the TTL.

The :data:`REGISTRY` is the single source of truth for what the dashboard sees
and what may be overridden. Settings marked ``hot=False`` are read once at
startup (connection pools, middleware, background loops) and are exposed
read-only so the dashboard can show them with a "needs restart" tag.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

from .config import settings
from .logger import logger

SettingType = Literal["str", "int", "float", "bool"]

_CACHE_TTL_SECONDS = 10.0


@dataclass(frozen=True)
class SettingSpec:
    """Declarative description of a configurable setting."""

    key: str
    type: SettingType
    hot: bool
    category: str
    description: str
    choices: tuple[str, ...] | None = None
    secret: bool = False


# NOTE: only settings whose consumption site actually reads through
# ``runtime_config.get()`` may be marked ``hot=True`` — otherwise the dashboard
# would advertise an override that silently never applies.
REGISTRY: tuple[SettingSpec, ...] = (
    # --- Hot: access control ---
    SettingSpec(
        "whitelist_phones",
        "str",
        True,
        "access",
        "Comma-separated allowed phone numbers / group JIDs (empty = all allowed).",
    ),
    # --- Hot: conversation behaviour ---
    SettingSpec(
        "history_limit_private",
        "int",
        True,
        "conversation",
        "Number of history messages loaded for private chats.",
    ),
    SettingSpec(
        "history_limit_group",
        "int",
        True,
        "conversation",
        "Number of history messages loaded for group chats.",
    ),
    SettingSpec(
        "core_memory_max_length",
        "int",
        True,
        "memory",
        "Maximum characters for the per-user core-memory document.",
    ),
    # --- Hot: semantic / knowledge-base search ---
    SettingSpec(
        "semantic_search_limit",
        "int",
        True,
        "search",
        "Number of past messages returned by conversation search.",
    ),
    SettingSpec(
        "semantic_similarity_threshold",
        "float",
        True,
        "search",
        "Cosine-similarity cutoff for conversation search (0-1).",
    ),
    SettingSpec(
        "semantic_context_window",
        "int",
        True,
        "search",
        "Messages of context returned around each semantic match.",
    ),
    SettingSpec(
        "kb_search_limit",
        "int",
        True,
        "knowledge_base",
        "Number of chunks returned by knowledge-base search.",
    ),
    SettingSpec(
        "kb_similarity_threshold",
        "float",
        True,
        "knowledge_base",
        "Cosine-similarity cutoff for knowledge-base search (0-1).",
    ),
    # --- Hot: PDF parsing ---
    SettingSpec(
        "pdf_parser",
        "str",
        True,
        "knowledge_base",
        "PDF parser to use.",
        choices=("auto", "llamaparse", "docling"),
    ),
    SettingSpec(
        "llamaparse_tier",
        "str",
        True,
        "knowledge_base",
        "LlamaParse quality/cost tier.",
        choices=("fast", "cost_effective", "agentic", "agentic_plus"),
    ),
    SettingSpec(
        "llamaparse_timeout_seconds",
        "int",
        True,
        "knowledge_base",
        "Per-parse LlamaParse timeout (must stay below kb_processing_timeout_seconds).",
    ),
    SettingSpec(
        "conversation_pdf_ttl_hours",
        "int",
        True,
        "knowledge_base",
        "Hours a conversation-scoped PDF is retained before cleanup.",
    ),
    # --- Hot: speech ---
    SettingSpec(
        "stt_provider",
        "str",
        True,
        "speech",
        "Speech-to-text backend.",
        choices=("auto", "groq", "whisper"),
    ),
    SettingSpec("groq_stt_model", "str", True, "speech", "Groq Whisper model name."),
    SettingSpec(
        "whisper_base_url",
        "str",
        True,
        "speech",
        "Self-hosted Whisper base URL (empty disables self-hosted).",
    ),
    SettingSpec("whisper_model", "str", True, "speech", "Self-hosted Whisper model name."),
    SettingSpec("tts_default_voice", "str", True, "speech", "Default Gemini TTS voice."),
    SettingSpec("tts_model", "str", True, "speech", "Gemini TTS model name."),
    SettingSpec(
        "tts_max_text_length",
        "int",
        True,
        "speech",
        "Maximum characters accepted for TTS synthesis.",
    ),
    # --- Display-only (read at startup → restart required) ---
    SettingSpec("log_level", "str", False, "runtime", "Log level (applied at startup)."),
    SettingSpec(
        "rate_limit_global",
        "int",
        False,
        "runtime",
        "Global rate limit, requests/minute (applied at startup).",
    ),
    SettingSpec(
        "rate_limit_expensive",
        "int",
        False,
        "runtime",
        "Expensive-endpoint rate limit, requests/minute (applied at startup).",
    ),
    SettingSpec(
        "cleanup_interval_minutes",
        "int",
        False,
        "runtime",
        "Expired-document cleanup interval (applied at startup).",
    ),
    SettingSpec(
        "cors_origins",
        "str",
        False,
        "runtime",
        "Comma-separated allowed CORS origins (applied at startup).",
    ),
    SettingSpec(
        "database_url",
        "str",
        False,
        "infra",
        "PostgreSQL connection string (applied at startup).",
        secret=True,
    ),
    SettingSpec("redis_host", "str", False, "infra", "Redis host (applied at startup)."),
    SettingSpec("redis_port", "int", False, "infra", "Redis port (applied at startup)."),
)

REGISTRY_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in REGISTRY}


def coerce_value(spec: SettingSpec, value: Any) -> Any:
    """Validate and normalise an incoming override value for ``spec``.

    Raises ``ValueError`` with a human-readable message on type/choice mismatch.
    Note: ``bool`` is intentionally rejected for numeric specs (Python treats
    ``bool`` as ``int``), and ``int`` is accepted (and widened) for float specs.
    """
    if spec.type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"'{spec.key}' must be a boolean")
        return value
    if spec.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"'{spec.key}' must be an integer")
        return value
    if spec.type == "float":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"'{spec.key}' must be a number")
        return float(value)
    # str
    if not isinstance(value, str):
        raise ValueError(f"'{spec.key}' must be a string")
    if spec.choices is not None and value not in spec.choices:
        raise ValueError(f"'{spec.key}' must be one of: {', '.join(spec.choices)}")
    return value


class _RuntimeConfig:
    """Thread-safe, TTL-cached view of the runtime-settings overlay."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._overrides: dict[str, Any] = {}
        self._loaded_at: float = 0.0

    def _refresh_locked(self) -> None:
        # Lazy import keeps this module free of a hard dependency on the ORM at
        # import time and avoids any import-ordering surprises.
        import json

        from .database import RuntimeSetting, SessionLocal

        overrides: dict[str, Any] = {}
        db = SessionLocal()
        try:
            for row in db.query(RuntimeSetting).all():
                spec = REGISTRY_BY_KEY.get(row.key)
                if spec is None or not spec.hot:
                    continue
                try:
                    overrides[row.key] = json.loads(row.value)
                except (ValueError, TypeError):
                    logger.warning("Ignoring malformed runtime override for '%s'", row.key)
        finally:
            db.close()
        self._overrides = overrides
        self._loaded_at = time.monotonic()

    def _ensure_fresh(self) -> None:
        if time.monotonic() - self._loaded_at < _CACHE_TTL_SECONDS:
            return
        with self._lock:
            if time.monotonic() - self._loaded_at < _CACHE_TTL_SECONDS:
                return
            try:
                self._refresh_locked()
            except Exception:
                # Never let a settings read break a request — fall back to the
                # last known overrides (or env defaults) and back off briefly.
                logger.error(
                    "Failed to refresh runtime settings; using last known values",
                    exc_info=True,
                )
                self._loaded_at = time.monotonic()

    def get(self, key: str) -> Any:
        """Return the effective value for ``key`` (override if hot+present, else env)."""
        spec = REGISTRY_BY_KEY.get(key)
        if spec is None or not spec.hot:
            return getattr(settings, key)
        self._ensure_fresh()
        if key in self._overrides:
            return self._overrides[key]
        return getattr(settings, key)

    def current_overrides(self) -> dict[str, Any]:
        """Return a copy of the currently cached (hot) overrides."""
        self._ensure_fresh()
        return dict(self._overrides)

    def invalidate(self) -> None:
        """Force the next read to reload overrides from the database."""
        with self._lock:
            self._loaded_at = 0.0


runtime_config = _RuntimeConfig()
