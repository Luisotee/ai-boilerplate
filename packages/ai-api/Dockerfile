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

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application source
COPY src ./src

# Create directory for knowledge base uploads
RUN mkdir -p /app/knowledge_base

# Set environment variables
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"
ENV PYTHONUNBUFFERED=1

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Default command (API server)
CMD ["uvicorn", "ai_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
