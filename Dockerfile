FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt && \
    playwright install chromium --with-deps

COPY . .

CMD gunicorn app:app --bind 0.0.0.0:${PORT:-5001} --workers 1 --threads 4 --timeout 300
