FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ shared/
COPY radar_bot/ radar_bot/
COPY prep_bot/ prep_bot/
COPY win_bot/ win_bot/
COPY templates/ templates/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
