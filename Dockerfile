FROM python:3.12-slim

# apt-get upgrade: pull Debian security updates the base image may lag behind
# (Trivy fails the build on fixed HIGH/CRITICAL CVEs in base-layer packages)
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
    curl \
    # Chromium runtime deps (Debian trixie names — playwright install-deps uses Ubuntu names)
    libasound2t64 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcairo2 \
    libcups2t64 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0t64 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    fonts-liberation \
    fonts-unifont \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium to a world-readable path so non-root user can run it
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium \
    && chmod -R o+rx /ms-playwright \
    && rm -rf /root/.cache/pip

COPY VERSION* ./
COPY app/ ./app/

RUN useradd --uid 1000 --no-create-home --shell /sbin/nologin happenhere \
    && mkdir -p /app/data \
    && chown -R happenhere /app

USER happenhere

EXPOSE 8080

ENV PYTHONUNBUFFERED=1
ENV CONFIG_PATH=/app/config.yml

ARG BUILD_VERSION=dev
LABEL org.opencontainers.image.title="happenhere" \
      org.opencontainers.image.description="Local events aggregator: venue sources to a public events page and calendar feeds" \
      org.opencontainers.image.version="${BUILD_VERSION}"

# Run with an init as PID 1 (compose `init: true`, `docker run --init`): uvicorn does not reap
# the Chromium helpers that outlive their parent, and they stay as zombies (roadmap item 20).
# Proxy headers are trusted only from FORWARDED_ALLOW_IPS (uvicorn's env var; default
# 127.0.0.1). Set it to the reverse proxy's address, never "*".
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
