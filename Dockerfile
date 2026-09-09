# Sonic Iceberg — FastAPI app that turns a Spotify export into an iceberg.
#
#   docker build -t sonic-iceberg .
#   docker run --rm -it -p 8000:8000 --env-file .env -v iceberg-data:/app/data sonic-iceberg
#
# The volume keeps the shared artist cache (data/cache.duckdb) and finished
# icebergs across restarts. Without it they're rebuilt from scratch.

FROM python:3.12-slim

# Don't buffer logs, don't write .pyc files, don't cache pip downloads.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so this layer is cached until requirements change.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Then the code. .dockerignore keeps .env, .venv, and personal data out.
COPY src/ src/
COPY data/samples/ data/samples/
COPY run.py .

# The app reads exports and writes results under /app/data.
RUN mkdir -p data/jobs && useradd --create-home app && chown -R app:app /app
USER app

EXPOSE 8000
# $PORT is set by most hosts (Render, Railway, Fly); default to 8000 locally.
CMD ["sh", "-c", "exec uvicorn iceberg.app:app --app-dir src --host 0.0.0.0 --port ${PORT:-8000}"]
