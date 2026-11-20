FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update && apt-get install --no-install-recommends -y \
    ca-certificates \
    curl \
    libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN python scripts/import_directory.py

EXPOSE 5001

CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-5001} --workers 1 --threads 4 --timeout 300"]
