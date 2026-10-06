FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY off_products ./off_products
ENV PORT=8000
CMD ["sh", "-c", "uvicorn off_products.api:app --host 0.0.0.0 --port ${PORT}"]
