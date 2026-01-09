# Stage 1: Build dependencies
FROM python:3.11-slim-bookworm AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency files and source (needed for package build)
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Create virtual environment and install dependencies
RUN uv sync --frozen --no-dev

# Stage 2: Runtime
FROM python:3.11-slim-bookworm AS runtime

# Install system dependencies required by docling for PDF parsing
# - poppler-utils: PDF rendering (pdftoppm, pdfinfo)
# - tesseract-ocr: OCR for scanned PDFs
# - libmagic1: File type detection
# - ffmpeg: Audio processing for pydub (TTS/STT)
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    tesseract-ocr \
    libmagic1 \
    ffmpeg \
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
