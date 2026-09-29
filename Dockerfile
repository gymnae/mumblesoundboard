# Use Python 3.11 Slim (Debian Bookworm)
FROM python:3.11-slim-bookworm

# 1. Install runtime dependencies (stable layer, rarely changes -> good cache hits)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    && rm -rf /var/lib/apt/lists/*

# 2. Security: Create non-root user
RUN useradd -m -u 1000 appuser

# gunicorn's control server writes to $HOME; make sure it's writable
ENV HOME=/app

WORKDIR /app

# 3. Setup Permissions
RUN mkdir -p /app/data /app/sounds && \
    chown -R appuser:appuser /app

# 4. Environment Variables
# pymumble's generated code (protoc <3.19) needs the pure-Python protobuf
# implementation when running under the modern protobuf required by LiveKit.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

# 5. Install resolver-compatible dependencies first. pymumble and opuslib have
# obsolete protobuf pins, so install them without dependency metadata afterward.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt && \
    pip install --no-deps pymumble==1.6.1 opuslib==3.0.1 && \
    python -c "import importlib.metadata as m; from google.protobuf.internal import builder; import opuslib, pymumble_py3, livekit, livekit.api; assert m.version('livekit') == '1.1.20'; assert int(m.version('protobuf').split('.')[0]) >= 5; print('deps OK')"

# 6. Copy App Code
COPY . .
RUN chown -R appuser:appuser /app

# 7. Switch User
USER appuser

# 8. Run Gunicorn (1 Worker, 20 Threads)
# This handles the spamming issue by allowing concurrent requests
CMD ["gunicorn", "--worker-class", "gthread", "--threads", "20", "--workers", "1", "--bind", "0.0.0.0:5000", "app:app"]
