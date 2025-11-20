# Use Python 3.11 Slim (Debian Bookworm) - Good balance of size and compatibility
FROM python:3.11-slim-bookworm

# 1. Install only runtime dependencies
# --no-install-recommends: CRITICAL. Prevents installing docs, X11 libs, and extras.
# We removed 'git' and 'curl' as they are not needed for the app to run.
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

# 4. Environment Variables for Python optimization
# PYTHONDONTWRITEBYTECODE=1: Prevents Python from writing .pyc files (saves space/clutter)
# PYTHONUNBUFFERED=1: Ensures logs show up immediately
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 5. Install Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade yt-dlp

# 6. Copy App Code
COPY . .
RUN chown -R appuser:appuser /app

# 7. Switch User
USER appuser

CMD ["python", "app.py"]
