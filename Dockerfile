# PRGuard: CLI for CI pipelines (`prguard review|describe|improve|ask|run`) and
# the webhook server + dashboard (`prguard serve`, the default command).
#
#   docker build -t prguard .
#   docker build -t prguard-sandbox backend/sandbox      # only needed for --sandbox / server mode

FROM node:20-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Docker CLI only (no daemon): server mode starts the sandbox as a sibling
# container through a mounted Docker socket.
FROM docker:27-cli AS dockercli

FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ backend/
COPY demo_target/ demo_target/
COPY --from=ui /ui/dist frontend/dist
RUN printf '#!/bin/sh\nexec python -m backend.cli "$@"\n' > /usr/local/bin/prguard \
 && chmod +x /usr/local/bin/prguard \
 && useradd --create-home --uid 10001 prguard \
 && mkdir -p /var/lib/prguard/runs /out \
 && chown -R prguard /var/lib/prguard /out \
 && chmod 1777 /var/lib/prguard/runs /out   # writable when CI runs the container as its own uid
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    RUNS_DIR=/var/lib/prguard/runs \
    UI_DIST_DIR=/app/frontend/dist
USER prguard
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
ENTRYPOINT ["prguard"]
CMD ["serve"]
