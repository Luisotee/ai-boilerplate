# Stage 1: Build dependencies
FROM python:3.11-slim-bookworm AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install the optional [docling] extra at build time.
# Default: false — LlamaParse is the primary parser and doesn't need Docling.
# Set INSTALL_DOCLING=true to bundle Docling as a local fallback.
ARG INSTALL_DOCLING=false

# Copy dependency files and source (needed for package build)
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Install dependencies, conditionally including the [docling] extra
RUN if [ "$INSTALL_DOCLING" = "true" ]; then \
      uv sync --frozen --no-dev --extra docling; \
    else \
      uv sync --frozen --no-dev; \
    fi

# Stage 2: Runtime
FROM python:3.11-slim-bookworm AS runtime

# Re-declare the build arg so the runtime stage can reference it.
ARG INSTALL_DOCLING=false

# Install system dependencies.
# - ffmpeg: audio processing for pydub (TTS/STT) — always needed.
# - poppler-utils, tesseract-ocr, libmagic1: only needed by Docling fallback.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
    && if [ "$INSTALL_DOCLING" = "true" ]; then \
         apt-get install -y --no-install-recommends \
           poppler-utils \
           tesseract-ocr \
           libmagic1; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# Copy uv for runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Non-root user (uid/gid 1000), created before the application files are copied.
# Writable paths:
# - /app: the working directory (some libraries drop files in the cwd)
# - /app/knowledge_base: PDF uploads (the knowledge-base-data volume)
# - /home/appuser/.cache: model caches — Docling pulls its layout/OCR models from
#   Hugging Face on first parse (HF_HOME), EasyOCR into EASYOCR_MODULE_PATH, and
#   tiktoken caches its encoding (TIKTOKEN_CACHE_DIR). docker-compose mounts the
#   model-cache volume here on the worker so they survive container recreation.
# A NEW named volume inherits this ownership on first mount; an EXISTING one keeps
# its old (root) owner and must be chowned once — see CLAUDE.md.
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/knowledge_base /home/appuser/.cache \
    && chown appuser:appuser /app /app/knowledge_base /home/appuser/.cache

WORKDIR /app

# Code and dependencies stay root-owned (read-only for appuser)
COPY --from=builder /app/.venv /app/.venv
COPY src ./src

# Set environment variables
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"
ENV PYTHONUNBUFFERED=1
ENV HOME=/home/appuser
ENV HF_HOME=/home/appuser/.cache/huggingface
ENV EASYOCR_MODULE_PATH=/home/appuser/.cache/easyocr
ENV TIKTOKEN_CACHE_DIR=/home/appuser/.cache/tiktoken

USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Default command (API server)
CMD ["uvicorn", "ai_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
