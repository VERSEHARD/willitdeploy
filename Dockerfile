FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TEMP_STORAGE_DIR=/tmp/willitdeploy

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates xz-utils make g++ python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /data /app/data /tmp/willitdeploy \
    && chmod 1777 /tmp/willitdeploy

EXPOSE 8080
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 900 app:application"]
