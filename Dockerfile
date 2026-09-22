# Use Python 3.11 Slim (Debian Bookworm)
FROM python:3.11-slim-bookworm

# 1. Install runtime dependencies (stable layer, rarely changes -> good cache hits)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    && rm -rf /var/lib/apt/lists/*

# 1b. Install build dependencies (separate layer so runtime layer stays cached).
#     These are only needed to build Matrix E2E (python-olm) wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libolm-dev gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

# 2. Security: Create non-root user
RUN useradd -m -u 1000 appuser

WORKDIR /app

# 3. Setup Permissions
RUN mkdir -p /app/data /app/sounds && \
    chown -R appuser:appuser /app

# 4. Environment Variables
# pymumble's generated code (protoc <3.19) needs the pure-Python protobuf
# implementation when running under protobuf 4.x
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

# 5. Install Dependencies (now includes gunicorn)
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt && \
    pip install --no-deps pymumble==1.6.1 opuslib==3.0.1 && \
    pip install --upgrade "protobuf>=4.21,<5" && \
    python -c "from google.protobuf.internal import builder; import opuslib, pymumble_py3, nio, livekit, livekit.api; print('deps OK')"

# 6. Copy App Code
COPY . .
RUN chown -R appuser:appuser /app

# 7. Switch User
USER appuser

# 8. Run Gunicorn (1 Worker, 20 Threads)
# This handles the spamming issue by allowing concurrent requests
CMD ["gunicorn", "--worker-class", "gthread", "--threads", "20", "--workers", "1", "--bind", "0.0.0.0:5000", "app:app"]
