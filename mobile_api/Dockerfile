# Mobile app gateway. Build context is the repo root:
#   docker compose up -d --build mobile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY off_products ./off_products
COPY kroger_sync ./kroger_sync
COPY mobile_api ./mobile_api
ENV PORT=8003
CMD ["sh", "-c", "uvicorn mobile_api.api:app --host 0.0.0.0 --port ${PORT}"]
