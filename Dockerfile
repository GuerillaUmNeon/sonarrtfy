FROM python:3.11-slim

# tzdata is required for zoneinfo/ZoneInfo to resolve "Europe/Amsterdam"
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY app.py .

# Run as non-root
RUN useradd --create-home --shell /bin/bash appuser
USER appuser

EXPOSE 5000

# Flask dev server is fine here; swap for gunicorn if you want.
CMD ["python", "app.py"]