# Kroger service. Build context is the repo root:
#   docker compose up -d --build kroger
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kroger_sync ./kroger_sync
ENV PORT=8001
CMD ["sh", "-c", "uvicorn kroger_sync.api:app --host 0.0.0.0 --port ${PORT}"]
