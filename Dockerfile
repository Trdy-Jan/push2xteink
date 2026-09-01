FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_PATH=/data/config.yaml \
    DB_PATH=/data/state.db \
    PORT=8080

# tzdata so a non-UTC TZ env resolves (APScheduler needs the zoneinfo db)
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only what `pip install .` needs (keeps the layer cache tight).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Non-root. /data is a bind mount at runtime; the baked-in dir is owned by app
# so a first run on an empty named volume is writable.
RUN useradd --system --uid 10001 --home /home/app --create-home app \
    && mkdir -p /data && chown app:app /data
USER app

EXPOSE 8080

# slim has no curl; hit the auth-exempt /healthz with urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/healthz', timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "push2xteink"]
