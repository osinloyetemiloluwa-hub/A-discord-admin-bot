# Dockerfile — Hugging Face Docker Space
# Builds and runs the AI Co-Admin Discord bot. Exposes port 7860 (HF's
# default) so the Space registers as "Running" via utils/keepalive.py.

FROM python:3.11-slim

WORKDIR /app

# System deps for aiosqlite/aiohttp build wheels (kept minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Create a non-root user (Hugging Face Spaces best practice)
RUN useradd -m -u 1000 botuser

COPY --chown=botuser:botuser . .

# Persistent-ish data dir for the SQLite DB (ephemeral unless a
# Space persistent storage volume is attached — see README)
RUN mkdir -p /app/data && chown -R botuser:botuser /app/data

USER botuser

ENV PYTHONUNBUFFERED=1
EXPOSE 7860

CMD ["python", "main.py"]
