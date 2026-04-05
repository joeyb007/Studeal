FROM python:3.13-slim

WORKDIR /app

# System deps needed by asyncpg and bcrypt
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY dealbot/ ./dealbot/
COPY alembic/ ./alembic/
COPY alembic.ini ./

RUN pip install --no-cache-dir -e .

CMD ["uvicorn", "dealbot.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
