FROM python:3.13-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY constraints.txt .
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY backend/ ./backend/
COPY web/ ./web/

ENV HLHQ_DATA_DIR=/data \
    HLHQ_WEB_DIR=/app/web \
    HLHQ_PORT=8770 \
    PYTHONUNBUFFERED=1
EXPOSE 8770 8771
VOLUME ["/data"]

CMD ["python3", "backend/app.py"]
