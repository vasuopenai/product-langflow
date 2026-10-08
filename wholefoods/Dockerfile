# Whole Foods brand products service. Build context is the repo root:
#   docker compose up -d --build wholefoods
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY wholefoods ./wholefoods
ENV PORT=8002
CMD ["sh", "-c", "uvicorn wholefoods.api:app --host 0.0.0.0 --port ${PORT}"]
