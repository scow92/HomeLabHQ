FROM python:3.13-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY web/ ./web/

ENV NM_DATA_DIR=/data \
    NM_WEB_DIR=/app/web \
    NM_PORT=8770 \
    PYTHONUNBUFFERED=1
EXPOSE 8770
VOLUME ["/data"]

CMD ["python3", "backend/app.py"]
