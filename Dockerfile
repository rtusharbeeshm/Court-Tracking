# The court's passed-over panel requires a real headless Chromium browser.
FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRACKER_DATA_DIR=/app/data \
    POLL_SECONDS=35 \
    PASSOVER_CHECK_SECONDS=120

COPY requirements.txt ./
# libatomic1 is required by Playwright's Node driver on slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY app.py engine.py scraper.py cloud_auth.py ./
COPY static ./static
RUN mkdir -p /app/data && chmod 700 /app/data
EXPOSE 10000
# One process = one poller and one SQLite writer. Do not use multiple workers.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1"]
