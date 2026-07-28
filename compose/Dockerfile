# syntax=docker/dockerfile:1
# Dev container: Flask backend (:8000) + Vite frontend (:5173) in one image.
# The vite proxy targets localhost:8000, so both run in the same container.

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
# Writable HOME for the non-root compose user (npm cache/logs)
ENV HOME=/tmp

# ffmpeg for the pipeline; Node 20+ (vite 7 needs >=20.19) via NodeSource
RUN apt-get update && apt-get install -y ffmpeg curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Slim runtime deps only — the full requirements.txt drags torch/spacy in
COPY requirements-backend.txt .
RUN pip install --no-cache-dir -r requirements-backend.txt

# Frontend deps baked into the image; compose keeps them in an anonymous
# volume so the repo bind-mount doesn't shadow them. chown so the non-root
# compose user (1000) can write vite's node_modules/.vite cache.
COPY web/package*.json ./web/
RUN cd web && npm install && chown -R 1000:1000 node_modules

EXPOSE 8000 5173

CMD ["/bin/sh", "-c", "(cd web && npm run dev) & exec python3 web_interface.py"]
