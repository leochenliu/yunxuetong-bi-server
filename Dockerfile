FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    BI_API_HOST=0.0.0.0 \
    BI_API_PORT=8787 \
    BI_DB_PATH=/data/store/bi.sqlite3 \
    BI_INBOUND_DIR=/data/inbound

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home-dir /app app \
    && mkdir -p /data/store /data/inbound \
    && chown -R app:app /app /data

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app src ./src
COPY --chown=app:app config ./config
COPY --chown=app:app sql ./sql
COPY --chown=app:app web ./web

USER app

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3)" || exit 1

CMD ["python", "-m", "bi.cli", "api"]
