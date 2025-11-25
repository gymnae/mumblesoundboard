# Use Python 3.11 Slim (Debian Bookworm)
FROM python:3.11-slim-bookworm

# 1. Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    && rm -rf /var/lib/apt/lists/*

# 2. Security: Create non-root user
RUN useradd -m -u 1000 appuser

WORKDIR /app

# 3. Setup Permissions
RUN mkdir -p /app/data /app/sounds && \
    chown -R appuser:appuser /app

# 4. Environment Variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 5. Install Dependencies (now includes gunicorn)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade yt-dlp

# 6. Copy App Code
COPY . .
RUN chown -R appuser:appuser /app

# 7. Switch User
USER appuser

# 8. Run Gunicorn (1 Worker, 20 Threads)
# This handles the spamming issue by allowing concurrent requests
CMD ["gunicorn", "--worker-class", "gthread", "--threads", "20", "--workers", "1", "--bind", "0.0.0.0:5000", "app:app"]
