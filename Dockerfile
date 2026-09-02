FROM python:3.12-slim

# 国内构建加速：默认走清华源。海外 / CI 构建关掉即可：
#   docker build --build-arg APT_MIRROR= --build-arg PIP_INDEX_URL=https://pypi.org/simple .
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    CONFIG_PATH=/data/config.yaml \
    DB_PATH=/data/state.db \
    PORT=8080

# tzdata so a non-UTC TZ env resolves (APScheduler needs the zoneinfo db)
RUN if [ -n "$APT_MIRROR" ] && [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g; s|security.debian.org|$APT_MIRROR|g" \
            /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only what `pip install .` needs (keeps the layer cache tight).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" .

# Non-root default. /data is a bind mount at runtime; the baked-in dir is owned
# by app so a first run on an empty named volume is writable. NOTE: compose sets
# `user: "${APP_UID}:${APP_GID}"`, which overrides this USER at runtime (so the
# container runs as the host user and ./data stays host-editable).
RUN useradd --system --uid 10001 --home /home/app --create-home app \
    && mkdir -p /data && chown app:app /data
USER app

EXPOSE 8080

# slim has no curl; hit the auth-exempt /healthz with urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/healthz', timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "push2xteink"]
