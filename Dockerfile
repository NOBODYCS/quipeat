FROM node:22-alpine AS css
WORKDIR /build
COPY package.json tailwind.config.js ./
COPY app/static/css/input.css app/static/css/input.css
COPY app/templates app/templates
RUN npm install --no-audit --no-fund && npm run build:css && mkdir -p /out && cp app/static/css/app.css /out/app.css
# htmx and fonts are vendored (no runtime CDN dependency); only app.css is built here.

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN pip install --no-cache-dir -r requirements.txt && \
    python -m playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/* /tmp/*
COPY app app
COPY --from=css /out/app.css app/static/css/app.css
RUN mkdir -p /app/data/uploads
ENV BIND_ADDR=0.0.0.0 PORT=8000 DATABASE_PATH=/app/data/quipeat.db UPLOAD_DIR=/app/data/uploads
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host ${BIND_ADDR} --port ${PORT}"]
