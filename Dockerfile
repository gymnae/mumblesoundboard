FROM python:3.11-slim-bookworm

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libopus0 \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create directories
RUN mkdir -p /app/data /app/sounds

# Install Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --upgrade yt-dlp

# Copy App
COPY . .

CMD ["python", "app.py"]