# One image, three roles: the ECS task definitions (and docker-compose) pick
# the role by overriding the command — uvicorn (api), celery worker, celery
# beat. Browsers are remote (Browserbase over CDP), so no Chromium is
# installed here; the playwright pip package is only the CDP client.

FROM python:3.12-slim

# asyncpg and bcrypt compile small C extensions at install time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Layer-caching order: dependencies install against pyproject.toml ALONE, in
# their own layer, before any application code is copied. Docker reuses a
# layer when its inputs are unchanged — so editing dealbot/*.py rebuilds only
# the cheap COPY layers below, not the multi-minute pip install. The stub
# package exists purely so `pip install .` has something to build.
COPY pyproject.toml ./
RUN mkdir dealbot && touch dealbot/__init__.py \
    && pip install --no-cache-dir . \
    && pip uninstall -y dealbot \
    && rm -rf dealbot

# Now the real code. It is NOT pip-installed — /app is the working directory
# and both uvicorn and celery resolve `dealbot.*` from the cwd, which keeps
# the image layout identical to the repo layout.
COPY dealbot/ ./dealbot/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Containers run as root by default; a process that never needs root
# shouldn't have it (defense in depth if anything in the container is ever
# compromised).
RUN useradd --create-home appuser
USER appuser

# Default role: api. Worker/beat override this in compose / ECS task defs.
CMD ["uvicorn", "dealbot.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
