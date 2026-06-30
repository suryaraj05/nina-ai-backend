FROM python:3.11.9-slim

WORKDIR /app

# System deps for Playwright (optional, used by generator DOM enrichment)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps + JSON schemas (contract validation)
COPY pyproject.toml ./
COPY schemas/ ./schemas/
COPY src/ ./src/

RUN pip install --no-cache-dir -e . \
    && pip install --no-cache-dir fastapi>=0.110 uvicorn[standard] redis cryptography sentry-sdk psycopg2-binary

# Expose console port
EXPOSE 8787

# Non-root user
RUN useradd -m nina && chown -R nina /app
USER nina

ENV PYTHONUNBUFFERED=1

# When DATABASE_URL is set, multiple workers are safe — state is in PostgreSQL.
# Without DATABASE_URL, keep --workers 1 (JSON-file store is single-process only).
CMD python -m uvicorn nina.console_app:app \
    --host 0.0.0.0 --port 8787 \
    --workers ${UVICORN_WORKERS:-1}
