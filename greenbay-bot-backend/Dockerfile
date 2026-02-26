# Multi-stage build for optimal size and security

# Stage 1: Build stage
FROM python:3.13-slim as base
# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create app directory and user
WORKDIR /app
RUN groupadd -r appuser && useradd -r -g appuser -m appuser

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories and set up cache directories
RUN mkdir -p temp_images logs receipts /home/appuser/.cache/huggingface/transformers /home/appuser/.cache/sentence-transformers

# Set environment variables for model caching
ENV HF_HOME=/home/appuser/.cache/huggingface
ENV TRANSFORMERS_CACHE=/home/appuser/.cache/huggingface/transformers
ENV SENTENCE_TRANSFORMERS_HOME=/home/appuser/.cache/sentence-transformers

# Pre-download the sentence transformer model as root to avoid permission issues
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-mpnet-base-v2')" || echo "Model download failed, will try at runtime"

# Fix permissions after model download
RUN chown -R appuser:appuser /app /home/appuser && \
    chmod -R 755 /home/appuser

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8888

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8888/health || exit 1

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8888"]
