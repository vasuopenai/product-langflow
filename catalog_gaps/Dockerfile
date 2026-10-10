# Catalog gap finder (long-running worker). Build context is the repo root:
#   COMPOSE_PROFILES=catalog docker compose up -d --build catalog
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY off_products ./off_products
COPY kroger_sync ./kroger_sync
COPY mobile_api ./mobile_api
COPY catalog_gaps ./catalog_gaps
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "catalog_gaps", "run"]
